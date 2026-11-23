"""Append-only tags on records (design §4, §5.3).

A record's tag history is a chain of ``Tag`` nodes: a new tag is created, the one it replaces is
marked ``current: false`` with ``superseded_by``, and nothing else about an old tag ever changes.
The rules that keep a model from weakening a deterministic finding live here, at the write door,
so they hold whichever agent or endpoint is calling:

- precedence is ``dirty`` > ``question`` > ``clean``;
- a model (``method: llm``) can never lower a record's current tag, and can never tag a record
  with trigger hits ``clean``;
- only a human decision makes ``dirty`` current: agents can only *propose* it.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from uniondeb.store.runner import CypherRunner

type Label = Literal["clean", "question", "dirty"]
type Method = Literal["trigger", "llm", "human"]
PRECEDENCE: dict[str, int] = {"clean": 0, "question": 1, "dirty": 2}


class TagRuleError(ValueError):
    pass


class TagWrite(BaseModel):
    record_key: str
    label: Label
    method: Method
    created_by: str
    trigger_hits: list[str] = Field(default_factory=list)
    trigger_list_version: str | None = None
    clauses: list[str] = Field(default_factory=list)
    evidence_span: str | None = None
    rationale: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    lang: str | None = None  # language of the text, for fairness monitoring


async def current_tag(runner: CypherRunner, record_key: str) -> dict[str, Any] | None:
    rows = await runner.run(
        "MATCH (t:Tag {current: true})-[:ON]->(:Record {key: $key}) RETURN t {.*} AS tag", {"key": record_key}
    )
    return rows[0]["tag"] if rows else None


async def history(runner: CypherRunner, record_key: str) -> list[dict[str, Any]]:
    rows = await runner.run(
        "MATCH (t:Tag)-[:ON]->(:Record {key: $key}) RETURN t {.*} AS tag ORDER BY t.created_at, t.id",
        {"key": record_key},
    )
    return [r["tag"] for r in rows]


def check(tag: TagWrite, current: dict[str, Any] | None, *, proposed: bool) -> None:
    """Raise ``TagRuleError`` if writing ``tag`` over ``current`` would break a rule."""
    if tag.label == "dirty" and tag.method != "human" and not proposed:
        raise TagRuleError("an agent can only propose dirty; a human decision makes it current")
    if tag.method != "llm":
        return
    if tag.label == "clean" and (tag.trigger_hits or (current or {}).get("trigger_hits")):
        raise TagRuleError("a model cannot tag a record with trigger hits clean")
    if current and not proposed and PRECEDENCE[tag.label] < PRECEDENCE[current["label"]]:
        raise TagRuleError(f"a model cannot downgrade {current['label']} to {tag.label}")


async def append_tag(
    runner: CypherRunner,
    tag: TagWrite,
    *,
    proposed: bool = False,
    activity_id: str | None = None,
    resolves: str | None = None,
) -> str:
    """Write one tag and return its id. ``proposed`` tags wait for review and never become current;
    ``resolves`` names the proposal a human decision settles."""
    check(tag, await current_tag(runner, tag.record_key), proposed=proposed)
    props = {k: v for k, v in tag.model_dump(exclude={"record_key"}).items() if v not in (None, [])}
    props |= {
        "id": str(uuid.uuid7()),
        "record_key": tag.record_key,
        "current": not proposed,
        "proposed": proposed,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if activity_id:
        props["activity_id"] = activity_id
    if proposed:
        rows = await runner.run(
            "MATCH (r:Record {key: $key}) CREATE (t:Tag) SET t = $props CREATE (t)-[:ON]->(r) RETURN t.id AS id",
            {"key": tag.record_key, "props": props},
        )
    else:
        rows = await runner.run(
            "MATCH (r:Record {key: $key}) "
            "OPTIONAL MATCH (old:Tag)-[:ON]->(r) WHERE old.current = true OR old.id = $resolves "
            "WITH r, collect(old) AS olds "
            "CREATE (t:Tag) SET t = $props CREATE (t)-[:ON]->(r) "
            "FOREACH (o IN olds | SET o.current = false, o.superseded_by = t.id CREATE (t)-[:SUPERSEDES]->(o)) "
            "RETURN t.id AS id",
            {"key": tag.record_key, "props": props, "resolves": resolves or ""},
        )
    if not rows:
        raise TagRuleError(f"no record {tag.record_key!r} to tag")
    return rows[0]["id"]


async def append_initial_tags(
    runner: CypherRunner, tags: list[TagWrite], *, activity_id: str | None = None
) -> list[str]:
    """First, deterministic tags for records that have none yet, in bulk. Records that already have a
    current tag are left alone, so a re-run cannot rewrite history. Returns the ids created."""
    now, rows = datetime.now(UTC).isoformat(), []
    for tag in tags:
        if tag.method != "trigger" or tag.label == "dirty":
            raise TagRuleError("initial tags come from the trigger scan and are clean or question")
        props = {k: v for k, v in tag.model_dump(exclude={"record_key"}).items() if v not in (None, [])}
        props |= {"id": str(uuid.uuid7()), "record_key": tag.record_key, "current": True, "proposed": False}
        props |= {"created_at": now} | ({"activity_id": activity_id} if activity_id else {})
        rows.append({"key": tag.record_key, "props": props})
    created: list[str] = []
    for i in range(0, len(rows), 1000):
        found = await runner.run(
            "UNWIND $rows AS row MATCH (r:Record {key: row.key}) "
            "WHERE NOT EXISTS { (:Tag {current: true})-[:ON]->(r) } "
            "CREATE (t:Tag) SET t = row.props CREATE (t)-[:ON]->(r) RETURN t.id AS id",
            {"rows": rows[i : i + 1000]},
        )
        created += [f["id"] for f in found]
    return created
