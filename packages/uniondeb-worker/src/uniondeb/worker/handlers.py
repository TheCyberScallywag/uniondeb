"""What each kind of job does. Every handler is idempotent, because jobs are retried."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from uniondeb.agents.didolwr import EthicsJudge, tag_file
from uniondeb.agents.gwyliwr import run_scan
from uniondeb.agents.porthor import ingest_file
from uniondeb.agents.porthor.mapping import propose_mappings
from uniondeb.worker.retention import run_retention
from uniondeb.worker.services import Services

type Handler = Callable[[Services, dict[str, Any]], Awaitable[dict[str, Any]]]


async def ingest(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    """Ingest an uploaded file, then queue tagging; ask for mapping proposals if columns were unknown."""
    result = await ingest_file(
        s.runner, s.tarddiad, s.cipher, s.uploads.path(args["sha256"]), dataset=args.get("dataset")
    )
    out = result.summary()
    if result.unknown_columns and result.errors and (model := s.model("porthor")):
        out["mapping_proposals"] = len(await propose_mappings(s.runner, s.tarddiad, result, model=model))
    if result.loaded:
        out["tag_job"] = await s.queue.enqueue("tag", {"sha256": result.sha256}, unique_key=f"tag:{result.sha256}")
    return out


async def tag(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    model = s.model("didolwr")
    judge = EthicsJudge(model) if model else None  # no model: the deterministic pass still runs
    r = await tag_file(s.runner, s.tarddiad, s.echdynnwr, args["sha256"], judge=judge)
    return {k: v for k, v in r.__dict__.items() if k != "review_items"} | {
        "review_items": len(r.review_items),
        "judge": bool(judge),
    }


async def scan(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    until = datetime.fromisoformat(args["window_to"]) if args.get("window_to") else None
    r = await run_scan(
        s.runner,
        s.tarddiad,
        s.echdynnwr,
        pseudonym_key=s.pseudonym_key,
        kind=args.get("kind", "on_demand"),
        window_to=until,
        window_days=args.get("window_days"),
        baseline_days=args.get("baseline_days", 28),
        calendar=s.calendar,
    )
    return {"scan_id": r.scan_id, "alerts": len(r.alerts), "signals": len(r.findings.signals)}


async def reap(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return {"reaped": len(await s.echdynnwr.reap())}


async def retention(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    return (await run_retention(s.runner, s.tarddiad, s.flags)).model_dump(exclude={"activity_ids"})


async def audit_verify(s: Services, args: dict[str, Any]) -> dict[str, Any]:
    """Nightly: verify the audit chain, then anchor its head outside the database (design §7.4)."""
    v = await s.audit.verify()
    if not v.ok:
        raise RuntimeError(f"audit chain broken at seq {v.broken_at}: {v.reason}")  # dead job → runbook
    out = {"checked": v.checked, "head_seq": v.head_seq}
    if s.anchor_path and s.anchor_key and v.head_seq:
        await s.audit.anchor(s.anchor_path, s.anchor_key)
        out["anchored"] = True
    return out


HANDLERS: dict[str, Handler] = {
    "ingest": ingest,
    "tag": tag,
    "scan": scan,
    "reap": reap,
    "retention": retention,
    "audit_verify": audit_verify,
}
