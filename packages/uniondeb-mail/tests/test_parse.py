"""Journal parsers: the three shapes, envelope and BCC visibility, bodies, attachments, failures."""

import hashlib
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from uniondeb.mail import ParseError, parse

from .conftest import RECEIVED, SIAN, WELSH_SUBJECT

PACK = b"%PDF-1.7 board pack " * 400


def original(**headers: str) -> EmailMessage:
    msg = EmailMessage()
    base = {
        "From": SIAN,
        "To": "Dafydd Puw <dafydd.puw@cyberscallywags.example>, ext@example.org",
        "Cc": "gwen.llwyd@cyberscallywags.example",
        "Subject": WELSH_SUBJECT,
        "Date": "Mon, 02 Mar 2026 09:30:15 +0000",
        "Message-ID": "<m1@cyberscallywags.example>",
    }
    for name, value in {**base, **headers}.items():
        if value:
            msg[name] = value
    msg.set_content("Bore da,\n\nDyma'r pecyn bwrdd.\n")
    return msg


def report(inner: EmailMessage, envelope: str, *, flagged: bool = True) -> bytes:
    outer = EmailMessage()
    outer["From"] = "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@cyberscallywags.example"
    outer["To"] = "cennad@cyberscallywags.example"
    outer["Subject"] = inner["Subject"] or ""
    outer["Message-ID"] = "<journal-1@cyberscallywags.example>"
    if flagged:
        outer["X-MS-Exchange-Organization-Journal-Report"] = ""
    outer.set_content(envelope)
    outer.add_attachment(inner)
    return outer.as_bytes()


ENVELOPE = """Sender: sian.jones@cyberscallywags.example
Subject: Cyfarfod bwrdd
Message-Id: <m1@cyberscallywags.example>
To: dafydd.puw@cyberscallywags.example
To: ext@example.org
Cc: gwen.llwyd@cyberscallywags.example
Bcc: sian.private@mail.example
Recipient: archive@example.net
"""


def test_m365_report_reveals_bcc_and_envelope_recipients():
    parsed = parse(report(original(), ENVELOPE), received_at=RECEIVED)
    ev = parsed.event
    assert ev.journal.source == "m365_envelope"
    assert [a.address for a in ev.journal.envelope_recipients] == [
        "dafydd.puw@cyberscallywags.example",
        "ext@example.org",
        "gwen.llwyd@cyberscallywags.example",
        "sian.private@mail.example",
        "archive@example.net",
    ]
    assert [a.address for a in ev.bcc] == ["sian.private@mail.example"]
    assert [a.address for a in ev.to] == ["dafydd.puw@cyberscallywags.example", "ext@example.org"]
    assert ev.message_id == "<m1@cyberscallywags.example>"  # the original's, not the report's
    assert ev.direction == "to_personal"
    assert ev.journal.received_at == RECEIVED
    assert ev.sent_at == datetime(2026, 3, 2, 9, 30, 15, tzinfo=UTC)
    assert ev.body == "Bore da,\n\nDyma'r pecyn bwrdd."
    assert ev.device is None


def test_m365_expanded_and_forwarded_recipient_lines():
    envelope = (
        "Sender: sian.jones@cyberscallywags.example\r\n"
        "Message-Id: <m1@cyberscallywags.example>\r\n"
        "To: dafydd.puw@cyberscallywags.example, Expanded: bwrdd@cyberscallywags.example\r\n"
        "To: gwen.llwyd@cyberscallywags.example, Expanded: bwrdd@cyberscallywags.example\r\n"
        "Bcc: away@example.org, Forwarded: gwen.llwyd@cyberscallywags.example\r\n"
        "Recipient: <ext@example.org>\r\n"
    )
    ev = parse(report(original(), envelope)).event
    assert [a.address for a in ev.journal.envelope_recipients] == [
        "dafydd.puw@cyberscallywags.example",
        "gwen.llwyd@cyberscallywags.example",
        "away@example.org",
        "ext@example.org",
    ]
    assert [a.address for a in ev.bcc] == ["away@example.org"]


def test_m365_report_detected_by_structure_without_the_header():
    assert parse(report(original(), ENVELOPE, flagged=False)).event.journal.source == "m365_envelope"


def test_forwarded_email_is_not_mistaken_for_a_journal_report():
    msg = original(**{"Message-ID": "<fwd@cyberscallywags.example>"})
    msg.add_attachment(original())
    parsed = parse(msg.as_bytes())
    assert parsed.event.journal.source == "bcc_rule"
    assert parsed.event.message_id == "<fwd@cyberscallywags.example>"
    assert [a.content_type for a in parsed.event.attachments] == ["message/rfc822"]


def test_flagged_report_without_original_is_an_error():
    outer = EmailMessage()
    outer["X-MS-Exchange-Organization-Journal-Report"] = ""
    outer.set_content(ENVELOPE)
    with pytest.raises(ParseError, match="without the original"):
        parse(outer.as_bytes())


def test_gws_copy_repeated_and_comma_separated_original_to():
    msg = original()
    msg["X-Gm-Original-To"] = "dafydd.puw@cyberscallywags.example, ext@example.org"
    msg["X-Gm-Original-To"] = "hidden@example.net"
    ev = parse(msg.as_bytes()).event
    assert ev.journal.source == "gws_bcc"
    assert [a.address for a in ev.journal.envelope_recipients] == [
        "dafydd.puw@cyberscallywags.example",
        "ext@example.org",
        "hidden@example.net",
    ]
    assert ev.bcc == []


def test_plain_copy_is_bcc_rule_with_no_envelope():
    ev = parse(original().as_bytes()).event
    assert ev.journal.source == "bcc_rule"
    assert ev.journal.envelope_recipients == [] and ev.bcc == []
    assert ev.direction == "outbound"


def test_non_ascii_names_and_subject_survive():
    ev = parse(original().as_bytes()).event
    assert (ev.sender.name, ev.sender.address) == ("Jones, Siân", "sian.jones@cyberscallywags.example")
    assert ev.subject == WELSH_SUBJECT
    assert ev.to[0].name == "Dafydd Puw"


def test_html_only_body_is_reduced_to_text():
    msg = original()
    msg.set_content("<html><body><p>Bore da &amp; croeso</p><p>Si&acirc;n</p></body></html>", subtype="html")
    assert parse(msg.as_bytes()).event.body == "Bore da & croeso Siân"


def test_plain_text_preferred_over_html():
    msg = original()
    msg.add_alternative("<p>HTML</p>", subtype="html")
    assert parse(msg.as_bytes()).event.body.startswith("Bore da")


def test_attachments_are_hashed_and_returned_with_their_bytes():
    msg = original()
    msg.add_attachment(PACK, maintype="application", subtype="pdf", filename="Pecyn bwrdd – Mawrth.pdf")
    msg.add_attachment("a,b\n1,2\n", subtype="csv", filename="rhifau.csv")
    parsed = parse(msg.as_bytes())
    (pdf, pdf_bytes), (csv, csv_bytes) = parsed.attachments
    assert pdf_bytes == PACK
    assert (pdf.filename, pdf.content_type, pdf.size) == ("Pecyn bwrdd – Mawrth.pdf", "application/pdf", len(PACK))
    assert pdf.sha256 == hashlib.sha256(PACK).hexdigest()
    assert (csv.filename, csv.content_type) == ("rhifau.csv", "text/csv")
    assert csv.sha256 == hashlib.sha256(csv_bytes).hexdigest()
    assert parsed.event.attachments == [pdf, csv]
    assert parsed.event.body.startswith("Bore da")


def test_same_bytes_in_two_messages_give_the_same_hash():
    first, second = original(), original(**{"Message-ID": "<m2@cyberscallywags.example>", "Subject": "Fwd: gwyliau"})
    first.add_attachment(PACK, maintype="application", subtype="pdf", filename="pack.pdf")
    second.add_attachment(PACK, maintype="application", subtype="octet-stream", filename="holiday-photos.zip")
    a, b = parse(report(first, ENVELOPE)), parse(second.as_bytes())
    assert a.event.attachments[0].sha256 == b.event.attachments[0].sha256
    assert a.raw_sha256 != b.raw_sha256


def test_raw_sha256_is_of_the_bytes_received():
    raw = original().as_bytes()
    assert parse(raw).raw_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (original(**{"Message-ID": ""}).as_bytes(), "no Message-ID"),
        (original(**{"From": "Mallory <mallory@example.org>"}).as_bytes(), "journal only carries mail sent from"),
        (original(To="", Cc="").as_bytes(), "at least one recipient"),
        (original(Date="").as_bytes(), "no Date"),
        (original(**{"Message-ID": "not-an-id"}).as_bytes(), "message_id"),
        (original(To="not an address").as_bytes(), "to.address: not an email address"),
        (bytes(range(256)) * 20, "no Message-ID"),
        (b"", "empty"),
        (b"  \r\n", "empty"),
    ],
)
def test_malformed_input_raises_parse_error(raw, reason):
    with pytest.raises(ParseError, match=reason) as exc:
        parse(raw)
    assert len(exc.value.reason) <= 500


def test_parse_error_reason_never_quotes_the_message():
    msg = original(**{"From": "Secret Sender <secret.sender@example.org>", "Message-ID": "<SECRETID>"})
    msg.set_content("cyfrinach fawr")
    with pytest.raises(ParseError) as exc:
        parse(msg.as_bytes())
    for leaked in ("secret", "SECRETID", "cyfrinach"):
        assert leaked not in exc.value.reason
