"""Human review queues and decisions (plan §6).

Every queue has a fixed set of decisions. A decision needs a reviewer and a rationale, is stored
as ``(:Reviewer)-[:DECIDED]->(:ReviewItem)`` and is recorded as a ``Review`` activity. Dirty tags
and alerts are **four-eyes**: the item is settled only when two different reviewers have given the
same decision, so no single person, and no agent, can make ``dirty`` current or open a case.

What a settled decision *does* is an effect. Tag effects are applied here; effects that belong to
other layers (replay a file, extend the alias registry, open a case) are passed in by the caller.

Implements: UND-050
"""

import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.store.provenance import ActivityHandle, Tarddiad
from uniondeb.store.runner import CypherRunner
from uniondeb.store.tags import TagWrite, append_tag, current_tag

REVIEW_AGENT = AgentRef(name="review-console", version="0.1.0")
OPEN_STATES = ("open", "awaiting_second", "disputed", "needs_info")


@dataclass(frozen=True, slots=True)
class Policy:
    options: frozenset[str]
    four_eyes: bool = False
    non_final: frozenset[str] = frozenset()  # recorded, but the item stays open
    restricted: tuple[tuple[str, str], ...] = ()  # (decision, role the reviewer must hold)


POLICIES: dict[str, Policy] = {
    "ingest_error": Policy(frozenset({"fix_resubmit", "reject", "source_issue"})),
    "mail_dlq": Policy(frozenset({"fix_resubmit", "reject", "source_issue"})),
    "mapping_proposal": Policy(frozenset({"approve", "reject"})),
    "dirty_tag": Policy(
        frozenset({"confirm_dirty", "downgrade_question", "downgrade_clean", "needs_info"}),
        four_eyes=True,
        non_final=frozenset({"needs_info"}),
    ),
    "question_tag": Policy(frozenset({"clear", "escalate"})),
    "alert": Policy(
        frozenset({"dismiss", "open_case", "escalate_legal"}),
        four_eyes=True,
        restricted=(("escalate_legal", "compliance_lead"),),
    ),
}


class ReviewError(ValueError):
    pass


class ItemNotFound(ReviewError):
    pass


class Reviewer(BaseModel):
    id: str
    roles: frozenset[str] = frozenset()


class Outcome(BaseModel):
    item_id: str
    kind: str
    state: str
    decision: str | None = None  # set once the item is settled
    decided_by: list[str] = []
    generated: list[str] = []


type Effect = Callable[[dict[str, Any], str, list[str], ActivityHandle], Awaitable[None]]


async def create_item(
    runner: CypherRunner,
    kind: str,
    *,
    created_by: str,
    about: EntityRef | None = None,
    summary: str = "",
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Open a review item. ``summary`` must be content-free: it is what queue listings show."""
    if kind not in POLICIES:
        raise ReviewError(f"unknown review kind {kind!r}")
    props = {
        "id": str(uuid.uuid7()),
        "kind": kind,
        "state": "open",
        "four_eyes": POLICIES[kind].four_eyes,
        "summary": summary,
        "payload_json": json.dumps(dict(payload or {}), default=str, ensure_ascii=False),
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": created_by,
    }
    if about is not None:
        props |= {"about_label": about.label, "about_key": about.key}
    await runner.run("CREATE (i:ReviewItem) SET i = $props", {"props": props})
    if about is not None:
        linked = await runner.run(
            f"MATCH (i:ReviewItem {{id: $id}}) MATCH (e:`{about.label}` {{`{about.key_property}`: $key}}) "
            "MERGE (i)-[:ABOUT]->(e) RETURN count(e) AS n",
            {"id": props["id"], "key": about.key},
        )
        if not linked or not linked[0]["n"]:
            raise ReviewError(f"review item is about a missing entity {about}")
    return props["id"]


def _item(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row["item"])
    item["payload"] = json.loads(item.pop("payload_json", None) or "{}")
    item["decisions"] = sorted((d for d in row["decisions"] if d.get("reviewer")), key=lambda d: d["at"])
    return item


_ITEM = (
    "OPTIONAL MATCH (rv:Reviewer)-[d:DECIDED]->(i) "
    "WITH i, collect({reviewer: rv.id, decision: d.decision, rationale: d.rationale, at: d.at}) AS decisions "
    "RETURN i {.*} AS item, decisions"
)


async def get_item(runner: CypherRunner, item_id: str) -> dict[str, Any]:
    rows = await runner.run("MATCH (i:ReviewItem {id: $id}) " + _ITEM, {"id": item_id})
    if not rows:
        raise ItemNotFound(f"no review item {item_id}")
    return _item(rows[0])


async def list_items(
    runner: CypherRunner, *, kind: str | None = None, states: tuple[str, ...] = OPEN_STATES, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await runner.run(
        "MATCH (i:ReviewItem) WHERE i.state IN $states AND ($kind IS NULL OR i.kind = $kind) "
        "WITH i ORDER BY i.created_at, i.id LIMIT $limit " + _ITEM + " ORDER BY item.created_at, item.id",
        {"states": list(states), "kind": kind, "limit": limit},
    )
    return [_item(r) for r in rows]


async def queue_stats(runner: CypherRunner) -> list[dict[str, Any]]:
    """Counts and oldest age per queue: all a notification may carry (plan §6: no content)."""
    return await runner.run(
        "MATCH (i:ReviewItem) WHERE i.state IN $states "
        "RETURN i.kind AS kind, count(i) AS open, min(i.created_at) AS oldest ORDER BY kind",
        {"states": list(OPEN_STATES)},
    )


def _settle(policy: Policy, decisions: list[dict[str, Any]]) -> tuple[str, str | None, list[str]]:
    """(state, settled decision, deciders) from every decision recorded so far."""
    latest = {d["reviewer"]: d["decision"] for d in decisions if d["decision"] not in policy.non_final}
    if not latest:
        return "needs_info", None, []
    if not policy.four_eyes:
        (reviewer, decision), *_ = latest.items()
        return "decided", decision, [reviewer]
    decision, votes = Counter(latest.values()).most_common(1)[0]
    if votes >= 2:
        return "decided", decision, sorted(r for r, d in latest.items() if d == decision)
    return ("awaiting_second" if len(latest) == 1 else "disputed"), None, []


async def decide(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    item_id: str,
    reviewer: Reviewer,
    decision: str,
    rationale: str,
    *,
    case_pack_version: str | None = None,
    effects: Mapping[str, Effect] | None = None,
) -> Outcome:
    item = await get_item(runner, item_id)
    policy = POLICIES[item["kind"]]
    if item["state"] not in OPEN_STATES:
        raise ReviewError(f"review item {item_id} is already {item['state']}")
    if decision not in policy.options:
        raise ReviewError(f"{decision!r} is not a decision for {item['kind']}: {sorted(policy.options)}")
    if not rationale or not rationale.strip():
        raise ReviewError("a rationale is required")
    for restricted, role in policy.restricted:
        if decision == restricted and role not in reviewer.roles:
            raise ReviewError(f"{decision} needs the {role} role")
    if decision not in policy.non_final and any(
        d["reviewer"] == reviewer.id and d["decision"] not in policy.non_final for d in item["decisions"]
    ):
        raise ReviewError("this reviewer has already decided this item; a second reviewer is needed")

    now = datetime.now(UTC).isoformat()
    recorded = [*item["decisions"], {"reviewer": reviewer.id, "decision": decision, "rationale": rationale, "at": now}]
    state, settled, deciders = _settle(policy, recorded)
    async with tarddiad.activity(ActivityKind.REVIEW, agent=REVIEW_AGENT, trigger=f"reviewer:{reviewer.id}") as act:
        act.used(EntityRef(label="ReviewItem", key=item_id))
        if settled is not None:
            effect = {**tag_effects(runner), **(effects or {})}.get(item["kind"])
            if effect is not None:
                await effect(item, settled, deciders, act)
        # The decision is recorded only after its effect succeeded, so a failed effect can be retried.
        await runner.run(
            "MATCH (i:ReviewItem {id: $id}) MERGE (rv:Reviewer {id: $reviewer}) "
            "CREATE (rv)-[:DECIDED {decision: $decision, rationale: $rationale, at: $at, activity_id: $act, "
            "case_pack_version: $pack}]->(i) "
            "SET i.state = $state, i.decision = $settled, i.decided_at = $decided_at, i.decided_by = $by",
            {
                "id": item_id,
                "reviewer": reviewer.id,
                "decision": decision,
                "rationale": rationale.strip(),
                "at": now,
                "act": act.id,
                "pack": case_pack_version or "",
                "state": state,
                "settled": settled,
                "decided_at": now if settled else None,
                "by": deciders,
            },
        )
        generated = [str(g) for g in act.record.generated]
    return Outcome(
        item_id=item_id, kind=item["kind"], state=state, decision=settled, decided_by=deciders, generated=generated
    )


# ───────────────────────────── tag effects ──────────────────────────────────────────


async def _human_tag(
    runner: CypherRunner, item: dict[str, Any], label: str, deciders: list[str], act: ActivityHandle, **extra: Any
) -> None:
    key = item["payload"]["record_key"]
    current = await current_tag(runner, key) or {}
    tag = TagWrite(
        record_key=key,
        label=label,
        method="human",
        created_by="+".join(deciders),
        trigger_hits=current.get("trigger_hits", []),
        trigger_list_version=current.get("trigger_list_version"),
        rationale=f"review item {item['id']}",
        **extra,
    )
    tag_id = await append_tag(runner, tag, activity_id=act.id, resolves=item["payload"].get("tag_id"))
    act.generated(EntityRef(label="Tag", key=tag_id))


def tag_effects(runner: CypherRunner) -> dict[str, Effect]:
    """Effects for the tag queues, bound to ``runner``."""

    async def dirty(item: dict[str, Any], decision: str, deciders: list[str], act: ActivityHandle) -> None:
        payload = item["payload"]
        if decision == "confirm_dirty":
            await _human_tag(
                runner,
                item,
                "dirty",
                deciders,
                act,
                clauses=payload.get("clauses", []),
                evidence_span=payload.get("evidence_span"),
            )
        else:
            await _human_tag(runner, item, decision.removeprefix("downgrade_"), deciders, act)

    async def question(item: dict[str, Any], decision: str, deciders: list[str], act: ActivityHandle) -> None:
        if decision == "clear":
            await _human_tag(runner, item, "clean", deciders, act)
            return
        key = item["payload"]["record_key"]
        proposal = TagWrite(record_key=key, label="dirty", method="human", created_by=deciders[0])
        tag_id = await append_tag(runner, proposal, proposed=True, activity_id=act.id)
        act.generated(EntityRef(label="Tag", key=tag_id))
        new = await create_item(
            runner,
            "dirty_tag",
            created_by=f"reviewer:{deciders[0]}",
            about=EntityRef(label="Record", key=key),
            summary=f"escalated from review item {item['id']}",
            payload={"record_key": key, "tag_id": tag_id, "escalated_from": item["id"]},
        )
        act.generated(EntityRef(label="ReviewItem", key=new))

    return {"dirty_tag": dirty, "question_tag": question}
