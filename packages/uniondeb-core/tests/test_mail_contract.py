"""Journaled email contract: RFC 5322 and Microsoft Graph shapes, journal semantics.

Verifies: UND-011
"""

import pytest
from pydantic import ValidationError

from uniondeb.core.mail import EmailEvent
from uniondeb.core.resolve import ResolutionReport

BASE = {
    "message_id": "<m1@cyberscallywags.example>",
    "sender": "Siân Jones <Sian.Jones@cyberscallywags.example>",
    "to": '"Jones, Dafydd" <dafydd.jones@example.org>; nia@cyberscallywags.example',
    "subject": "Rota",
    "body": "See attached",
    "sent_at": "2026-11-19T09:00:00Z",
    "journal": {"source": "m365_envelope", "received_at": "2026-11-19T09:00:03Z"},
}


def test_rfc5322_strings_and_direction() -> None:
    e = EmailEvent.model_validate(BASE)
    assert e.sender.address == "sian.jones@cyberscallywags.example" and e.sender.name == "Siân Jones"
    assert [a.address for a in e.to] == ["dafydd.jones@example.org", "nia@cyberscallywags.example"]
    assert e.to[0].name == "Jones, Dafydd"  # comma inside a quoted display name survives
    assert e.direction == "outbound"


def test_microsoft_graph_shape_resolves_with_report() -> None:
    graph = {
        "internetMessageId": "<g1@cyberscallywags.example>",
        "from": {"emailAddress": {"name": "Nia", "address": "nia@cyberscallywags.example"}},
        "toRecipients": [{"emailAddress": {"address": "nia.personal@mail.example"}}],
        "bccRecipients": [{"emailAddress": {"address": "huw@cyberscallywags.example"}}],
        "body": {"contentType": "html", "content": "<p>Fwd &amp; keep</p>"},
        "sentDateTime": "2026-11-19T21:37:00Z",
        "attachments": [{"name": "pack.pdf", "contentType": "application/pdf", "size": 10, "sha256": "b" * 64}],
        "journal": {"journal_type": "Google Workspace", "receivedDateTime": "2026-11-19T21:37:01Z"},
        "isRead": True,
    }
    report = ResolutionReport()
    e = EmailEvent.model_validate(graph, context={"report": report})
    assert (e.body, e.direction, e.journal.source) == ("Fwd & keep", "to_personal", "gws_bcc")
    assert e.attachments[0].filename == "pack.pdf"
    assert "EmailEvent: 'isRead'" in report.summary()["unknown_keys"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sender": "someone@example.org"}, "journal only carries mail sent from"),
        ({"to": ""}, "at least one recipient"),
        ({"sender": "not-an-address"}, "not an email address"),
        ({"message_id": "m1"}, "pattern"),
    ],
)
def test_contract_rejects(change: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EmailEvent.model_validate(BASE | change)


def test_envelope_recipients_count_as_recipients() -> None:
    """M365 envelope journaling exposes BCC recipients that the message headers hide."""
    doc = BASE | {"to": "", "journal": {**BASE["journal"], "envelope_recipients": "hidden@example.net"}}
    assert EmailEvent.model_validate(doc).direction == "outbound"
