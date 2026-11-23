"""Tarddiad: record activities and walk lineage back to source-file hashes.

    async with tarddiad.activity(ActivityKind.INGEST, agent=PORTHOR, config_versions={...}) as act:
        act.used(EntityRef(label="SourceFile", key=sha))
        ...write records...
        act.generated(*record_refs)

The activity runs inside a Logfire span and stores its trace id. It is written when the block
exits, successful or not; a failed activity is stored with ``status: failed`` and keeps whatever it
used. Every referenced entity must already exist, so provenance never points at nothing.
"""

from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import logfire

from uniondeb.core.observe import current_trace_id
from uniondeb.core.prov import KEY_PROPERTIES, ROOT_LABELS, ActivityKind, ActivityRecord, AgentRef, EntityRef
from uniondeb.store.runner import CypherRunner


class ProvenanceError(RuntimeError):
    pass


class ActivityHandle:
    def __init__(self, record: ActivityRecord) -> None:
        self.record = record

    @property
    def id(self) -> str:
        return self.record.id

    def used(self, *refs: EntityRef) -> None:
        self.record.used.extend(refs)

    def generated(self, *refs: EntityRef) -> None:
        self.record.generated.extend(refs)


@dataclass
class LineageStep:
    entity: EntityRef
    activity: dict[str, Any]
    used: list[EntityRef] = field(default_factory=list)


@dataclass
class Lineage:
    entity: EntityRef
    steps: list[LineageStep]
    sources: list[EntityRef]

    @property
    def source_hashes(self) -> list[str]:
        return [s.key for s in self.sources if s.label in ROOT_LABELS]


def _group(refs: Iterable[EntityRef]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in refs:
        if r.key not in out[r.label]:
            out[r.label].append(r.key)
    return out


class Tarddiad:
    def __init__(self, runner: CypherRunner) -> None:
        self.runner = runner

    @asynccontextmanager
    async def activity(
        self,
        kind: ActivityKind,
        *,
        agent: AgentRef,
        model: str | None = None,
        prompt_version: str | None = None,
        config_versions: dict[str, str] | None = None,
        trigger: str | None = None,
    ) -> AsyncIterator[ActivityHandle]:
        record = ActivityRecord(
            kind=kind,
            agent=agent,
            model=model,
            prompt_version=prompt_version,
            config_versions=config_versions or {},
            trigger=trigger,
        )
        handle = ActivityHandle(record)
        with logfire.span("activity {kind} by {agent}", kind=kind.value, agent=agent.name, activity_id=record.id):
            record.trace_id = current_trace_id()
            try:
                yield handle
            except BaseException as exc:
                record.status, record.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                record.generated.clear()  # nothing half-made is claimed as generated
                record.ended_at = datetime.now(UTC)
                await self.record(record)
                raise
            record.status, record.ended_at = "succeeded", datetime.now(UTC)
            await self.record(record)

    async def record(self, act: ActivityRecord) -> None:
        await self.runner.run(
            "MERGE (sa:SoftwareAgent {name: $name, version: $version}) "
            "CREATE (a:Activity) SET a = $props "
            "CREATE (a)-[:WAS_ASSOCIATED_WITH]->(sa)",
            {"name": act.agent.name, "version": act.agent.version, "props": act.node_properties()},
        )
        for rel, refs in (("USED", act.used), ("GENERATED", act.generated)):
            for label, keys in _group(refs).items():
                prop = KEY_PROPERTIES[label]
                rows = await self.runner.run(
                    f"MATCH (a:Activity {{id: $id}}) UNWIND $keys AS k "
                    f"MATCH (e:`{label}` {{`{prop}`: k}}) MERGE (a)-[:{rel}]->(e) RETURN collect(k) AS found",
                    {"id": act.id, "keys": keys},
                )
                missing = sorted(set(keys) - set(rows[0]["found"] if rows else []))
                if missing:
                    raise ProvenanceError(f"activity {act.id} {rel.lower()} missing {label} {missing[:5]}")

    async def lineage(self, entity: EntityRef, *, max_depth: int = 25) -> Lineage:
        """Every activity and input that led to ``entity``, back to hashed source files."""
        steps: list[LineageStep] = []
        sources: list[EntityRef] = []
        frontier, seen = [entity], {entity}
        for _ in range(max_depth):
            if not frontier:
                break
            nxt: list[EntityRef] = []
            for ent in frontier:
                rows = await self.runner.run(
                    f"MATCH (e:`{ent.label}` {{`{ent.key_property}`: $key}})<-[:GENERATED]-(a:Activity) "
                    "OPTIONAL MATCH (a)-[:USED]->(u) "
                    "RETURN a {.*} AS activity, collect({labels: labels(u), props: properties(u)}) AS used "
                    "ORDER BY activity.started_at",
                    {"key": ent.key},
                )
                if ent.label in ROOT_LABELS and ent not in sources:  # a hashed input, however it arrived
                    sources.append(ent)
                for row in rows:
                    used = [r for u in row["used"] if (r := _ref(u)) is not None]
                    steps.append(LineageStep(ent, row["activity"], used))
                    for u in used:
                        if u not in seen:
                            seen.add(u)
                            nxt.append(u)
            frontier = nxt
        return Lineage(entity, steps, sources)


def _ref(u: dict[str, Any]) -> EntityRef | None:
    for label in u.get("labels") or []:
        if label in KEY_PROPERTIES and (key := (u.get("props") or {}).get(KEY_PROPERTIES[label])) is not None:
            return EntityRef(label=label, key=str(key))
    return None
