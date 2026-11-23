"""The Cennad pipeline without SMTP: raw store, publish, dead letters, duplicates, storage, settings."""

import hashlib
import json
import stat

import pytest

from uniondeb.core.mail import EmailEvent
from uniondeb.mail import (
    Cennad,
    CennadSettings,
    InMemoryPublisher,
    KafkaPublisher,
    LocalObjectStore,
    NdjsonPublisher,
    to_eml,
    to_gws_copy,
    to_journal_report,
)

from .conftest import RECEIVED

PACK = b"board pack " * 500
PACK_SHA = hashlib.sha256(PACK).hexdigest()
ATT = {"filename": "pack.pdf", "content_type": "application/pdf", "size": len(PACK), "sha256": PACK_SHA}


def test_handle_stores_raw_and_publishes_canonical_json(cennad, store, publisher, make_event):
    raw = to_journal_report(make_event())
    out = cennad.handle(raw, peer_ip="192.0.2.10", received_at=RECEIVED)
    assert out.published and out.raw_new and out.error is None
    assert out.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert store.get(out.raw_sha256) == raw
    (sent,) = publisher.sent
    assert sent.key == out.key == "<0199a1b2-c3d4@cyberscallywags.example>"
    assert sent.headers == {
        "journal_source": "m365_envelope",
        "direction": "to_personal",
        "normalised": "true",
        "raw_sha256": out.raw_sha256,
        "synthetic": "false",
    }
    assert sent.doc == json.loads(EmailEvent.model_validate(sent.doc).model_dump_json())
    assert sent.doc["bcc"][0]["address"] == "sian.private@mail.example"
    assert sent.doc["device"] is None
    assert publisher.dead == []


def test_synthetic_flag_reaches_the_headers(store, publisher, make_event):
    Cennad(store, publisher, synthetic=True).handle(to_eml(make_event()), peer_ip=None)
    assert publisher.sent[0].headers["synthetic"] == "true"


def test_unparseable_mail_is_stored_and_dead_lettered(cennad, store, publisher, make_event):
    good = to_eml(make_event())
    bad = {
        "no message id": good.replace(b"Message-ID:", b"X-Was-Message-ID:"),
        "external sender": good.replace(b"sian.jones@cyberscallywags.example", b"sian.jones@example.org"),
        "garbage": b"\x00\xff\xfe not an email at all \x80" * 50,
        "no recipients": b"From: a@cyberscallywags.example\r\nMessage-ID: <x@y.example>\r\n"
        b"Date: Mon, 02 Mar 2026 09:30:15 +0000\r\n\r\nhi\r\n",
    }
    for raw in bad.values():
        out = cennad.handle(raw, peer_ip="192.0.2.10", received_at=RECEIVED)
        assert out.status == "dead_lettered" and out.error and out.key is None
        assert store.get(out.raw_sha256) == raw
    assert publisher.sent == [] and len(publisher.dead) == len(bad)
    for dead, raw in zip(publisher.dead, bad.values(), strict=True):
        assert dead.doc == {
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "error": dead.error,
            "peer_ip": "192.0.2.10",
            "received_at": RECEIVED.isoformat(),
        }
        assert dead.headers["normalised"] == "false" and dead.headers["raw_sha256"] == dead.doc["raw_sha256"]
        assert "Dyma" not in dead.error and "sian" not in dead.error


def test_duplicate_copies_share_a_key_and_identical_raw_is_stored_once(cennad, store, publisher, make_event):
    ev = make_event(source="gws_bcc", bcc=[])
    report, copy = to_journal_report(ev), to_gws_copy(ev)
    outs = [cennad.handle(raw, peer_ip=None) for raw in (report, report, copy)]
    assert [o.raw_new for o in outs] == [True, False, True]
    assert {s.key for s in publisher.sent} == {ev.message_id} and len(publisher.sent) == 3
    assert len(list((store.root / "raw").rglob("*.eml"))) == 2


def test_attachment_forwarded_in_a_second_message_is_stored_once(cennad, store, publisher, make_event):
    first = make_event(attachments=[ATT])
    forward = make_event(
        message_id="<fwd-1@cyberscallywags.example>",
        subject="lluniau gwyliau",
        to=["sian.private@mail.example"],
        cc=[],
        bcc=[],
        attachments=[{**ATT, "filename": "lluniau.zip"}],
    )
    a = cennad.handle(to_journal_report(first, {PACK_SHA: PACK}), peer_ip=None)
    b = cennad.handle(to_journal_report(forward, {PACK_SHA: PACK}), peer_ip=None)
    assert (a.attachments_stored, b.attachments_stored) == (1, 0)
    assert store.get(PACK_SHA, kind="attachments") == PACK
    assert [s.doc["attachments"][0]["sha256"] for s in publisher.sent] == [PACK_SHA, PACK_SHA]
    assert len([p for p in (store.root / "attachments").rglob("*") if p.is_file()]) == 1


def test_store_or_publisher_failure_propagates_so_smtp_can_answer_451(store, make_event):
    class Down(InMemoryPublisher):
        def send(self, key, doc, headers):
            raise ConnectionError("broker down")

    raw = to_eml(make_event())
    with pytest.raises(ConnectionError):
        Cennad(store, Down()).handle(raw, peer_ip=None)
    assert store.exists(hashlib.sha256(raw).hexdigest())  # the evidence is held; the retry republishes


# ───────────────────────────── storage ──────────────────────────────────────────────
def test_local_store_is_content_addressed_private_and_idempotent(tmp_path):
    store = LocalObjectStore(tmp_path)
    data = b"From: a@b\r\n\r\nhi\r\n"
    sha = hashlib.sha256(data).hexdigest()
    assert not store.exists(sha)
    assert store.put(sha, data) is True and store.put(sha, data) is False
    path = tmp_path / "raw" / sha[:2] / f"{sha}.eml"
    assert path.read_bytes() == data == store.get(sha)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.put(sha, data, kind="attachments") is True
    assert (tmp_path / "attachments" / sha[:2] / sha).is_file()
    assert not list(tmp_path.rglob(".tmp-*"))


def test_local_store_refuses_wrong_hash_and_path_tricks(tmp_path):
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("0" * 64, b"something else")
    for bad in ("../../etc/passwd", "AB" * 32, "abc"):
        with pytest.raises(ValueError):
            store.exists(bad)
    assert not list(tmp_path.rglob("*"))


# ───────────────────────────── publishers & settings ────────────────────────────────
def test_kafka_publisher_does_not_import_or_connect_on_construction():
    pub = KafkaPublisher("localhost:9092")
    assert (pub.topic, pub.dlq, pub._producer) == ("mail-journal", "mail-journal-dlq", None)
    pub.close()


def test_ndjson_publisher_writes_one_line_per_document(capsys):
    pub = NdjsonPublisher()
    pub.send("<k@x.example>", {"subject": "Siân"}, {"normalised": "true"})
    pub.dead_letter({"raw_sha256": "ab"}, "no Message-ID header", {"normalised": "false"})
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "key": "<k@x.example>",
        "headers": {"normalised": "true"},
        "document": {"subject": "Siân"},
    }
    assert json.loads(err)["error"] == "no Message-ID header"


def test_settings_from_env_and_fail_closed_defaults(tmp_path):
    default = CennadSettings.from_env({})
    assert default.allow == () and default.networks == () and not default.allow_plaintext and default.kafka is None
    s = CennadSettings.from_env(
        {
            "CENNAD_HOST": "0.0.0.0",
            "CENNAD_PORT": "2525",
            "CENNAD_ALLOW": "40.92.0.0/15, 2a01:111:f400::/48 ,192.0.2.7",
            "CENNAD_MAX_BYTES": "1000",
            "CENNAD_TLS_CERT": "/etc/cennad/tls.crt",
            "CENNAD_TLS_KEY": "/etc/cennad/tls.key",
            "CENNAD_STORE": str(tmp_path),
            "CENNAD_KAFKA": "kafka:9092",
        },
        port=None,
    )
    assert (s.host, s.port, s.max_bytes, s.kafka, s.store) == ("0.0.0.0", 2525, 1000, "kafka:9092", tmp_path)
    assert s.allow == ("40.92.0.0/15", "2a01:111:f400::/48", "192.0.2.7/32")
    assert str(s.tls_cert) == "/etc/cennad/tls.crt"
    with pytest.raises(ValueError):
        CennadSettings.from_env({"CENNAD_ALLOW": "not-a-network"})
