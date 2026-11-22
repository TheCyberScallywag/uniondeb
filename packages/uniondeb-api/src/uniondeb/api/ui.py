"""The review console and the Ymchwiliad portal as server-rendered pages.

The pages are thin: each calls the same ``actions`` and services as the JSON API, so the access
checks, the four-eyes rule and the audit trail cannot differ between the two. Plain HTML forms, no
client-side state, every value escaped by the template engine. (The plan's decision D7 offered
Streamlit or FastAPI pages; pages in the API process share its auth and need no second service.)

Implements: UND-051, UND-075
"""

import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, PackageLoader, select_autoescape

from uniondeb.agents.models import ModelNotConfigured
from uniondeb.api import actions
from uniondeb.api.auth import COOKIE, Principal, current
from uniondeb.cases import Actor, CaseError
from uniondeb.core.prov import EntityRef
from uniondeb.store import review
from uniondeb.store.tags import TagRuleError
from uniondeb.worker.services import Services

router = APIRouter(include_in_schema=False)
env = Environment(loader=PackageLoader("uniondeb.api", "templates"), autoescape=select_autoescape(default=True))
env.filters["pretty"] = lambda v: json.dumps(v, indent=1, ensure_ascii=False, default=str)
USER_ERRORS = (CaseError, review.ReviewError, TagRuleError, ModelNotConfigured, ValueError)


class _Login(Exception):
    pass


def user(request: Request) -> Principal:
    try:
        return current(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise _Login from exc
        raise


def page(
    name: str, request: Request, principal: Principal | None = None, status: int = 200, **ctx: Any
) -> HTMLResponse:
    html = env.get_template(name).render(principal=principal, error=request.query_params.get("error"), **ctx)
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def back(url: str, error: str | None = None) -> RedirectResponse:
    return RedirectResponse(url + (f"?error={quote(error[:300])}" if error else ""), status_code=303)


def svc(request: Request) -> Services:
    return request.app.state.services


U, S = Depends(user), Depends(svc)


@router.get("/login")
async def login_form(request: Request) -> HTMLResponse:
    return page("login.html", request, dev=request.app.state.auth.secret is not None)


@router.post("/login")
async def login(request: Request) -> RedirectResponse:
    """Development sign-in: paste a token. In production the OIDC proxy sets the cookie."""
    if request.app.state.auth.secret is None:
        raise HTTPException(404)
    token = str((await request.form()).get("token") or "").strip()
    try:
        request.app.state.auth.decode(token)
    except Exception:
        return back("/login", "that token is not valid")
    out = RedirectResponse("/", status_code=303)
    out.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return out


@router.get("/logout")
async def logout() -> RedirectResponse:
    out = RedirectResponse("/login", status_code=303)
    out.delete_cookie(COOKIE)
    return out


@router.get("/")
async def home(request: Request, principal: Principal = U) -> HTMLResponse:
    return page("home.html", request, principal)


# ───────────────────────────── review console ───────────────────────────────────────


def _reviewer(principal: Principal) -> None:
    if not principal.roles & {"reviewer", "compliance_lead"}:
        raise HTTPException(403, "the review console needs the reviewer role")


@router.get("/console")
async def console(request: Request, kind: str | None = None, principal: Principal = U, s: Services = S) -> HTMLResponse:
    _reviewer(principal)
    return page(
        "console.html",
        request,
        principal,
        queues=await review.queue_stats(s.runner),
        items=await review.list_items(s.runner, kind=kind, limit=100),
        kind=kind,
        notification=await actions.notification(s),
    )


@router.get("/console/items/{item_id}")
async def console_item(item_id: str, request: Request, principal: Principal = U, s: Services = S) -> HTMLResponse:
    _reviewer(principal)
    item = await actions.describe_item(s, await review.get_item(s.runner, item_id), principal)
    return page(
        "item.html",
        request,
        principal,
        item=item,
        decided=any(d["reviewer"] == principal.sub for d in item["decisions"]),
    )


@router.post("/console/items/{item_id}")
async def console_decide(item_id: str, request: Request, principal: Principal = U, s: Services = S) -> RedirectResponse:
    _reviewer(principal)
    form = await request.form()
    try:
        fixed = json.loads(str(form.get("fixed_row"))) if form.get("fixed_row") else None
        body = actions.DecisionIn(
            decision=str(form.get("decision") or ""), rationale=str(form.get("rationale") or ""), fixed_row=fixed
        )
        await actions.decide_item(s, principal, item_id, body)
    except USER_ERRORS as exc:
        return back(f"/console/items/{item_id}", str(exc))
    return back("/console")


@router.post("/console/items/{item_id}/pack")
async def console_pack(item_id: str, request: Request, principal: Principal = U, s: Services = S) -> RedirectResponse:
    _reviewer(principal)
    item = await review.get_item(s.runner, item_id)
    pack = await actions.build_alert_pack(s, principal, item["payload"]["alert_id"])
    return back(f"/console/packs/{pack.id}")


@router.get("/console/packs/{pack_id}")
async def console_pack_view(pack_id: str, request: Request, principal: Principal = U, s: Services = S) -> HTMLResponse:
    _reviewer(principal)
    from uniondeb.agents import crynhoi

    pack = await crynhoi.load_pack(s.runner, s.cipher, pack_id)
    if pack is None:
        raise HTTPException(404)
    return page("pack.html", request, principal, pack=pack)


# ───────────────────────────── investigation portal ─────────────────────────────────


def _portal(principal: Principal) -> None:
    if not principal.roles & {"investigator", "nominated_officer", "case_admin"}:
        raise HTTPException(403, "the portal is for assigned investigators and the nominated officer")


@router.get("/portal")
async def portal(request: Request, principal: Principal = U, s: Services = S) -> HTMLResponse:
    _portal(principal)
    return page("portal.html", request, principal, cases=await s.cases.list_cases(principal.actor))


@router.post("/portal")
async def portal_open(request: Request, principal: Principal = U, s: Services = S) -> RedirectResponse:
    _portal(principal)
    form = await request.form()
    try:
        case = await s.cases.open_manual(str(form.get("person") or ""), principal.actor, str(form.get("reason") or ""))
    except USER_ERRORS as exc:
        return back("/portal", str(exc))
    return back(f"/portal/cases/{case.id}")


async def _workspace(request: Request, principal: Principal, s: Services, case_id: str, **extra: Any) -> HTMLResponse:
    case = await s.cases.view(case_id, principal.actor)  # audited; 404 if not assigned
    return page(
        "case.html",
        request,
        principal,
        case=case,
        timeline=await actions.case_timeline(s, principal, case_id),
        contacts=await actions.case_contacts(s, principal, case_id),
        flags=await s.flags.flags_for_case(case_id, principal.actor),
        notes=await s.cases.notes(case_id, principal.actor),
        drafts=await s.sars.drafts(case_id, principal.actor),
        submissions=await s.sars.submissions(case_id, principal.actor)
        if principal.roles & {"nominated_officer", "auditor"}
        else [],
        vocabulary=s.flags.vocabulary.flags if hasattr(s.flags, "vocabulary") else [],
        **extra,
    )


@router.get("/portal/cases/{case_id}")
async def portal_case(case_id: str, request: Request, principal: Principal = U, s: Services = S) -> HTMLResponse:
    _portal(principal)
    return await _workspace(request, principal, s, case_id)


@router.post("/portal/cases/{case_id}/{action}")
async def portal_action(case_id: str, action: str, request: Request, principal: Principal = U, s: Services = S):
    """Every form on the case page posts here. Results that must be seen once (the identity, an
    answer) are rendered straight back and never stored in a URL."""
    _portal(principal)
    form = {k: str(v) for k, v in (await request.form()).items()}
    url, actor = f"/portal/cases/{case_id}", principal.actor
    try:
        match action:
            case "reveal":
                return await _workspace(
                    request,
                    principal,
                    s,
                    case_id,
                    profile=await actions.case_profile(s, principal, case_id, form.get("reason", "")),
                )
            case "holwr":
                return await _workspace(
                    request,
                    principal,
                    s,
                    case_id,
                    answer=await actions.ask_holwr(s, principal, case_id, form.get("question", "")),
                )
            case "provenance":
                return await _workspace(
                    request,
                    principal,
                    s,
                    case_id,
                    provenance=await actions.record_provenance(s, principal, case_id, form.get("key", "")),
                )
            case "note":
                await s.cases.add_note(case_id, actor, form.get("text", ""))
            case "flag":
                target = (
                    EntityRef(label=form["target_label"], key=form["target_key"]) if form.get("target_key") else None
                )
                await s.flags.set_flag(
                    case_id,
                    actor,
                    form.get("label", ""),
                    target=target,
                    reason=form.get("reason", ""),
                    param=form.get("param") or None,
                )
            case "tag":
                body = actions.RecordTagIn(
                    label=form.get("label", ""),
                    rationale=form.get("rationale", ""),
                    clauses=form.get("clauses", "").split(),
                    context="context" in form,
                )
                await actions.tag_record(s, principal, case_id, form.get("key", ""), body)
            case "assign":
                investigator = Actor(
                    id=form.get("investigator_id", ""),
                    roles=frozenset({"investigator"}),
                    person_id=form.get("person_id") or None,
                )
                await s.cases.assign(case_id, investigator, actor)
            case "sar_draft":
                fields = json.loads(form.get("fields") or "{}")
                if form.get("draft_id"):
                    await s.sars.update_draft(form["draft_id"], actor, fields)
                else:
                    await s.sars.draft(case_id, actor, fields)
            case "sar_narrative":
                return await _workspace(
                    request, principal, s, case_id, narrative=await actions.draft_sar_narrative(s, principal, case_id)
                )
            case "sar_approve":
                await s.sars.approve_and_submit(form.get("draft_id", ""), actor)
            case "close":
                await s.cases.close(case_id, actor, form.get("outcome", ""), form.get("retention_review", ""))
            case _:
                raise HTTPException(404)
    except USER_ERRORS as exc:
        if type(exc).__name__ == "NotFound":
            raise
        detail = "; ".join(getattr(exc, "errors", None) or getattr(exc, "rules", None) or [str(exc)])
        return back(url, detail)
    return back(url)
