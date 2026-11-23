"""What the review console and the investigation portal do, as functions of (services, principal).

The JSON API and the HTML pages both call these, so a rule enforced here holds for both. Every
function that reads case material checks access through the case service (which audits refusals)
and then writes its own audit event, so there is no way to look at a case without leaving a trace.
"""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from uniondeb.agents import crynhoi
from uniondeb.agents.holwr import Holwr, HolwrAnswer, SarNarrative
from uniondeb.agents.models import ModelNotConfigured
from uniondeb.agents.porthor.mapping import mapping_effect
from uniondeb.agents.porthor.pipeline import _reason, resubmit_row
from uniondeb.agents.porthor.stream import consume_mail
from uniondeb.api.auth import Principal
from uniondeb.cases import Case, NotFound, PermissionDenied
from uniondeb.core.prov import EntityRef
from uniondeb.store import review
from uniondeb.store.provenance import ActivityHandle
from uniondeb.store.tags import TagWrite, append_tag, history
from uniondeb.worker.services import Services

TEXT = ("subject", "body", "transcript", "content")
EVER = ("0000", "9999")


class DecisionIn(BaseModel):
    decision: str
    rationale: str
    fixed_row: dict[str, Any] | None = None  # with fix_resubmit: the corrected row
    case_pack_version: str | None = None


class RecordTagIn(BaseModel):
    label: str = Field(pattern="^(clean|question|dirty)$")
    rationale: str
    clauses: list[str] = Field(default_factory=list)
    context: bool = False  # relevant to the case but not itself misconduct (design §7.3)


# ───────────────────────────── review console ───────────────────────────────────────


async def _record_view(s: Services, key: str) -> dict[str, Any]:
    rows = await s.runner.run(
        "MATCH (r:Record {key: $key}) OPTIONAL MATCH (r)-[l:FROM|TO|ON|SENT_FROM]->(d:Device) "
        "OPTIONAL MATCH (p:Person)-[:USES]->(d) WITH r, l, d, collect(p.kind) AS kinds "
        "RETURN r {.*} AS r, collect({role: type(l), device: d.kind, "
        "party: CASE WHEN 'employee' IN kinds THEN 'employee' WHEN 'external' IN kinds THEN 'external' ELSE 'unknown' END}) AS parties",
        {"key": key},
    )
    if not rows:
        return {"key": key, "missing": True}
    r = rows[0]["r"]
    return {
        "key": key,
        "dataset": r.get("dataset"),
        "occurred_at": r.get("occurred_at"),
        "medium": r.get("channel") or r.get("network"),
        "direction": r.get("direction"),
        "text": {k: r[k] for k in TEXT if r.get(k)},
        "content_purged": bool(r.get("content_purged_at")),
        "parties": [p for p in rows[0]["parties"] if p["role"]],  # roles and kinds; never who
        "tags": await history(s.runner, key),
    }


async def describe_item(s: Services, item: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """A review item with what a reviewer needs to decide it. Opening one shows record content, so
    it is logged like any other read of content."""
    await s.audit.append(
        principal.sub, "review.view_item", target=f"ReviewItem:{item['id']}", detail={"kind": item["kind"]}
    )
    out, payload = dict(item), item["payload"]
    out["options"] = sorted(review.POLICIES[item["kind"]].options)
    if payload.get("record_key"):
        out["record"] = await _record_view(s, payload["record_key"])
    if item.get("about_label") == "IngestError":
        rows = await s.runner.run("MATCH (e:IngestError {id: $id}) RETURN e {.*} AS e", {"id": item["about_key"]})
        if rows:
            e = rows[0]["e"]
            out["error"] = {
                "row": e.get("row"),
                "reason": e.get("reason"),
                "dataset": e.get("dataset"),
                "report": json.loads(e.get("report_json") or "{}"),
                "raw_row": s.cipher.decrypt_json(e["raw_enc"]) if e.get("raw_enc") else None,
            }
    if item["kind"] == "alert":
        rows = await s.runner.run("MATCH (a:Alert {id: $id}) RETURN a {.*} AS a", {"id": payload["alert_id"]})
        if rows:
            a = rows[0]["a"]
            out["alert"] = {
                k: a.get(k)
                for k in (
                    "id",
                    "state",
                    "patterns",
                    "score",
                    "subject_pseudonym",
                    "window_from",
                    "window_to",
                    "explanation",
                )
            } | {
                "metrics": json.loads(a.get("metrics_json") or "{}"),
                "hypotheses": json.loads(a.get("hypotheses_json") or "[]"),
                "records": len(a.get("record_keys") or []),
            }
            packs = await s.runner.run(
                "MATCH (c:CasePack {subject_pseudonym: $s}) RETURN c.id AS id, c.version AS version, c.created_at AS at ORDER BY version DESC",
                {"s": a.get("subject_pseudonym")},
            )
            out["case_packs"] = packs
    return out


def _effects(s: Services, principal: Principal, body: DecisionIn) -> dict[str, review.Effect]:
    async def ingest_error(item: dict[str, Any], decision: str, deciders: list[str], act: ActivityHandle) -> None:
        if item.get("about_label") != "IngestError":  # the whole file was unreadable
            if decision == "fix_resubmit":
                await s.queue.enqueue(
                    "ingest", {"sha256": item["payload"]["file"]}, unique_key=f"ingest:{item['payload']['file']}"
                )
            return
        if decision == "fix_resubmit":
            if not body.fixed_row:
                raise review.ReviewError("fix_resubmit needs the corrected row")
            try:
                key = await resubmit_row(s.runner, s.tarddiad, item["about_key"], body.fixed_row, by=principal.sub)
            except (ValidationError, ValueError) as exc:
                raise review.ReviewError(f"the corrected row still fails: {_reason(exc)}") from exc  # no values
            act.generated(EntityRef(label="Record", key=key))
            await s.queue.enqueue("tag", {"sha256": item["payload"]["file"]})
        else:
            await s.runner.run(
                "MATCH (e:IngestError {id: $id}) SET e.state = $state, e.resolved_at = $now, e.resolved_by = $by",
                {"id": item["about_key"], "state": decision, "now": datetime.now(UTC).isoformat(), "by": principal.sub},
            )

    async def mail_dlq(item: dict[str, Any], decision: str, deciders: list[str], act: ActivityHandle) -> None:
        if decision != "fix_resubmit":
            return
        if not body.fixed_row:
            raise review.ReviewError("fix_resubmit needs the corrected message document")
        if (
            await consume_mail(s.runner, s.tarddiad, body.fixed_row, {"raw_sha256": item["payload"]["file"]})
            == "review"
        ):
            raise review.ReviewError("the corrected message still does not validate")

    async def alert(item: dict[str, Any], decision: str, deciders: list[str], act: ActivityHandle) -> None:
        alert_id, state, case_id = (
            item["payload"]["alert_id"],
            {"dismiss": "dismissed", "escalate_legal": "escalated_legal"}.get(decision),
            None,
        )
        if decision == "open_case":  # the escalation path of design §7.1: only after two reviewers agreed
            case = await s.cases.open_from_alert(alert_id, principal.actor, body.rationale)
            state, case_id = "case_opened", case.id
            act.generated(EntityRef(label="Case", key=case.id))
        await s.runner.run(
            "MATCH (a:Alert {id: $id}) SET a.state = $state, a.case_id = $case, a.decided_at = $now, a.decided_by = $by",
            {"id": alert_id, "state": state, "case": case_id, "now": datetime.now(UTC).isoformat(), "by": deciders},
        )

    return {
        "ingest_error": ingest_error,
        "mail_dlq": mail_dlq,
        "alert": alert,
        "mapping_proposal": mapping_effect(s.runner, s.tarddiad, s.cipher, s.uploads.path),
    }


async def decide_item(s: Services, principal: Principal, item_id: str, body: DecisionIn) -> review.Outcome:
    return await review.decide(
        s.runner,
        s.tarddiad,
        item_id,
        principal.reviewer,
        body.decision,
        body.rationale,
        case_pack_version=body.case_pack_version,
        effects=_effects(s, principal, body),
    )


async def notification(s: Services) -> str:
    """All a notification may say (design §6): a count, no content, no names, no case details."""
    n = sum(q["open"] for q in await review.queue_stats(s.runner))
    return f"{n} review item{'' if n == 1 else 's'} waiting"


async def _alert_subject(s: Services, alert_id: str) -> dict[str, Any]:
    rows = await s.runner.run(
        "MATCH (a:Alert {id: $id})-[:ABOUT]->(p:Person) RETURN a {.*} AS a, p {.*} AS p", {"id": alert_id}
    )
    if not rows:
        raise review.ItemNotFound(f"no alert {alert_id}")
    return rows[0]


async def reveal_alert_subject(s: Services, principal: Principal, alert_id: str, reason: str) -> dict[str, Any]:
    """Resolve an alert's pseudonym. Compliance leads only, with a reason, and always audited."""
    if not reason.strip():
        raise review.ReviewError("a reason is required to resolve an identity")
    row = await _alert_subject(s, alert_id)
    await s.audit.append(principal.sub, "alert.reveal_identity", target=f"Alert:{alert_id}", reason=reason.strip())
    p = row["p"]
    return {"name": p.get("name"), "person_id": p.get("person_id"), "team": p.get("team"), "role": p.get("role")}


async def build_alert_pack(s: Services, principal: Principal, alert_id: str) -> crynhoi.CasePack:
    row = await _alert_subject(s, alert_id)
    a = row["a"]
    await s.audit.append(principal.sub, "pack.build", target=f"Alert:{alert_id}")  # a pack holds record text
    return await crynhoi.build_pack(
        s.runner,
        s.tarddiad,
        s.echdynnwr,
        s.cipher,
        subject=row["p"]["id"],
        subject_pseudonym=a["subject_pseudonym"],
        window_from=a["window_from"],
        window_to=a["window_to"],
        model=s.model("crynhoi"),
    )


# ───────────────────────────── dashboards ───────────────────────────────────────────


async def concern_dashboard(s: Services, *, scans: int = 12) -> dict[str, Any]:
    """Concern volumes per person (pseudonymous) and per team, and the trend over recent scans."""
    rows = await s.runner.run("MATCH (x:Scan) RETURN x {.*} AS x ORDER BY x.created_at DESC LIMIT $n", {"n": scans})
    history = []
    for r in rows:
        metrics = [m for m in json.loads(r["x"]["metrics_json"]) if m["scored"]]
        teams: dict[str, list[float]] = {}
        for m in metrics:
            teams.setdefault(m["team"] or "none", []).append(m["concern_per_1000"])
        history.append(
            {
                "scan_id": r["x"]["id"],
                "kind": r["x"]["kind"],
                "window_from": r["x"]["window_from"],
                "window_to": r["x"]["window_to"],
                "people": metrics,
                "teams": {
                    t: {"people": len(v), "mean_concern_per_1000": round(sum(v) / len(v), 1)}
                    for t, v in sorted(teams.items())
                },
            }
        )
    alerts = await s.runner.run("MATCH (a:Alert) RETURN a.state AS state, count(a) AS n ORDER BY state")
    return {
        "latest": history[0] if history else None,
        "trend": [{k: h[k] for k in ("scan_id", "window_to", "teams")} for h in history],
        "alerts": {a["state"]: a["n"] for a in alerts},
    }


# ───────────────────────────── investigation portal ─────────────────────────────────


async def _open_case(s: Services, principal: Principal, case_id: str, screen: str, **detail: Any) -> tuple[Case, str]:
    """Access check (refusals are audited by the case service), then the audit event for this screen."""
    case = await s.cases.require_access(case_id, principal.actor, attempted=f"portal.{screen}")
    await s.audit.append(principal.sub, f"portal.{screen}", case_id=case_id, detail=detail or None)
    [row] = await s.runner.run("MATCH (:Case {id: $id})-[:SUBJECT]->(p:Person) RETURN p.id AS id", {"id": case_id})
    return case, row["id"]


def _window(case: Case) -> dict[str, str]:
    return {"from": case.window_from or EVER[0], "to": case.window_to or EVER[1]}


async def case_timeline(s: Services, principal: Principal, case_id: str) -> list[dict[str, Any]]:
    """Every record involving the subject in the case window, with tags; text for flagged records only."""
    case, subject = await _open_case(s, principal, case_id, "timeline")
    rows = await s.runner.run(
        "CALL { MATCH (p:Person {id: $subject})-[:USES]->(:Device)<-[:FROM|TO|ON|SENT_FROM]-(r:Record) RETURN r "
        "UNION MATCH (p:Person {id: $subject})<-[:BELONGS_TO]-(:EmailAddress)<-[:FROM]-(r:Record) RETURN r } "
        "WITH r WHERE r.occurred_at >= $from AND r.occurred_at < $to "
        "OPTIONAL MATCH (t:Tag {current: true})-[:ON]->(r) "
        "OPTIONAL MATCH (q:Tag {proposed: true})-[:ON]->(r) WHERE q.superseded_by IS NULL "
        "WITH r, t, collect(q.label) AS proposals, COUNT { (:Flag {active: true})-[:ON]->(r) } AS flags "
        "RETURN r.key AS key, r.dataset AS dataset, r.occurred_at AS at, coalesce(r.channel, r.network) AS medium, "
        "r.direction AS direction, coalesce(t.label, 'untagged') AS tag, t.method AS method, t.clauses AS clauses, "
        "t.trigger_hits AS trigger_hits, t.evidence_span AS evidence_span, proposals, flags, "
        "CASE WHEN t.label IN ['question', 'dirty'] OR size(proposals) > 0 OR flags > 0 "
        "THEN coalesce(r.subject + ' | ', '') + coalesce(r.body, r.transcript, r.content, '') END AS text "
        "ORDER BY at, key",
        {"subject": subject, **_window(case)},
    )
    return rows


async def case_contacts(s: Services, principal: Principal, case_id: str) -> list[dict[str, Any]]:
    """Who the subject talks to, through which channels: the contact graph as a table."""
    case, subject = await _open_case(s, principal, case_id, "contacts")
    return await s.runner.run(
        "MATCH (:Person {id: $subject})-[:USES]->(sd:Device)<-[:FROM|TO]-(r:Record)-[:FROM|TO]->(d:Device) "
        "WHERE d <> sd AND r.occurred_at >= $from AND r.occurred_at < $to "
        "OPTIONAL MATCH (o:Person)-[:USES]->(d) WHERE o.person_id IS NOT NULL "
        "WITH d, collect(DISTINCT o.person_id) AS owners, collect(DISTINCT o.kind) AS kinds, r "
        "RETURN d.id AS device, d.kind AS kind, owners, kinds, size(owners) = 0 AS unregistered, count(DISTINCT r) AS records, "
        "collect(DISTINCT coalesce(r.channel, r.network)) AS media, min(r.occurred_at) AS first, max(r.occurred_at) AS last "
        "ORDER BY records DESC, device",
        {"subject": subject, **_window(case)},
    )


async def case_profile(s: Services, principal: Principal, case_id: str, reason: str) -> dict[str, Any]:
    """The subject's identity, devices and identifiers. This is the reveal: it needs a reason."""
    identity = await s.cases.reveal_identity(case_id, principal.actor, reason)
    devices = await s.runner.run(
        "MATCH (:Case {id: $id})-[:SUBJECT]->(p:Person)-[:USES]->(d:Device) OPTIONAL MATCH (d)-[:HAS_IDENTIFIER]->(i:Identifier) "
        "RETURN d.id AS device, d.kind AS kind, collect({kind: i.kind, value: i.value}) AS identifiers ORDER BY device",
        {"id": case_id},
    )
    return {"identity": identity.model_dump(), "devices": devices}


async def _in_case(s: Services, case_id: str, key: str) -> None:
    rows = await s.runner.run(
        "MATCH (:Case {id: $id})-[:SUBJECT]->(p:Person) MATCH (r:Record {key: $key}) "
        "WHERE EXISTS { (p)-[:USES]->(:Device)<-[:FROM|TO|ON|SENT_FROM]-(r) } "
        "OR EXISTS { (p)<-[:BELONGS_TO]-(:EmailAddress)<-[:FROM]-(r) } RETURN r.key AS key",
        {"id": case_id, "key": key},
    )
    if not rows:
        raise NotFound(f"no record {key} in this case")


async def record_provenance(s: Services, principal: Principal, case_id: str, key: str) -> dict[str, Any]:
    await _open_case(s, principal, case_id, "provenance", record=key)
    await _in_case(s, case_id, key)
    lineage = await s.tarddiad.lineage(EntityRef(label="Record", key=key))
    tags = await history(s.runner, key)
    tag_steps = []
    for t in tags:
        tl = await s.tarddiad.lineage(EntityRef(label="Tag", key=t["id"]), max_depth=1)
        tag_steps += [
            {"tag": t["id"], "label": t["label"], "method": t["method"], **_step(x.activity)} for x in tl.steps
        ]
    return {
        "record": key,
        "source_sha256": lineage.source_hashes,
        "steps": [_step(x.activity) for x in lineage.steps],
        "tags": tags,
        "tag_steps": tag_steps,
    }


def _step(a: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id",
        "kind",
        "agent",
        "agent_version",
        "model",
        "prompt_version",
        "started_at",
        "status",
        "trigger",
        "trace_id",
    )
    return {k: a.get(k) for k in keep} | {k: v for k, v in a.items() if k.startswith("cfg_")}


async def tag_record(s: Services, principal: Principal, case_id: str, key: str, body: RecordTagIn) -> str:
    """An investigator's tag: human, current at once, superseding whatever was there (design §7.3)."""
    if not principal.roles & {"investigator", "nominated_officer"}:
        raise PermissionDenied("only an investigator or nominated officer tags records in a case")
    if not body.rationale.strip():
        raise review.ReviewError("a rationale is required")
    await _open_case(s, principal, case_id, "tag_record", record=key, label=body.label)
    await _in_case(s, case_id, key)
    async with s.cases.activity("record.tag", principal.actor) as act:
        act.used(EntityRef(label="Case", key=case_id), EntityRef(label="Record", key=key))
        tag = TagWrite(
            record_key=key,
            label=body.label,
            method="human",
            created_by=principal.sub,
            clauses=body.clauses,
            rationale=("[context] " if body.context else "") + body.rationale.strip(),
        )
        tag_id = await append_tag(s.runner, tag, activity_id=act.id)
        act.generated(EntityRef(label="Tag", key=tag_id))
    return tag_id


async def _case_extract(s: Services, case: Case, subject: str):
    handle = await s.echdynnwr.build("ymchwiliad.case.v1", {"subject": subject, **_window(case)})
    return s.echdynnwr.open(handle)


async def ask_holwr(s: Services, principal: Principal, case_id: str, question: str) -> HolwrAnswer:
    if not question.strip():
        raise review.ReviewError("ask a question")
    model = s.model("holwr")
    if model is None:
        raise ModelNotConfigured("set UNIONDEB_HOLWR_MODEL to use Holwr")
    case, subject = await _open_case(s, principal, case_id, "holwr")
    async with await _case_extract(s, case, subject) as reader:
        answer = await Holwr(model).ask(reader, question.strip())
    await s.audit.append(
        principal.sub,
        "holwr.question",
        case_id=case_id,
        detail={
            "question": answer.question,
            "cypher": answer.cypher,
            "record_keys": answer.record_keys,
            "model": answer.model,
        },
    )
    return answer


async def draft_sar_narrative(s: Services, principal: Principal, case_id: str, brief: str = "") -> SarNarrative:
    model = s.model("holwr")
    if model is None:
        raise ModelNotConfigured("set UNIONDEB_HOLWR_MODEL to use Holwr")
    case, subject = await _open_case(s, principal, case_id, "sar_narrative")
    async with await _case_extract(s, case, subject) as reader:
        narrative = await Holwr(model).draft_sar(reader, brief)
    await s.audit.append(
        principal.sub,
        "holwr.sar_narrative",
        case_id=case_id,
        detail={"record_keys": narrative.record_keys, "refused": narrative.refused_actions},
    )
    return narrative
