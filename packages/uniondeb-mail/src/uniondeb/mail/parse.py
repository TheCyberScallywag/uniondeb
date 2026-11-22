"""Journal parsers: raw ``.eml`` bytes in, one validated ``EmailEvent`` out.

Three shapes arrive at Cennad, and which one captured a message decides how much of the
envelope an analyst can trust:

* **Microsoft 365 envelope journal report** (``m365_envelope``): an outer report whose text lists
  the P1 envelope (``Sender:``, ``Message-Id:``, one ``To:``/``Cc:``/``Bcc:``/``Recipient:`` line per
  recipient, optionally ``, Expanded: list@…`` or ``, Forwarded: …``) and whose attachment is the
  original message, unaltered. This is the only shape that reveals BCC recipients.
* **Google Workspace BCC copy** (``gws_bcc``): the original itself, with ``X-Gm-Original-To``
  headers recovering part of the envelope. Other BCC recipients stay invisible.
* **Anything else** (``bcc_rule``): a plain transport-rule copy with no envelope information.

Only the standard library ``email`` package is used. Every failure surfaces as one ``ParseError``
whose reason names the field and the rule that failed, never the message content, so the reason
is safe to put on the dead-letter topic. ``device`` is never set here: journaling works per
mailbox, and the sending device is attached later only when it can be shown.

Body text is given LF line endings, loses its trailing newlines (MIME cannot tell ``"a"`` from
``"a\\n"``) and is cut at the
contract's 200 000 characters; the raw message in object storage stays complete.

Implements: UND-025
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

from pydantic import ValidationError

from uniondeb.core.mail import Attachment, EmailEvent, JournalSource, _coerce_body

JOURNAL_REPORT_HEADER = "X-MS-Exchange-Organization-Journal-Report"
GWS_ORIGINAL_TO = "X-Gm-Original-To"
BODY_MAX = 200_000

_RECIPIENT = re.compile(r"^(Recipient|To|Cc|Bcc):\s*<?([^\s,<>]+)>?\s*(?:,\s*(?:Expanded|Forwarded):.*)?$", re.I)
_FIELD = re.compile(r"^(Sender|Message-Id):\s*(.+?)\s*$", re.I)


class ParseError(ValueError):
    """The message cannot become an ``EmailEvent``. ``reason`` is short and carries no content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Parsed:
    event: EmailEvent
    raw_sha256: str
    attachments: list[tuple[Attachment, bytes]]


@dataclass(frozen=True)
class _Envelope:
    sender: str | None
    message_id: str | None
    recipients: list[str]  # every recipient line, in order, de-duplicated
    bcc: list[str]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse(raw: bytes, *, received_at: datetime | None = None) -> Parsed:
    """Parse one journaled message. ``received_at`` is when Cennad accepted it (default: now, UTC)."""
    if not raw or not raw.strip():
        raise ParseError("empty message")
    try:
        return _parse(raw, received_at or datetime.now(UTC))
    except ParseError:
        raise
    except ValidationError as exc:
        raise ParseError(_validation_reason(exc)) from None
    except Exception as exc:  # the email package raises a wide variety on hostile input
        raise ParseError(f"unreadable message ({type(exc).__name__})") from None


def _parse(raw: bytes, received_at: datetime) -> Parsed:
    outer = message_from_bytes(raw, policy=policy.default)
    original, envelope = _journal_report(outer)
    if envelope is not None:
        source, recipients, bcc = JournalSource.M365_ENVELOPE, envelope.recipients, envelope.bcc
    elif gws := _addresses(original, GWS_ORIGINAL_TO):
        source, recipients, bcc = JournalSource.GWS_BCC, gws, []
    else:
        source, recipients, bcc = JournalSource.BCC_RULE, [], []

    message_id = _text(original, "Message-ID") or (envelope.message_id if envelope else None)
    if not message_id:
        raise ParseError("no Message-ID header")
    sender = _addresses(original, "From")[:1] or ([envelope.sender] if envelope and envelope.sender else [])
    if not sender:
        raise ParseError("no From header")
    attachments = [(_attachment(n, part, data), data) for n, (part, data) in enumerate(_attachment_parts(original), 1)]
    event = EmailEvent.model_validate(
        {
            "message_id": message_id,
            "sender": sender[0],
            "to": _addresses(original, "To"),
            "cc": _addresses(original, "Cc"),
            "bcc": bcc,
            "subject": " ".join((_text(original, "Subject") or "").split()),
            "body": _body(original),
            "sent_at": _sent_at(original),
            "in_reply_to": _text(original, "In-Reply-To"),
            "attachments": [a for a, _ in attachments],
            "journal": {"source": source, "received_at": received_at, "envelope_recipients": recipients},
        }
    )
    return Parsed(event=event, raw_sha256=sha256_hex(raw), attachments=attachments)


# ───────────────────────────── Microsoft 365 journal report ─────────────────────────
def _journal_report(outer: EmailMessage) -> tuple[EmailMessage, _Envelope | None]:
    """The original message and its envelope, or ``(outer, None)`` when this is not a report."""
    flagged = outer[JOURNAL_REPORT_HEADER] is not None
    parts = list(outer.iter_parts()) if outer.is_multipart() else []
    originals = [p for p in parts if p.get_content_type() == "message/rfc822"]
    if not flagged and len(originals) != 1:
        return outer, None
    text = next((p for p in parts if p.get_content_type() == "text/plain"), None)
    envelope = _envelope(text.get_content() if text is not None else "")
    if not flagged and not (envelope.sender and envelope.recipients):
        return outer, None  # an ordinary message that happens to forward one email
    if not originals:
        raise ParseError("journal report without the original message attached")
    if not envelope.recipients:
        raise ParseError("journal report without envelope recipients")
    return originals[0].get_payload(0), envelope


def _envelope(text: str) -> _Envelope:
    fields: dict[str, str] = {}
    recipients: dict[str, None] = {}
    bcc: dict[str, None] = {}
    for line in text.splitlines():
        line = line.strip()
        if m := _RECIPIENT.match(line):
            recipients.setdefault(m[2].lower())
            if m[1].lower() == "bcc":
                bcc.setdefault(m[2].lower())
        elif m := _FIELD.match(line):
            fields.setdefault(m[1].lower(), m[2])
    return _Envelope(fields.get("sender"), fields.get("message-id"), list(recipients), list(bcc))


# ───────────────────────────── headers, body, attachments ───────────────────────────
def _text(msg: EmailMessage, name: str) -> str | None:
    value = msg[name]
    return str(value).strip() or None if value is not None else None


def _addresses(msg: EmailMessage, name: str) -> list[dict[str, Any]]:
    """Every address in every occurrence of a header (repeats and comma-separated lists alike)."""
    pairs = getaddresses([str(v) for v in msg.get_all(name, [])])
    return [{"address": addr, "name": display or None} for display, addr in pairs if addr]


def _sent_at(msg: EmailMessage) -> datetime:
    header = msg["Date"]
    if header is None:
        raise ParseError("no Date header")
    when = getattr(header, "datetime", None)
    if when is None:
        raise ParseError("unreadable Date header")
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _body(msg: EmailMessage) -> Any:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return None
    try:
        content = part.get_content()
    except LookupError, UnicodeError:  # unknown or lying charset: keep what can be read
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if not isinstance(content, str):
        return None
    if part.get_content_subtype() == "html":
        content = _coerce_body({"contentType": "html", "content": content})
    return content.replace("\r\n", "\n").rstrip("\n")[:BODY_MAX] or None


def _attachment_parts(msg: EmailMessage) -> Iterator[tuple[EmailMessage, bytes]]:
    """Attachment parts at any depth. An attached email is one attachment, not a place to recurse."""
    if msg.is_multipart() and msg.get_content_maintype() == "multipart":
        for part in msg.iter_parts():
            yield from _attachment_parts(part)
    elif msg.get_content_type() == "message/rfc822" and msg.is_multipart():
        yield msg, msg.get_payload(0).as_bytes()
    elif msg.get_content_disposition() == "attachment" or msg.get_filename():
        data = msg.get_payload(decode=True)
        yield msg, data if isinstance(data, bytes) else b""


def _attachment(n: int, part: EmailMessage, data: bytes) -> Attachment:
    default = f"attachment-{n}.eml" if part.get_content_type() == "message/rfc822" else f"attachment-{n}"
    return Attachment(
        filename=(part.get_filename() or "").strip() or default,
        content_type=part.get_content_type(),
        size=len(data),
        sha256=sha256_hex(data),
    )


def _validation_reason(exc: ValidationError) -> str:
    """Field and rule only: Pydantic's own message can quote the offending input."""
    reasons: dict[str, None] = {}
    for err in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(p) for p in err["loc"] if isinstance(p, str)) or "message"
        what = err["msg"].removeprefix("Value error, ").split(": ", 1)[0]
        reasons.setdefault(f"{where}: {what}" if where != "message" else what)
    return "; ".join(reasons)[:500]
