"""The SMTP endpoint against a real aiosmtpd controller on 127.0.0.1: allowlist, size, TLS, 250/451."""

import contextlib
import shutil
import smtplib
import socket
import ssl
import subprocess

import pytest

from uniondeb.mail import Cennad, CennadSettings, InMemoryPublisher, from_settings, make_controller, to_journal_report
from uniondeb.mail.receiver import JournalHandler, tls_context

FROM, TO = "journal@cyberscallywags.example", ["cennad@cyberscallywags.example"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def running(cennad, **kw):
    controller = make_controller(cennad, host="127.0.0.1", port=free_port(), **kw)
    controller.start()
    try:
        yield controller.port
    finally:
        controller.stop()


def test_smtp_round_trip_publishes_and_stores(cennad, store, publisher, make_event):
    ev = make_event()
    raw = to_journal_report(ev)
    with running(cennad, allow=["127.0.0.0/8"], allow_plaintext=True) as port, smtplib.SMTP("127.0.0.1", port) as smtp:
        assert smtp.sendmail(FROM, TO, raw) == {}
    (sent,) = publisher.sent
    assert sent.key == ev.message_id and sent.doc["bcc"][0]["address"] == "sian.private@mail.example"
    assert sent.doc["subject"] == ev.subject and sent.doc["sender"]["name"] == "Jones, Siân"
    assert store.exists(sent.headers["raw_sha256"])
    assert publisher.dead == []


def test_accepted_garbage_is_dead_lettered_with_the_peer_ip(cennad, store, publisher):
    with running(cennad, allow=["127.0.0.1"], allow_plaintext=True) as port, smtplib.SMTP("127.0.0.1", port) as smtp:
        smtp.sendmail(FROM, TO, b"this is not a journal report\r\n")
    (dead,) = publisher.dead
    assert dead.doc["peer_ip"] == "127.0.0.1" and store.exists(dead.doc["raw_sha256"])
    assert publisher.sent == []


@pytest.mark.parametrize("allow", [[], ["192.0.2.0/24", "2001:db8::/32"]])
def test_peer_outside_the_allowlist_gets_550(cennad, store, publisher, make_event, allow):
    with (
        running(cennad, allow=allow, allow_plaintext=True) as port,
        smtplib.SMTP("127.0.0.1", port) as smtp,
        pytest.raises(smtplib.SMTPSenderRefused) as exc,
    ):
        smtp.sendmail(FROM, TO, to_journal_report(make_event()))
    assert exc.value.smtp_code == 550
    assert publisher.sent == [] and publisher.dead == [] and not (store.root / "raw").exists()


def test_allowlist_matching():
    handler = JournalHandler(None, ["192.0.2.0/24", "2001:db8::/32"], max_bytes=1)
    assert handler.allowed("192.0.2.9") and handler.allowed("2001:db8::1") and handler.allowed("::ffff:192.0.2.9")
    assert not handler.allowed("198.51.100.1") and not handler.allowed(None) and not handler.allowed("nonsense")
    assert not JournalHandler(None, [], max_bytes=1).allowed("127.0.0.1")


def test_oversize_message_gets_552(cennad, publisher, make_event):
    raw = to_journal_report(make_event(body="x" * 20_000))
    with running(cennad, allow=["127.0.0.1"], allow_plaintext=True, max_bytes=4_000) as port:
        with smtplib.SMTP("127.0.0.1", port) as smtp, pytest.raises(smtplib.SMTPResponseException) as exc:
            smtp.sendmail(FROM, TO, raw)  # refused at MAIL, from the SIZE the client declares
        assert exc.value.smtp_code == 552
        with smtplib.SMTP("127.0.0.1", port) as smtp:  # and at DATA when the client declares nothing
            smtp.helo("test")
            assert smtp.mail(FROM)[0] == 250 and smtp.rcpt(TO[0])[0] == 250
            assert smtp.data(raw)[0] == 552
    assert publisher.sent == [] and publisher.dead == []


def test_storage_failure_gets_451_so_the_mta_retries(publisher, make_event):
    class FullDisk:
        def put(self, sha256, data, *, kind="raw"):
            raise OSError(28, "No space left on device")

    with (
        running(Cennad(FullDisk(), publisher), allow=["127.0.0.1"], allow_plaintext=True) as port,
        smtplib.SMTP("127.0.0.1", port) as smtp,
        pytest.raises(smtplib.SMTPDataError) as exc,
    ):
        smtp.sendmail(FROM, TO, to_journal_report(make_event()))
    assert exc.value.smtp_code == 451
    assert publisher.sent == [] and publisher.dead == []


def test_plaintext_must_be_explicit(cennad, tmp_path):
    with pytest.raises(ValueError, match="TLS"):
        make_controller(cennad, allow=["127.0.0.1"])
    with pytest.raises(ValueError, match="TLS"):
        from_settings(CennadSettings(store=tmp_path), InMemoryPublisher())
    with pytest.raises(ValueError, match="both"):
        from_settings(CennadSettings(store=tmp_path, tls_cert=tmp_path / "c.pem"), InMemoryPublisher())
    controller = from_settings(CennadSettings(store=tmp_path, allow_plaintext=True, allow=("127.0.0.1",)))
    assert controller.handler.allowed("127.0.0.1") and not controller.handler.allowed("127.0.0.2")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI to make a test certificate")
def test_starttls_is_required_when_a_certificate_is_configured(cennad, publisher, make_event, tmp_path):
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost"]
        + ["-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    with running(cennad, allow=["127.0.0.1"], tls=tls_context(cert, key)) as port:
        with smtplib.SMTP("127.0.0.1", port) as smtp:
            smtp.ehlo()
            assert smtp.mail(FROM)[0] == 530
        with smtplib.SMTP("127.0.0.1", port) as smtp:
            client = ssl.create_default_context()
            client.check_hostname, client.verify_mode = False, ssl.CERT_NONE
            smtp.starttls(context=client)
            smtp.sendmail(FROM, TO, to_journal_report(make_event()))
    assert len(publisher.sent) == 1
