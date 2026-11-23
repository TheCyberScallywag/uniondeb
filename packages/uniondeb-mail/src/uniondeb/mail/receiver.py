"""Cennad ("messenger"): the receiver every journaled email is copied to.

``Cennad.handle`` is the whole pipeline for one received message and needs no SMTP:

    store raw by SHA-256 → parse → store attachments by SHA-256 → publish canonical JSON

The raw message is stored *first*, so whatever happens next the evidence is held and its hash is
the provenance anchor. A message that cannot be parsed or validated goes to the dead-letter topic
as ``{raw_sha256, error, peer_ip, received_at}``: a pointer and a reason, never content. Every
message ends in exactly one of topic or dead-letter topic. If storing or publishing itself fails,
``handle`` raises, the SMTP layer answers 451, and the sending MTA retries; the retry is harmless
because storage is content-addressed and the stream is keyed by Message-ID.

The SMTP layer (aiosmtpd) adds what a journaling endpoint on the internet needs: a sender-IP
allowlist that rejects everyone when empty, a message size limit, and STARTTLS required unless
plaintext is explicitly allowed.
"""

import asyncio
import ipaddress
import json
import logging
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aiosmtpd.controller import Controller

from uniondeb.core.mail import EmailEvent

from .parse import ParseError, parse, sha256_hex
from .publish import KafkaPublisher, NdjsonPublisher, Publisher
from .settings import CennadSettings, Network
from .storage import LocalObjectStore, ObjectStore

log = logging.getLogger("uniondeb.cennad")


@dataclass(frozen=True)
class Outcome:
    status: Literal["published", "dead_lettered"]
    raw_sha256: str
    raw_new: bool  # False when this exact copy was already stored
    key: str | None = None  # the Message-ID the document was published under
    error: str | None = None  # the dead-letter reason
    attachments_stored: int = 0  # attachments new to the store
    event: EmailEvent | None = None

    @property
    def published(self) -> bool:
        return self.status == "published"


class Cennad:
    def __init__(self, store: ObjectStore, publisher: Publisher, *, synthetic: bool = False) -> None:
        self.store, self.publisher, self.synthetic = store, publisher, synthetic

    def handle(self, raw: bytes, *, peer_ip: str | None, received_at: datetime | None = None) -> Outcome:
        received_at = received_at or datetime.now(UTC)
        raw_sha256 = sha256_hex(raw)
        raw_new = self.store.put(raw_sha256, raw, kind="raw")
        common = {"raw_sha256": raw_sha256, "synthetic": str(self.synthetic).lower()}
        try:
            parsed = parse(raw, received_at=received_at)
        except ParseError as exc:
            doc = {
                "raw_sha256": raw_sha256,
                "error": exc.reason,
                "peer_ip": peer_ip,
                "received_at": received_at.isoformat(),
            }
            self.publisher.dead_letter(doc, exc.reason, {**common, "normalised": "false"})
            return Outcome("dead_lettered", raw_sha256, raw_new, error=exc.reason)
        stored = sum(self.store.put(a.sha256, data, kind="attachments") for a, data in parsed.attachments)
        event = parsed.event
        headers = {"journal_source": str(event.journal.source), "direction": str(event.direction), "normalised": "true"}
        self.publisher.send(event.message_id, json.loads(event.model_dump_json()), {**headers, **common})
        return Outcome("published", raw_sha256, raw_new, key=event.message_id, attachments_stored=stored, event=event)


# ───────────────────────────── SMTP ─────────────────────────────────────────────────
def _networks(allow: Iterable[str | Network]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(n, strict=False) if isinstance(n, str) else n for n in allow)


class JournalHandler:
    """aiosmtpd handler: allowlist at MAIL, the pipeline at DATA, 250 only once the raw is stored."""

    def __init__(self, cennad: Cennad, allow: Iterable[str | Network], *, max_bytes: int) -> None:
        self.cennad, self.allow, self.max_bytes = cennad, _networks(allow), max_bytes

    def _peer_ip(self, session: Any) -> str | None:
        peer = session.peer
        return str(peer[0]) if isinstance(peer, (tuple, list)) and peer else None

    def allowed(self, peer_ip: str | None) -> bool:
        try:
            ip = ipaddress.ip_address(peer_ip or "")
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return any(ip in net for net in self.allow)

    async def handle_MAIL(self, server: Any, session: Any, envelope: Any, address: str, mail_options: list[str]) -> str:
        if not self.allowed(peer := self._peer_ip(session)):
            log.warning("rejected peer %s: not in the allowlist", peer)
            return "550 5.7.1 Sender address not permitted"
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        peer = self._peer_ip(session)
        if not self.allowed(peer):
            return "550 5.7.1 Sender address not permitted"
        raw = envelope.content if isinstance(envelope.content, bytes) else str(envelope.content or "").encode()
        if len(raw) > self.max_bytes:
            return "552 5.3.4 Message exceeds the size limit"
        try:
            outcome = await asyncio.to_thread(self.cennad.handle, raw, peer_ip=peer)
        except Exception as exc:  # storage or broker failure: the sending MTA must retry
            log.error("could not accept message from %s: %s", peer, type(exc).__name__)
            return "451 4.3.0 Temporary failure, try again later"
        log.info("%s %s from %s", outcome.status, outcome.raw_sha256, peer)
        return f"250 2.0.0 Accepted {outcome.raw_sha256}"


def tls_context(cert: str | Path, key: str | Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def make_controller(
    cennad: Cennad,
    *,
    host: str = "127.0.0.1",
    port: int = 8025,
    allow: Iterable[str | Network] = (),
    max_bytes: int = 50 * 1024 * 1024,
    tls: ssl.SSLContext | None = None,
    allow_plaintext: bool = False,
) -> Controller:
    """An unstarted aiosmtpd controller. Without a TLS context it refuses unless plaintext is explicit."""
    if tls is None and not allow_plaintext:
        raise ValueError("Cennad needs a TLS certificate and key (or allow_plaintext=True for tests)")
    return Controller(
        JournalHandler(cennad, allow, max_bytes=max_bytes),
        hostname=host,
        port=port,
        data_size_limit=max_bytes,
        tls_context=tls,
        require_starttls=tls is not None and not allow_plaintext,
    )


def from_settings(settings: CennadSettings, publisher: Publisher | None = None) -> Controller:
    """Wire store, publisher and SMTP controller from settings. Kafka when configured, else NDJSON on stdout."""
    if (settings.tls_cert is None) != (settings.tls_key is None):
        raise ValueError("set both CENNAD_TLS_CERT and CENNAD_TLS_KEY, or neither")
    tls = tls_context(settings.tls_cert, settings.tls_key) if settings.tls_cert and settings.tls_key else None
    if publisher is None:
        publisher = KafkaPublisher(settings.kafka) if settings.kafka else NdjsonPublisher()
    cennad = Cennad(LocalObjectStore(settings.store), publisher, synthetic=settings.synthetic)
    return make_controller(
        cennad,
        host=settings.host,
        port=settings.port,
        allow=settings.networks,
        max_bytes=settings.max_bytes,
        tls=tls,
        allow_plaintext=settings.allow_plaintext,
    )
