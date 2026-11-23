"""Hash-chained, append-only audit log (design §7.4).

Every audited action, reads included, appends one ``(:AuditEvent)``. An event stores the hash of the
event before it and its own hash over every other field, so editing, deleting, reordering or
relinking anything already written breaks the chain at a point ``verify`` can name. What a chain
cannot show by itself is a rewritten or truncated tail, so ``anchor`` writes the head's seq and
hash, signed with a key the database never sees, to a file outside it; ``check_anchor`` fails when
the chain no longer contains that event.

This module has an append and reads, and nothing else: there is no update or delete path, and a
correction is a new event. The append is one Cypher statement, so an event never exists without
its ``PREV`` link. Writers compute ``seq`` from the head they just read; the unique ``seq``
constraint makes the loser of a race fail, and it retries against the new head.
"""

import asyncio
import hashlib
import hmac
import json
import random
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from uniondeb.cases.models import AuditChainError, now_iso
from uniondeb.store.runner import CypherRunner

GENESIS = "0" * 64
HASHED = ("id", "seq", "at", "actor", "action", "case_id", "target", "reason", "detail_json", "prev_hash")
RETRIES = 8
PAGE = 2000

APPEND = (
    "OPTIONAL MATCH (prev:AuditEvent {seq: $prev_seq}) "
    "OPTIONAL MATCH (c:Case {id: $case_id}) "
    "CREATE (e:AuditEvent {id: $id, seq: $seq, at: $at, actor: $actor, action: $action, case_id: $case_id, "
    "target: $target, reason: $reason, detail_json: $detail_json, prev_hash: $prev_hash, hash: $hash}) "
    "FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (e)-[:PREV]->(prev)) "
    "FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | CREATE (e)-[:ABOUT]->(c)) "
    "RETURN e.seq AS seq"
)


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def event_hash(fields: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: fields.get(k) for k in HASHED})).hexdigest()


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    seq: int
    at: str
    actor: str
    action: str
    case_id: str | None = None
    target: str | None = None
    reason: str | None = None
    detail_json: str = "{}"
    prev_hash: str
    hash: str

    @property
    def detail(self) -> dict[str, Any]:
        return json.loads(self.detail_json)


class VerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    checked: int = 0
    head_seq: int = 0
    head_hash: str = GENESIS
    broken_at: int | None = None
    reason: str | None = None


class Anchor(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    hash: str
    at: str
    hmac: str


class AnchorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    seq: int | None = None
    hash: str | None = None
    reason: str | None = None


def _sign(key: bytes, seq: int, digest: str, at: str) -> str:
    return hmac.new(key, _canonical({"seq": seq, "hash": digest, "at": at}), hashlib.sha256).hexdigest()


class AuditLog:
    """Share one instance per process: its lock is what keeps local writers from racing each other."""

    def __init__(self, runner: CypherRunner) -> None:
        self.runner = runner
        self._lock = asyncio.Lock()

    async def append(
        self,
        actor: str,
        action: str,
        *,
        case_id: str | None = None,
        target: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        base = {
            "id": str(uuid.uuid7()),
            "actor": actor,
            "action": action,
            "case_id": case_id,
            "target": target,
            "reason": reason,
            "detail_json": json.dumps(detail or {}, sort_keys=True, default=str),
        }
        async with self._lock:
            for attempt in range(RETRIES):
                if attempt:
                    await asyncio.sleep(random.uniform(0, 0.01 * attempt))
                head = await self.head()
                prev_seq, prev_hash = (head.seq, head.hash) if head else (0, GENESIS)
                fields = base | {"seq": prev_seq + 1, "at": now_iso(), "prev_hash": prev_hash}
                event = AuditEvent(**fields, hash=event_hash(fields))
                try:
                    await self.runner.run(APPEND, event.model_dump() | {"prev_seq": prev_seq})
                except Exception:
                    # Lost the seq to a writer in another process? Then the head has moved: go again.
                    moved = await self.head()
                    if moved is None or moved.seq <= prev_seq:
                        raise
                    continue
                return event
        raise AuditChainError(f"could not append {action!r}: lost the head {RETRIES} times")

    async def head(self) -> AuditEvent | None:
        rows = await self.runner.run("MATCH (e:AuditEvent) RETURN e {.*} AS e ORDER BY e.seq DESC LIMIT 1")
        return AuditEvent.model_validate(rows[0]["e"]) if rows else None

    async def events(
        self, *, case_id: str | None = None, actor: str | None = None, limit: int = 200
    ) -> list[AuditEvent]:
        """Newest first."""
        rows = await self.runner.run(
            "MATCH (e:AuditEvent) WHERE ($case_id IS NULL OR e.case_id = $case_id) "
            "AND ($actor IS NULL OR e.actor = $actor) RETURN e {.*} AS e ORDER BY e.seq DESC LIMIT $limit",
            {"case_id": case_id, "actor": actor, "limit": limit},
        )
        return [AuditEvent.model_validate(r["e"]) for r in rows]

    async def verify(self, *, from_seq: int = 1) -> VerifyResult:
        """Recompute every hash and link from ``from_seq`` to the head; stop at the first break."""
        from_seq = max(from_seq, 1)
        rows = await self.runner.run("MATCH (e:AuditEvent) RETURN max(e.seq) AS seq")
        top = (rows[0]["seq"] if rows else None) or 0
        expected_prev, checked = GENESIS, 0

        def broken(seq: int, reason: str) -> VerifyResult:
            return VerifyResult(ok=False, checked=checked, head_seq=top, broken_at=seq, reason=reason)

        if from_seq > 1:
            before = await self._page(from_seq - 1, from_seq)
            if not before or "hash" not in before[0]["e"]:
                return broken(from_seq - 1, f"event {from_seq - 1} is missing")
            expected_prev = before[0]["e"]["hash"]
        expected_seq = from_seq
        for lo in range(from_seq, top + 1, PAGE):
            for row in await self._page(lo, lo + PAGE):
                raw = row["e"]
                if raw.get("seq") != expected_seq:
                    return broken(expected_seq, f"event {expected_seq} is missing (next found: {raw.get('seq')})")
                try:
                    event = AuditEvent.model_validate(raw)
                except ValidationError:
                    return broken(expected_seq, "event is malformed: a field was removed or retyped")
                if event.hash != event_hash(raw):
                    return broken(expected_seq, "hash does not match the event's fields: it was edited")
                if event.prev_hash != expected_prev:
                    return broken(expected_seq, "prev_hash does not match the event before it: relinked or forged")
                if row["prev"] != ([expected_seq - 1] if expected_seq > 1 else []):
                    return broken(expected_seq, f"PREV link points at {row['prev']}, not the event before it")
                expected_prev, expected_seq, checked = event.hash, expected_seq + 1, checked + 1
        if expected_seq <= top:
            return broken(expected_seq, f"event {expected_seq} is missing")
        return VerifyResult(ok=True, checked=checked, head_seq=top, head_hash=expected_prev if top else GENESIS)

    async def _page(self, lo: int, hi: int) -> list[dict[str, Any]]:
        return await self.runner.run(
            "MATCH (e:AuditEvent) WHERE e.seq >= $lo AND e.seq < $hi OPTIONAL MATCH (e)-[:PREV]->(p:AuditEvent) "
            "RETURN e {.*} AS e, collect(p.seq) AS prev ORDER BY e.seq",
            {"lo": lo, "hi": hi},
        )

    async def anchor(self, path: str | Path, key: bytes) -> Anchor:
        """Write the head's seq and hash, signed with ``key``, to a file outside the database."""
        head = await self.head()
        seq, digest, at = (head.seq, head.hash, now_iso()) if head else (0, GENESIS, now_iso())
        anchor = Anchor(seq=seq, hash=digest, at=at, hmac=_sign(key, seq, digest, at))
        Path(path).write_text(anchor.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return anchor

    async def check_anchor(self, path: str | Path, key: bytes) -> AnchorCheck:
        """Is the anchored event still in the chain, unchanged? (Run ``verify`` as well.)"""
        try:
            anchor = Anchor.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            return AnchorCheck(ok=False, reason=f"anchor unreadable: {type(exc).__name__}")
        if not hmac.compare_digest(anchor.hmac, _sign(key, anchor.seq, anchor.hash, anchor.at)):
            return AnchorCheck(ok=False, seq=anchor.seq, hash=anchor.hash, reason="anchor signature is invalid")
        if anchor.seq == 0:
            return AnchorCheck(ok=True, seq=0, hash=anchor.hash)
        rows = await self.runner.run("MATCH (e:AuditEvent {seq: $seq}) RETURN e.hash AS hash", {"seq": anchor.seq})
        if not rows:
            reason = f"the chain no longer contains event {anchor.seq}"
        elif rows[0]["hash"] != anchor.hash:
            reason = f"event {anchor.seq} no longer has the anchored hash"
        else:
            return AnchorCheck(ok=True, seq=anchor.seq, hash=anchor.hash)
        return AnchorCheck(ok=False, seq=anchor.seq, hash=anchor.hash, reason=reason)
