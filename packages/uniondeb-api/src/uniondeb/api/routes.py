"""The JSON API: uploads and jobs, review queues, write-back, dashboards, cases, audit.

Implements: UND-020, UND-024, UND-050, UND-062, UND-070, UND-082
"""

from pathlib import PurePath
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from uniondeb.agents import crynhoi
from uniondeb.agents.porthor import register_upload
from uniondeb.api import actions
from uniondeb.api.auth import Principal, current, require
from uniondeb.cases import Actor
from uniondeb.core.prov import EntityRef
from uniondeb.store import review
from uniondeb.store.tags import TagWrite, append_tag
from uniondeb.worker.services import Services

router = APIRouter()
EXTENSIONS = {".jsonl", ".csv", ".parquet"}
DATASETS = {"calls", "messages", "handoff", "email"}

reviewer = require("reviewer", "compliance_lead")
compliance = require("compliance", "compliance_lead")
case_roles = require("investigator", "nominated_officer", "case_admin")
officer = require("nominated_officer")


def svc(request: Request) -> Services:
    return request.app.state.services


S = Depends(svc)

# ───────────────────────────── uploads and jobs ─────────────────────────────────────


@router.post("/uploads", status_code=202)
async def upload(
    request: Request,
    file: UploadFile,
    dataset: str | None = Form(None),
    source_system: str | None = Form(None),
    principal: Principal = Depends(require("uploader")),
    s: Services = S,
) -> JSONResponse:
    """Store a file under its SHA-256, record it, and queue ingest. The same bytes are accepted once."""
    limit = request.app.state.max_upload_bytes
    if int(request.headers.get("content-length") or 0) > limit + 65536:
        raise HTTPException(413, f"upload exceeds {limit} bytes")
    name = PurePath(file.filename or "upload").name
    if PurePath(name).suffix.lower() not in EXTENSIONS:
        raise HTTPException(415, f"upload a {', '.join(sorted(EXTENSIONS))} file")
    if dataset is not None and dataset not in DATASETS:
        raise HTTPException(422, f"dataset must be one of {sorted(DATASETS)}")
    sha, size, _ = await run_in_threadpool(s.uploads.save, file.file, max_bytes=limit)
    if size == 0:
        raise HTTPException(422, "the file is empty")
    synthetic = getattr(getattr(s.runner, "database", None), "endswith", lambda _x: False)("synthetic")
    _, new = await register_upload(
        s.runner, s.tarddiad, s.uploads.path(sha), uploader=principal.sub, filename=name, dataset=dataset,
        source_system=source_system, synthetic=synthetic,
    )  # fmt: skip
    if not new:
        return JSONResponse({"sha256": sha, "bytes": size, "duplicate": True}, status_code=200)
    job = await s.queue.enqueue("ingest", {"sha256": sha, "dataset": dataset}, unique_key=f"ingest:{sha}")
    return JSONResponse({"sha256": sha, "bytes": size, "duplicate": False, "job": job}, status_code=202)


@router.get("/uploads/{sha256}")
async def upload_status(
    sha256: str, _: Principal = Depends(require("uploader", "reviewer")), s: Services = S
) -> dict[str, Any]:
    rows = await s.runner.run(
        "MATCH (f:SourceFile {sha256: $sha}) RETURN f.state AS state, f.dataset AS dataset, f.format AS format, f.rows AS rows, "
        "f.rows_loaded AS loaded, f.rows_rejected AS in_review, f.rows_duplicate AS linked, f.uploaded_at AS uploaded_at",
        {"sha": sha256},
    )
    if not rows:
        raise HTTPException(404, "not found")
    return rows[0]


@router.get("/jobs/{job_id}")
async def job(
    job_id: str,
    _: Principal = Depends(require("uploader", "reviewer", "compliance", "compliance_lead")),
    s: Services = S,
) -> dict[str, Any]:
    found = await s.queue.get(job_id)
    if found is None:
        raise HTTPException(404, "not found")
    return found.model_dump(
        include={
            "id",
            "kind",
            "state",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
            "error",
            "result",
        }
    )


# ───────────────────────────── review ───────────────────────────────────────────────


@router.get("/review/queues")
async def queues(_: Principal = Depends(reviewer), s: Services = S) -> dict[str, Any]:
    return {"queues": await review.queue_stats(s.runner), "notification": await actions.notification(s)}


@router.get("/review/items")
async def items(
    kind: str | None = None, limit: int = 50, _: Principal = Depends(reviewer), s: Services = S
) -> list[dict[str, Any]]:
    found = await review.list_items(s.runner, kind=kind, limit=min(limit, 200))
    return [
        {k: i.get(k) for k in ("id", "kind", "state", "four_eyes", "summary", "created_at")}
        | {"decisions": len(i["decisions"])}
        for i in found
    ]


@router.get("/review/items/{item_id}")
async def item(item_id: str, principal: Principal = Depends(reviewer), s: Services = S) -> dict[str, Any]:
    return await actions.describe_item(s, await review.get_item(s.runner, item_id), principal)


@router.post("/review/items/{item_id}/decisions")
async def decide(
    item_id: str, body: actions.DecisionIn, principal: Principal = Depends(reviewer), s: Services = S
) -> review.Outcome:
    return await actions.decide_item(s, principal, item_id, body)


class ReasonIn(BaseModel):
    reason: str


@router.post("/review/alerts/{alert_id}/identity")
async def alert_identity(
    alert_id: str, body: ReasonIn, principal: Principal = Depends(require("compliance_lead")), s: Services = S
) -> dict[str, Any]:
    return await actions.reveal_alert_subject(s, principal, alert_id, body.reason)


@router.post("/review/alerts/{alert_id}/pack", status_code=201)
async def alert_pack(alert_id: str, principal: Principal = Depends(reviewer), s: Services = S) -> crynhoi.CasePack:
    return await actions.build_alert_pack(s, principal, alert_id)


@router.get("/review/packs/{pack_id}")
async def pack(pack_id: str, _: Principal = Depends(reviewer), s: Services = S) -> crynhoi.CasePack:
    found = await crynhoi.load_pack(s.runner, s.cipher, pack_id)
    if found is None:
        raise HTTPException(404, "not found")
    return found


class RatingIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/review/packs/{pack_id}/rating", status_code=204)
async def rate(pack_id: str, body: RatingIn, principal: Principal = Depends(reviewer), s: Services = S) -> None:
    try:
        await crynhoi.rate_pack(s.runner, pack_id, principal.sub, body.rating, body.comment)
    except ValueError as exc:
        raise HTTPException(404, "not found") from exc


# ───────────────────────────── write-back (agents) ──────────────────────────────────


class TagProposalIn(TagWrite):
    proposed: bool = False


@router.post("/writeback/tags", status_code=201)
async def propose_tag(
    body: TagProposalIn, _: Principal = Depends(require("service")), s: Services = S
) -> dict[str, str]:
    """The narrow door for an agent running outside this process: one typed tag, checked by the tag rules."""
    if body.method == "human":
        raise HTTPException(422, "agents do not write human tags")
    tag = TagWrite(**body.model_dump(exclude={"proposed"}))
    tag_id = await append_tag(s.runner, tag, proposed=body.proposed)
    out = {"tag_id": tag_id}
    if body.proposed and body.label == "dirty":
        out["review_item"] = await review.create_item(
            s.runner, "dirty_tag", created_by=body.created_by, about=EntityRef(label="Record", key=body.record_key),
            summary="dirty proposed", payload={"record_key": body.record_key, "tag_id": tag_id, "clauses": body.clauses, "evidence_span": body.evidence_span},
        )  # fmt: skip
    return out


# ───────────────────────────── dashboards and scans ─────────────────────────────────


@router.get("/dashboards/concern")
async def concern(_: Principal = Depends(compliance), s: Services = S) -> dict[str, Any]:
    return await actions.concern_dashboard(s)


@router.get("/dashboards/evals")
async def evals(_: Principal = Depends(compliance), s: Services = S) -> dict[str, Any]:
    from uniondeb.evals.online import online_report

    return (await online_report(s.runner)).model_dump()


class ScanIn(BaseModel):
    kind: str = Field(default="on_demand", pattern="^(daily|weekly|on_demand)$")
    window_days: int | None = Field(default=None, ge=1, le=366)
    window_to: str | None = None


@router.post("/scans", status_code=202)
async def scan(body: ScanIn, _: Principal = Depends(compliance), s: Services = S) -> dict[str, str]:
    return {"job": await s.queue.enqueue("scan", body.model_dump(exclude_none=True))}


# ───────────────────────────── cases ────────────────────────────────────────────────


class OpenCaseIn(BaseModel):
    person: str  # Person.id or roster person_id
    reason: str
    window_from: str | None = None
    window_to: str | None = None


@router.get("/cases")
async def cases(principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return [c.model_dump() for c in await s.cases.list_cases(principal.actor)]


@router.post("/cases", status_code=201)
async def open_case(body: OpenCaseIn, principal: Principal = Depends(officer), s: Services = S) -> dict[str, Any]:
    case = await s.cases.open_manual(
        body.person, principal.actor, body.reason, window_from=body.window_from, window_to=body.window_to
    )
    return case.model_dump()


@router.get("/cases/{case_id}")
async def case(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> dict[str, Any]:
    return (await s.cases.view(case_id, principal.actor)).model_dump()


class AssignIn(BaseModel):
    investigator_id: str
    person_id: str | None = None  # the investigator's own roster id; without it the conflict check refuses


@router.post("/cases/{case_id}/assignments")
async def assign(
    case_id: str, body: AssignIn, principal: Principal = Depends(officer), s: Services = S
) -> dict[str, Any]:
    investigator = Actor(id=body.investigator_id, roles=frozenset({"investigator"}), person_id=body.person_id)
    return (await s.cases.assign(case_id, investigator, principal.actor)).model_dump()


@router.post("/cases/{case_id}/identity")
async def identity(
    case_id: str, body: ReasonIn, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return await actions.case_profile(s, principal, case_id, body.reason)


@router.get("/cases/{case_id}/timeline")
async def timeline(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return await actions.case_timeline(s, principal, case_id)


@router.get("/cases/{case_id}/contacts")
async def contacts(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return await actions.case_contacts(s, principal, case_id)


@router.get("/cases/{case_id}/records/{key:path}/provenance")
async def provenance(
    case_id: str, key: str, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return await actions.record_provenance(s, principal, case_id, key)


@router.post("/cases/{case_id}/records/{key:path}/tags", status_code=201)
async def record_tag(
    case_id: str, key: str, body: actions.RecordTagIn, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, str]:
    return {"tag_id": await actions.tag_record(s, principal, case_id, key, body)}


class NoteIn(BaseModel):
    text: str


@router.get("/cases/{case_id}/notes")
async def notes(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return [n.model_dump() for n in await s.cases.notes(case_id, principal.actor)]


@router.post("/cases/{case_id}/notes", status_code=201)
async def add_note(
    case_id: str, body: NoteIn, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return (await s.cases.add_note(case_id, principal.actor, body.text)).model_dump()


class FlagIn(BaseModel):
    label: str
    reason: str
    target_label: str | None = None
    target_key: str | None = None
    param: str | None = None


@router.get("/cases/{case_id}/flags")
async def flags(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return [f.model_dump(mode="json") for f in await s.flags.flags_for_case(case_id, principal.actor)]


@router.post("/cases/{case_id}/flags", status_code=201)
async def set_flag(
    case_id: str, body: FlagIn, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    target = EntityRef(label=body.target_label, key=body.target_key) if body.target_label and body.target_key else None
    flag = await s.flags.set_flag(
        case_id, principal.actor, body.label, target=target, reason=body.reason, param=body.param
    )
    return flag.model_dump(mode="json")


class QuestionIn(BaseModel):
    question: str


@router.post("/cases/{case_id}/holwr")
async def holwr(
    case_id: str, body: QuestionIn, principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return (await actions.ask_holwr(s, principal, case_id, body.question)).model_dump()


@router.post("/cases/{case_id}/sar/narrative")
async def sar_narrative(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> dict[str, Any]:
    return (await actions.draft_sar_narrative(s, principal, case_id)).model_dump()


@router.get("/cases/{case_id}/sar/drafts")
async def sar_drafts(case_id: str, principal: Principal = Depends(case_roles), s: Services = S) -> list[dict[str, Any]]:
    return [d.model_dump() | {"payload": d.payload} for d in await s.sars.drafts(case_id, principal.actor)]


@router.post("/cases/{case_id}/sar/drafts", status_code=201)
async def sar_draft(
    case_id: str, fields: dict[str, Any], principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return (await s.sars.draft(case_id, principal.actor, fields)).model_dump()


@router.put("/sar/drafts/{draft_id}")
async def sar_update(
    draft_id: str, fields: dict[str, Any], principal: Principal = Depends(case_roles), s: Services = S
) -> dict[str, Any]:
    return (await s.sars.update_draft(draft_id, principal.actor, fields)).model_dump()


@router.post("/sar/drafts/{draft_id}/approval", status_code=201)
async def sar_approve(draft_id: str, principal: Principal = Depends(officer), s: Services = S) -> dict[str, Any]:
    """The nominated officer, as a second person, approves; only then is the (mock) submission made."""
    sub = await s.sars.approve_and_submit(draft_id, principal.actor)
    return sub.model_dump(exclude={"payload_json"})


@router.get("/cases/{case_id}/sar/submissions")
async def sar_submissions(
    case_id: str, principal: Principal = Depends(require("nominated_officer", "auditor")), s: Services = S
) -> list[dict[str, Any]]:
    return [x.model_dump(exclude={"payload_json"}) for x in await s.sars.submissions(case_id, principal.actor)]


class CloseIn(BaseModel):
    outcome: str
    retention_review: str


@router.post("/cases/{case_id}/closure")
async def close(
    case_id: str, body: CloseIn, principal: Principal = Depends(officer), s: Services = S
) -> dict[str, Any]:
    return (await s.cases.close(case_id, principal.actor, body.outcome, body.retention_review)).model_dump()


# ───────────────────────────── audit ────────────────────────────────────────────────


@router.get("/audit/events")
async def audit_events(
    case_id: str | None = None,
    actor: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(require("auditor")),
    s: Services = S,
) -> list[dict[str, Any]]:
    await s.audit.append(principal.sub, "audit.read", case_id=case_id, detail={"actor": actor, "limit": limit})
    return [e.model_dump() for e in await s.audit.events(case_id=case_id, actor=actor, limit=min(limit, 1000))]


@router.get("/audit/verification")
async def audit_verification(principal: Principal = Depends(require("auditor")), s: Services = S) -> dict[str, Any]:
    return (await s.audit.verify()).model_dump()


@router.get("/whoami")
async def whoami(principal: Principal = Depends(current)) -> dict[str, Any]:
    return {"sub": principal.sub, "roles": sorted(principal.roles)}
