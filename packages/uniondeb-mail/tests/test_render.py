"""Render → parse round trips for the three journal shapes, and what attachment filler means.

Verifies: UND-025
"""

import hashlib

import pytest

from uniondeb.mail import parse, render, to_eml, to_gws_copy, to_journal_report
from uniondeb.mail.render import FILLER_MAX, filler

from .conftest import RECEIVED

SAME = ("message_id", "sender", "to", "cc", "subject", "body", "sent_at", "in_reply_to")
ATT = {"filename": "Pecyn bwrdd.pdf", "content_type": "application/pdf", "size": 3000, "sha256": "ab" * 32}


def assert_same(a, b, *, bcc: bool):
    for name in SAME:
        assert getattr(a, name) == getattr(b, name), name
    assert [x.filename for x in a.attachments] == [x.filename for x in b.attachments]
    assert a.journal.source == b.journal.source
    if bcc:
        assert a.bcc == b.bcc


def test_journal_report_round_trip_keeps_bcc(make_event):
    ev = make_event(attachments=[ATT], in_reply_to="<m0@cyberscallywags.example>")
    back = parse(to_journal_report(ev), received_at=RECEIVED).event
    assert_same(ev, back, bcc=True)
    assert {a.address for a in back.journal.envelope_recipients} == {a.address for a in ev.all_recipients}
    assert back.direction == ev.direction == "to_personal"


def test_journal_report_lists_envelope_only_recipients(make_event):
    ev = make_event(envelope_recipients=["dafydd.puw@cyberscallywags.example", "list-member@example.net"])
    back = parse(to_journal_report(ev)).event
    assert "list-member@example.net" in [a.address for a in back.journal.envelope_recipients]
    assert [a.address for a in back.bcc] == ["sian.private@mail.example"]


def test_gws_copy_round_trip(make_event):
    ev = make_event(source="gws_bcc", bcc=[], envelope_recipients=["ext@example.org", "hidden@example.net"])
    back = parse(to_gws_copy(ev)).event
    assert_same(ev, back, bcc=True)
    assert back.journal.envelope_recipients == ev.journal.envelope_recipients


def test_plain_eml_round_trip_loses_bcc(make_event):
    ev = make_event(source="bcc_rule", attachments=[ATT])
    back = parse(to_eml(ev)).event
    assert_same(ev, back, bcc=False)
    assert back.bcc == [] and back.journal.envelope_recipients == []


@pytest.mark.parametrize("source", ["m365_envelope", "gws_bcc", "bcc_rule"])
def test_render_follows_the_journal_source(make_event, source):
    assert parse(render(make_event(source=source))).event.journal.source == source


def test_real_attachment_bytes_reproduce_the_declared_hash(make_event):
    data = b"board pack bytes" * 100
    sha = hashlib.sha256(data).hexdigest()
    ev = make_event(attachments=[{**ATT, "size": len(data), "sha256": sha}])
    att, got = parse(to_journal_report(ev, {sha: data})).attachments[0]
    assert (att.sha256, att.size, got) == (sha, len(data), data)


def test_filler_is_deterministic_but_not_the_declared_hash(make_event):
    ev = make_event(attachments=[ATT])
    first, second = parse(to_journal_report(ev)), parse(to_eml(ev))
    att, data = first.attachments[0]
    assert data == filler(ATT["sha256"], ATT["size"]) and att.size == ATT["size"]
    assert att.sha256 == second.attachments[0][0].sha256 != ATT["sha256"]
    assert len(filler("cd" * 32, 10 * FILLER_MAX)) == FILLER_MAX
