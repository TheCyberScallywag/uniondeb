"""Porthor's streaming side: consume ``mail-journal`` and its dead-letter topic (design §5.2).

One message in, one outcome. A journal copy is keyed by Message-ID, so a second copy of the same
message (two journal rules, a redelivery) links its raw ``.eml`` hash to the record that already
exists and writes nothing else. The raw message's SHA-256 is the ``SourceFile`` for provenance.
Anything Cennad dead-lettered, and anything that no longer validates here, becomes a review item.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError

from uniondeb.agents.porthor.pipeline import PORTHOR, _content_hash, _reason
from uniondeb.core.mail import EmailEvent
from uniondeb.core.prov import ActivityKind, EntityRef
from uniondeb.core.schemas import SCHEMA_VERSION
from uniondeb.store import review
from uniondeb.store.load import to_graph_rows, write_batch
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner

TOPIC, DLQ_TOPIC = "mail-journal", "mail-journal-dlq"
type Outcome = Literal["ingested", "duplicate", "review"]


async def _source(runner: CypherRunner, sha: str, headers: Mapping[str, str], state: str) -> None:
    await runner.run(
        "MERGE (f:SourceFile {sha256: $sha}) ON CREATE SET f.kind = 'eml', f.dataset = 'email', f.format = 'eml', "
        "f.received_at = $now, f.journal_source = $source, f.synthetic = $synthetic, f.state = $state",
        {
            "sha": sha,
            "now": datetime.now(UTC).isoformat(),
            "source": headers.get("journal_source"),
            "synthetic": str(headers.get("synthetic", "false")).lower() == "true",
            "state": state,
        },
    )


async def _to_review(runner: CypherRunner, sha: str, reason: str) -> str | None:
    ref = EntityRef(label="SourceFile", key=sha)
    open_items = await runner.run(
        "MATCH (i:ReviewItem {kind: 'mail_dlq'})-[:ABOUT]->(:SourceFile {sha256: $sha}) WHERE i.state IN $open "
        "RETURN i.id AS id",
        {"sha": sha, "open": list(review.OPEN_STATES)},
    )
    if open_items:
        return None
    return await review.create_item(
        runner,
        "mail_dlq",
        created_by=PORTHOR.name,
        about=ref,
        summary=f"journaled mail could not be normalised: {reason}",
        payload={"file": sha, "reason": reason},
    )


async def consume_mail(
    runner: CypherRunner, tarddiad: Tarddiad, doc: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> Outcome:
    headers = headers or {}
    sha = headers.get("raw_sha256") or hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    try:
        event = EmailEvent.model_validate(dict(doc))
        g = to_graph_rows("email", event)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        await _source(runner, sha, headers, "dead_letter")
        await _to_review(runner, sha, _reason(exc))
        return "review"
    g["props"]["content_sha256"] = _content_hash(event)
    await _source(runner, sha, headers, "ingested")
    link = "MATCH (f:SourceFile {sha256: $sha}) MATCH (r:Record {key: $key}) MERGE (f)-[:CONTAINED]->(r) RETURN r.key AS key"
    if await runner.run(link, {"sha": sha, "key": g["key"]}):
        return "duplicate"
    async with tarddiad.activity(
        ActivityKind.INGEST, agent=PORTHOR, trigger=f"{TOPIC}:{sha[:16]}", config_versions={"schema": SCHEMA_VERSION}
    ) as act:
        act.used(EntityRef(label="SourceFile", key=sha))
        await write_batch(runner, [g], act.id)
        await runner.run(link, {"sha": sha, "key": g["key"]})
        act.generated(EntityRef(label="Record", key=g["key"]))
    return "ingested"


async def consume_dead_letter(
    runner: CypherRunner, doc: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> str | None:
    """A dead-lettered message becomes one open review item (the id), or ``None`` if it already has one."""
    sha = doc.get("raw_sha256") or (headers or {}).get("raw_sha256")
    if not sha:
        sha = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    await _source(runner, sha, headers or {}, "dead_letter")
    return await _to_review(runner, sha, str(doc.get("error") or "dead-lettered")[:300])


async def run_consumer(
    bootstrap: str,
    on_mail: Callable[[dict[str, Any], dict[str, str]], Awaitable[Any]],
    on_dead_letter: Callable[[dict[str, Any], dict[str, str]], Awaitable[Any]],
    *,
    group: str = "porthor",
    stop: Callable[[], bool] = lambda: False,
) -> None:  # pragma: no cover - needs a broker
    """Consume both topics until ``stop()``. Offsets are committed only after a message is handled,
    and handling is idempotent, so a crash replays rather than loses."""
    import asyncio

    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([TOPIC, DLQ_TOPIC])
    try:
        while not stop():
            msg = await asyncio.to_thread(consumer.poll, 1.0)
            if msg is None or msg.error():
                continue
            headers = {k: v.decode() for k, v in (msg.headers() or [])}
            doc = json.loads(msg.value())
            await (on_dead_letter if msg.topic() == DLQ_TOPIC else on_mail)(doc, headers)
            await asyncio.to_thread(consumer.commit, msg)
    finally:
        consumer.close()
