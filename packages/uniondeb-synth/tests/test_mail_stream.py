"""Email templates honour the trigger list; the stream normalises or dead-letters.

Verifies: UND-012
"""

import pytest

from uniondeb.synth import content as C
from uniondeb.synth.stream import Stats, publish

BASE = {
    "message_id": "<m1@cyberscallywags.example>",
    "sender": "Siân Jones <Sian.Jones@cyberscallywags.example>",
    "to": '"Jones, Dafydd" <dafydd.jones@example.org>; nia@cyberscallywags.example',
    "subject": "Rota",
    "body": "See attached",
    "sent_at": "2026-11-19T09:00:00Z",
    "journal": {"source": "m365_envelope", "received_at": "2026-11-19T09:00:03Z"},
}


@pytest.mark.parametrize(
    "t",
    list(C.NORMAL_EMAILS + C.BENIGN_TRIGGER_EMAILS) + list(C.INSIDER_EMAILS.values()) + list(C.DRUG_EMAILS.values()),
    ids=lambda t: t.subject[:30],
)
def test_email_templates_agree_with_trigger_list(t) -> None:
    hits = C.trigger_hits(t.text.format(day="Tue", place="X", first="Nia", n="1", project="Prosiect Morlo"))
    assert not (t.tag == "clean" and hits) and not (t.tag == "question" and not hits)


class _Capture:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, dict, dict]] = []
        self.dead: list[tuple[dict, str]] = []

    def send(self, key, doc, headers) -> None:
        self.sent.append((key, doc, headers))

    def dead_letter(self, doc, error) -> None:
        self.dead.append((doc, error))

    def close(self) -> None:
        pass


def test_publish_normalises_or_dead_letters() -> None:
    sink, stats = _Capture(), Stats()
    publish(sink, {**BASE, "isRead": True}, stats)  # unknown keys are dropped, not fatal
    publish(sink, BASE | {"sender": "x@example.org"}, stats)
    assert (stats.sent, stats.dead_lettered) == (1, 1)
    key, doc, headers = sink.sent[0]
    assert key == "<m1@cyberscallywags.example>" and doc["direction"] == "outbound"
    assert headers["normalised"] == "true" and headers["journal_source"] == "m365_envelope"
