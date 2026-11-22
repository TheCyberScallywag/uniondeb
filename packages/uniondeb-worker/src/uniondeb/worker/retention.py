"""The retention job: remove record content on schedule, keep the metadata, honour legal hold.

What is purged and when comes from the retention schedule (a governance decision; the packaged
file is a draft). A record's period depends on its current tag. Content means the free-text
properties only: the record, its tags and its provenance stay, so the fact of a communication and
every decision about it remain auditable after its words are gone.

Nothing tied to a case with an active ``legal_hold`` flag is touched. Each run is a ``Retention``
activity that used the records it purged, so the purge itself is in provenance.

Implements: UND-090
"""

import os
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from uniondeb.cases import FlagService
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.roles import CONTENT_PROPERTIES
from uniondeb.store.runner import CypherRunner

RETENTION = AgentRef(name="retention", version="0.1.0")
BATCH = 2000


class RetentionSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    status: str = "draft"
    content_days: dict[str, int] = Field(min_length=1)
    ingest_error_raw_days: int = Field(gt=0)

    @classmethod
    def load(cls, path: Path | None = None) -> RetentionSchedule:
        path = path or (Path(os.environ["UNIONDEB_RETENTION"]) if os.environ.get("UNIONDEB_RETENTION") else None)
        source = path or files("uniondeb.worker") / "config" / "retention.draft.yaml"
        schedule = cls.model_validate(yaml.safe_load(source.read_text("utf-8")))
        if missing := {"clean", "question", "dirty", "untagged"} - set(schedule.content_days):
            raise ValueError(f"retention schedule has no period for {sorted(missing)}")
        return schedule


class RetentionResult(BaseModel):
    schedule_version: str
    purged: dict[str, int]
    held: int  # due for purge but on legal hold
    ingest_errors: int
    activity_ids: list[str]


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_retention(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    flags: FlagService,
    *,
    schedule: RetentionSchedule | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    schedule, now = schedule or RetentionSchedule.load(), now or datetime.now(UTC)
    on_hold = await flags.records_on_hold()
    result = RetentionResult(schedule_version=schedule.version, purged={}, held=0, ingest_errors=0, activity_ids=[])
    has_content = " OR ".join(f"r.`{p}` IS NOT NULL" for p in CONTENT_PROPERTIES)
    remove = ", ".join(f"r.`{p}`" for p in CONTENT_PROPERTIES)
    for label, days in schedule.content_days.items():
        due = await runner.run(
            "MATCH (r:Record) WHERE r.occurred_at < $cutoff AND r.content_purged_at IS NULL AND (" + has_content + ") "
            "OPTIONAL MATCH (t:Tag {current: true})-[:ON]->(r) "
            "WITH r, coalesce(t.label, 'untagged') AS label WHERE label = $label RETURN r.key AS key",
            {"cutoff": _cutoff(now, days), "label": label},
        )
        keys = [d["key"] for d in due if d["key"] not in on_hold]
        result.held += len(due) - len(keys)
        result.purged[label] = len(keys)
        for i in range(0, len(keys), BATCH):
            part = keys[i : i + BATCH]
            async with tarddiad.activity(
                ActivityKind.RETENTION,
                agent=RETENTION,
                trigger=f"content:{label}:{days}d",
                config_versions={"retention_schedule": schedule.version},
            ) as act:
                act.used(*(EntityRef(label="Record", key=k) for k in part))
                await runner.run(
                    f"UNWIND $keys AS k MATCH (r:Record {{key: k}}) REMOVE {remove} "
                    "SET r.content_purged_at = $now, r.content_purged_by = $act",
                    {"keys": part, "now": now.isoformat(), "act": act.id},
                )
                result.activity_ids.append(act.id)
    rows = await runner.run(
        "MATCH (e:IngestError) WHERE e.created_at < $cutoff AND e.raw_enc IS NOT NULL "
        "REMOVE e.raw_enc SET e.raw_purged_at = $now RETURN count(e) AS n",
        {"cutoff": (now - timedelta(days=schedule.ingest_error_raw_days)).isoformat(), "now": now.isoformat()},
    )
    result.ingest_errors = rows[0]["n"] if rows else 0
    return result
