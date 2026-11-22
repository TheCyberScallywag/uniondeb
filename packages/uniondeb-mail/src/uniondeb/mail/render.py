"""The inverse of the parsers: turn an ``EmailEvent`` back into the mail a journal would deliver.

Tests, evals and demos need real ``.eml`` bytes to push through Cennad, and the synthetic
generator only produces events. Three renderers mirror the three journal shapes:

* ``to_journal_report``: a Microsoft 365 envelope journal report with the original attached
* ``to_gws_copy``: a Google Workspace BCC copy carrying ``X-Gm-Original-To`` headers
* ``to_eml``: the bare original, which is also what a plain BCC rule delivers

**Attachment bytes.** An event holds attachment metadata only. Pass ``attachment_bytes`` (SHA-256 →
bytes) to attach the real content; a round trip then reproduces the declared hash. Without it,
each attachment gets deterministic filler derived from its declared hash (same declared hash →
same filler, so forwards still link by hash), at most ``FILLER_MAX`` bytes. A round trip then
yields the *filler's* hash and size, not the declared ones.

What survives render → parse: message_id, sender, to, cc, bcc (journal report only), subject,
body text (minus trailing newlines), sent_at to the second, in_reply_to, attachment filenames and
the journal source. ``device`` and ``journal.received_at`` are not part of a message on the wire.

Implements: UND-025
"""

import hashlib
from collections.abc import Mapping
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage

from uniondeb.core.mail import COMPANY_DOMAIN, EmailAddress, EmailEvent, JournalSource

from .parse import GWS_ORIGINAL_TO, JOURNAL_REPORT_HEADER

FILLER_MAX = 64 * 1024
JOURNAL_FROM = f"MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@{COMPANY_DOMAIN}"
JOURNAL_TO = f"cennad@{COMPANY_DOMAIN}"

type AttachmentBytes = Mapping[str, bytes] | None


def filler(sha256: str, size: int) -> bytes:
    """Deterministic stand-in content for an attachment whose real bytes are not to hand."""
    size, out, n = min(size, FILLER_MAX), bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(f"{sha256}:{n}".encode()).digest()
        n += 1
    return bytes(out[:size])


def _address(a: EmailAddress) -> Address:
    return Address(display_name=a.name or "", addr_spec=a.address)


def _original(event: EmailEvent, attachment_bytes: AttachmentBytes) -> EmailMessage:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = _address(event.sender)
    if event.to:
        msg["To"] = [_address(a) for a in event.to]
    if event.cc:
        msg["Cc"] = [_address(a) for a in event.cc]
    msg["Subject"] = event.subject
    msg["Date"] = event.sent_at
    msg["Message-ID"] = event.message_id
    if event.in_reply_to:
        msg["In-Reply-To"] = event.in_reply_to
    msg.set_content(event.body or "")
    for att in event.attachments:
        data = (attachment_bytes or {}).get(att.sha256)
        maintype, _, subtype = att.content_type.partition("/")
        if maintype in ("message", "multipart") or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data if data is not None else filler(att.sha256, att.size),
            maintype=maintype,
            subtype=subtype,
            filename=att.filename,
        )
    return msg


def to_eml(event: EmailEvent, attachment_bytes: AttachmentBytes = None) -> bytes:
    """The original message as sent. BCC recipients are not in its headers, as in real mail."""
    return _original(event, attachment_bytes).as_bytes()


def to_gws_copy(event: EmailEvent, attachment_bytes: AttachmentBytes = None) -> bytes:
    """A Google Workspace routing copy: the original plus one ``X-Gm-Original-To`` per envelope recipient."""
    msg = _original(event, attachment_bytes)
    for a in event.journal.envelope_recipients or event.all_recipients:
        msg[GWS_ORIGINAL_TO] = a.address
    return msg.as_bytes()


def to_journal_report(event: EmailEvent, attachment_bytes: AttachmentBytes = None) -> bytes:
    """A Microsoft 365 envelope journal report: P1 envelope in the body, the original attached."""
    lines = [f"Sender: {event.sender.address}", f"Subject: {event.subject}", f"Message-Id: {event.message_id}"]
    for label, group in (("To", event.to), ("Cc", event.cc), ("Bcc", event.bcc)):
        lines += [f"{label}: {a.address}" for a in group]
    listed = {a.address for a in (*event.to, *event.cc, *event.bcc)}
    lines += [f"Recipient: {a.address}" for a in event.journal.envelope_recipients if a.address not in listed]

    report = EmailMessage(policy=policy.SMTP)
    report["From"] = JOURNAL_FROM
    report["To"] = JOURNAL_TO
    report["Subject"] = event.subject
    report["Date"] = event.journal.received_at
    report["Message-ID"] = f"<journal-{hashlib.sha256(event.message_id.encode()).hexdigest()[:32]}@{COMPANY_DOMAIN}>"
    report[JOURNAL_REPORT_HEADER] = ""
    report.set_content("\n".join(lines) + "\n")
    report.add_attachment(_original(event, attachment_bytes))
    return report.as_bytes()


def render(event: EmailEvent, attachment_bytes: AttachmentBytes = None) -> bytes:
    """Render in the shape of the event's own ``journal.source``."""
    return {
        JournalSource.M365_ENVELOPE: to_journal_report,
        JournalSource.GWS_BCC: to_gws_copy,
        JournalSource.BCC_RULE: to_eml,
    }[JournalSource(event.journal.source)](event, attachment_bytes)
