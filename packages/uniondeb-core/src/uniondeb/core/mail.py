"""Email captured by journaling: every message sent from CyberScallywags UK accounts and equipment
is blind-copied (Google Workspace routing) or envelope-journaled (Microsoft 365) to the platform,
normalised to this JSON contract and streamed.

The contract accepts the shapes real sources produce, through the same alias layer as the
other datasets:

* RFC 5322 strings: ``"Siân Jones <sian.jones@cyberscallywags.example>"``, address lists split
  with ``getaddresses`` (so ``"Jones, Siân" <…>`` survives)
* Microsoft Graph JSON: ``internetMessageId``, ``from.emailAddress``, ``toRecipients``,
  ``bccRecipients``, ``body.contentType/content`` (HTML is reduced to text), ``sentDateTime``
* plain dicts: ``{"email": …, "display_name": …}``

All domains are reserved for documentation (RFC 2606): the company is
``cyberscallywags.example``, external contacts use ``example.org`` / ``example.net``, and
personal webmail uses ``mail.example`` and ``post.example``.
"""

import html
import re
from datetime import datetime
from email.utils import getaddresses, parseaddr
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BeforeValidator, computed_field, model_validator

from .models import _CONFIG, Device, _Event, known_as
from .resolve import Resolving, one_of

COMPANY_DOMAIN = "cyberscallywags.example"
PERSONAL_DOMAINS = frozenset({"mail.example", "post.example"})
_ADDR = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class Direction(StrEnum):
    INTERNAL = "internal"  # every recipient is in the company domain
    OUTBOUND = "outbound"  # at least one external recipient
    TO_PERSONAL = "to_personal"  # at least one recipient at a personal webmail domain


class JournalSource(StrEnum):
    M365_ENVELOPE = "m365_envelope"  # Exchange Online journal report: envelope incl. BCC recipients
    GWS_BCC = "gws_bcc"  # Google Workspace routing "Add more recipients" (BCC-style only)
    BCC_RULE = "bcc_rule"  # a plain transport rule BCC: envelope recipients are NOT preserved


JOURNAL_SYNONYMS = {
    JournalSource.M365_ENVELOPE: ("envelope", "exchange", "m365", "microsoft 365", "journal report", "eej"),
    JournalSource.GWS_BCC: ("gmail", "google", "google workspace", "gws", "workspace routing"),
    JournalSource.BCC_RULE: ("bcc", "transport rule", "mail flow rule"),
}


# ───────────────────────────── addresses ────────────────────────────────────────────
def _coerce_address(v: Any) -> Any:
    if isinstance(v, str):
        name, addr = parseaddr(v)
        return {"address": addr or v, "name": name or None}
    if isinstance(v, dict) and isinstance(v.get("emailAddress"), dict):  # Microsoft Graph recipient
        return v["emailAddress"]
    return v


def _coerce_address_list(v: Any) -> Any:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [{"address": a, "name": n or None} for n, a in getaddresses([v.replace(";", ",")]) if a]
    if isinstance(v, (list, tuple)):
        return [_coerce_address(x) for x in v]
    return [_coerce_address(v)]


def _addr(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip().lower()
        if not _ADDR.match(v):
            raise ValueError(f"not an email address: {v!r}")
    return v


class EmailAddress(Resolving):
    model_config = _CONFIG

    address: Annotated[str, BeforeValidator(_addr), known_as("address")]
    name: Annotated[str | None, known_as("name")] = None

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[1]


type Addr = Annotated[EmailAddress, BeforeValidator(_coerce_address)]
type AddrList = Annotated[list[Addr], BeforeValidator(_coerce_address_list)]


# ───────────────────────────── attachments & journal ────────────────────────────────
class Attachment(Resolving):
    """Metadata only; the bytes live in object storage, addressed by SHA-256.
    Identical hashes across messages reveal forwards, which is how a board pack sent to a
    personal account is caught even when the subject line changes."""

    model_config = _CONFIG

    filename: Annotated[str, known_as("filename")]
    content_type: Annotated[str, known_as("content_type")] = "application/octet-stream"
    size: Annotated[int, known_as("size", ge=0)]
    sha256: Annotated[str, known_as("sha256", pattern=r"^[0-9a-f]{64}$")]


class Journal(Resolving):
    model_config = _CONFIG

    source: Annotated[JournalSource, one_of(JournalSource, JOURNAL_SYNONYMS), known_as("source")]
    received_at: Annotated[datetime, known_as("received_at")]
    envelope_recipients: Annotated[AddrList, known_as("envelope_recipients")] = []


# ───────────────────────────── the email event ──────────────────────────────────────
_TAGS = re.compile(r"<[^>]+>")


def _coerce_body(v: Any) -> Any:
    """Graph ``{"contentType": "html", "content": "<p>…</p>"}`` → plain text."""
    if isinstance(v, dict):
        content = v.get("content") or v.get("text") or ""
        kind = str(v.get("contentType") or v.get("content_type") or "text").lower()
        if kind == "html":
            content = html.unescape(_TAGS.sub(" ", content))
            content = re.sub(r"[ \t]+", " ", content).strip()
        return content
    return v


class EmailEvent(_Event):
    message_id: Annotated[str, known_as("message_id", pattern=r"^<[^<>@\s]+@[^<>@\s]+>$")]
    sender: Annotated[Addr, known_as("sender")]
    to: Annotated[AddrList, known_as("to")] = []
    cc: Annotated[AddrList, known_as("cc")] = []
    bcc: Annotated[AddrList, known_as("bcc")] = []
    subject: Annotated[str, known_as("subject", max_length=998)] = ""
    body: Annotated[str | None, BeforeValidator(_coerce_body), known_as("body", max_length=200_000)] = None
    sent_at: Annotated[datetime, known_as("sent_at")]
    in_reply_to: Annotated[str | None, known_as("in_reply_to")] = None
    attachments: Annotated[list[Attachment], known_as("attachments")] = []
    device: Annotated[Device | None, known_as("device")] = None  # company equipment it was sent from, if known
    journal: Annotated[Journal, known_as("journal")]

    @computed_field(description="internal | outbound | to_personal, derived from recipient domains")
    @property
    def direction(self) -> Direction:
        domains = {a.domain for a in self.all_recipients}
        if domains & PERSONAL_DOMAINS:
            return Direction.TO_PERSONAL
        return Direction.INTERNAL if domains <= {COMPANY_DOMAIN} else Direction.OUTBOUND

    @property
    def all_recipients(self) -> list[EmailAddress]:
        seen: dict[str, EmailAddress] = {}
        for a in (*self.to, *self.cc, *self.bcc, *self.journal.envelope_recipients):
            seen.setdefault(a.address, a)
        return list(seen.values())

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.sender.domain != COMPANY_DOMAIN:
            raise ValueError(f"journal only carries mail sent from {COMPANY_DOMAIN} accounts")
        if not self.all_recipients:
            raise ValueError("an email needs at least one recipient")
        if self.device is not None and self.device.type == "landline":
            raise ValueError("email cannot be sent from a landline")
        return self

    def kafka_key(self) -> str:
        return self.message_id
