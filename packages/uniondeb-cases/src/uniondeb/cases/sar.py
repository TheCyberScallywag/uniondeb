"""Suspicious Activity Reports: the payload, a mock portal, and second-person approval (plan §7.5).

``SarPayload`` holds what the NCA's SAR Portal guidance asks for: structured subject fields where
anything not known is the literal ``UNKNOWN`` (never a blank or a symbol), and a reason for
suspicion of at most 8,000 characters that starts with its glossary codes. The codes are
configuration loaded from a packaged file, to be replaced from the NCA's current booklet. Linked
record keys and provenance references stay inside: ``submission_document`` is what is sent.

Submission is mocked: ``MockSarPortal`` applies the same validation and returns a ``MOCK-SAR-``
reference without touching the network. One person drafts and a different person holding
``nominated_officer`` approves; on approval the frozen payload, its SHA-256, the receipt and both
names are stored, and the case gets the ``sar_submitted`` flag. The reference is written to the
audit log before the copy is stored, so a receipt is never lost to a failed write. Submissions
are readable by nominated officers and auditors only.

Implements: UND-074
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from uniondeb.cases.audit import AuditLog
from uniondeb.cases.cases import CaseService
from uniondeb.cases.flags import FlagService
from uniondeb.cases.models import (
    AUDITOR,
    NOMINATED_OFFICER,
    Actor,
    NotFound,
    PermissionDenied,
    SarError,
    SarValidationError,
    now_iso,
)
from uniondeb.core.prov import ActivityKind, EntityRef
from uniondeb.store.runner import CypherRunner

UNKNOWN = "UNKNOWN"
REASON_MAX = 8000
CODE = re.compile(r"^XX[A-Z0-9]+XX$")
_PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "none", "null", "nil", "tbc", "tba", "not known", "not applicable"})


class SarGlossary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    version: str
    codes: dict[str, str]  # code → meaning

    @field_validator("codes")
    @classmethod
    def _code_format(cls, v: dict[str, str]) -> dict[str, str]:
        if bad := sorted(c for c in v if not CODE.match(c)):
            raise ValueError(f"glossary codes must look like XX…XX: {bad}")
        return v

    @classmethod
    def load(cls, path: str | Path | None = None) -> SarGlossary:
        source = Path(path) if path else files("uniondeb.cases") / "config" / "sar_glossary.yaml"
        return cls.model_validate(yaml.safe_load(source.read_text("utf-8")))


@cache
def default_glossary() -> SarGlossary:
    return SarGlossary.load()


def _stated(value: str) -> str:
    """A real value or the literal ``UNKNOWN``; nothing that merely stands for "don't know"."""
    text = value.strip()
    if text == UNKNOWN:
        return text
    if not text or text.casefold() in _PLACEHOLDERS or not any(ch.isalnum() for ch in text):
        raise ValueError(f"write the literal {UNKNOWN} for anything not known, not {value!r}")
    return text


Stated = Annotated[str, AfterValidator(_stated)]


def _birth_date(value: str) -> str:
    if value != UNKNOWN:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"date of birth must be YYYY-MM-DD or the literal {UNKNOWN}") from None
    return value


class SarRoute(StrEnum):
    NCA_SAR = "nca_sar"
    POLICE_REFERRAL = "police_referral"
    FCA_REPORT = "fca_report"


class SarSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Stated
    date_of_birth: Annotated[Stated, AfterValidator(_birth_date)]
    address: Stated
    identifiers: Annotated[tuple[Stated, ...], Field(min_length=1)]  # (UNKNOWN,) when there are none
    occupation: Stated


class SarPayload(BaseModel):
    """Validate with ``context={"glossary": SarGlossary}`` to use a list other than the packaged one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: SarRoute = SarRoute.NCA_SAR
    subject: SarSubject
    glossary_codes: Annotated[tuple[str, ...], Field(min_length=1)]
    reason_for_suspicion: str
    record_keys: tuple[str, ...] = ()  # internal: never sent
    provenance_refs: tuple[str, ...] = ()  # internal: never sent

    @field_validator("glossary_codes")
    @classmethod
    def _known_codes(cls, v: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        glossary = (info.context or {}).get("glossary") or default_glossary()
        for code in v:
            if not CODE.match(code):
                raise ValueError(f"glossary code {code!r} must match XX…XX in capitals and digits")
            if code not in glossary.codes:
                raise ValueError(f"glossary code {code!r} is not in the configured list ({glossary.version})")
        return v

    @field_validator("reason_for_suspicion")
    @classmethod
    def _reason_length(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason for suspicion is required")
        if len(v) > REASON_MAX:
            raise ValueError(f"reason for suspicion is {len(v):,} characters; the limit is {REASON_MAX:,}")
        return v

    @model_validator(mode="after")
    def _reason_starts_with_codes(self) -> SarPayload:
        opening = [w.strip(".,;:-") for w in self.reason_for_suspicion.split()[: len(self.glossary_codes)]]
        if opening != list(self.glossary_codes):
            raise ValueError(
                f"reason for suspicion must start with its glossary codes: {' '.join(self.glossary_codes)}"
            )
        return self

    def submission_document(self) -> dict[str, Any]:
        """What is sent: everything except the internal record and provenance links."""
        return self.model_dump(mode="json", exclude={"record_keys", "provenance_refs"})

    def frozen_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_payload(fields: dict[str, Any], glossary: SarGlossary | None = None) -> SarPayload:
    """``fields`` as a ``SarPayload``, or ``SarValidationError`` listing every problem."""
    try:
        return SarPayload.model_validate(fields, context={"glossary": glossary})
    except ValidationError as exc:
        problems = [f"{'.'.join(map(str, e['loc'])) or 'payload'}: {e['msg']}" for e in exc.errors()]
        raise SarValidationError(problems) from None


class SarReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    acknowledged_at: datetime
    mock: bool = False


class SarClient(Protocol):
    async def submit(self, payload: SarPayload) -> SarReceipt: ...


class MockSarPortal:
    """The default client: no network, same rules."""

    def __init__(self, glossary: SarGlossary | None = None) -> None:
        self.glossary = glossary

    async def submit(self, payload: SarPayload) -> SarReceipt:
        validate_payload(payload.model_dump(mode="json", warnings=False), self.glossary)
        return SarReceipt(reference=f"MOCK-SAR-{uuid.uuid7()}", acknowledged_at=datetime.now(UTC), mock=True)


class SarDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    case_id: str
    state: str  # draft | approved (being submitted) | submitted
    payload_json: str
    drafted_by: str
    drafted_at: str
    updated_at: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


class SarSubmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    case_id: str
    draft_id: str
    payload_json: str
    payload_sha256: str
    reference: str
    mock: bool
    route: str
    drafted_by: str
    approved_by: str
    submitted_at: str
    acknowledged_at: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


class SarService:
    def __init__(
        self,
        runner: CypherRunner,
        audit: AuditLog,
        cases: CaseService,
        flags: FlagService,
        client: SarClient | None = None,
        glossary: SarGlossary | None = None,
    ) -> None:
        self.runner = runner
        self.audit = audit
        self.cases = cases
        self.flags = flags
        self.glossary = glossary
        self.client = client or MockSarPortal(glossary)

    # ───────────────────────────── drafting ─────────────────────────────────────────

    async def draft(self, case_id: str, actor: Actor, fields: dict[str, Any]) -> SarDraft:
        """Start a draft. ``fields`` may be incomplete: it is validated at approval."""
        case = await self.cases.require_access(case_id, actor, attempted="sar.draft")
        if case.state != "open":
            raise SarError(f"case {case_id} is closed")
        draft = SarDraft(
            id=str(uuid.uuid7()),
            case_id=case_id,
            state="draft",
            payload_json=_json(fields),
            drafted_by=actor.id,
            drafted_at=now_iso(),
        )
        await self.audit.append(actor.id, "sar.draft", case_id=case_id, target=f"SarDraft:{draft.id}")
        async with self.cases.activity("sar.draft", actor, ActivityKind.SAR) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run(
                "MATCH (c:Case {id: $case_id}) "
                "CREATE (d:SarDraft {id: $id, case_id: $case_id, state: $state, payload_json: $payload_json, "
                "drafted_by: $drafted_by, drafted_at: $drafted_at}) CREATE (c)-[:HAS]->(d)",
                draft.model_dump(),
            )
            act.generated(EntityRef(label="SarDraft", key=draft.id))
        return draft

    async def _draft(self, draft_id: str, actor: Actor, attempted: str) -> SarDraft:
        rows = await self.runner.run("MATCH (d:SarDraft {id: $id}) RETURN d {.*} AS d", {"id": draft_id})
        if not rows:
            await self.audit.append(actor.id, "case.access_denied", detail={"attempted": attempted, "exists": False})
            raise NotFound(f"SAR draft {draft_id} not found")
        draft = SarDraft.model_validate(rows[0]["d"])
        try:
            await self.cases.require_access(draft.case_id, actor, attempted=attempted)
        except NotFound:
            raise NotFound(f"SAR draft {draft_id} not found") from None
        return draft

    async def update_draft(self, draft_id: str, actor: Actor, fields: dict[str, Any]) -> SarDraft:
        """Replace the draft's payload. Only its drafter may, so the approver is always a second person."""
        draft = await self._draft(draft_id, actor, "sar.update_draft")
        if actor.id != draft.drafted_by:
            await self.audit.append(actor.id, "sar.update_denied", case_id=draft.case_id, target=f"SarDraft:{draft_id}")
            raise PermissionDenied("only the drafter edits a SAR draft")
        if draft.state != "draft":
            raise SarError(f"SAR draft {draft_id} is {draft.state} and can no longer be edited")
        update = {"payload_json": _json(fields), "updated_at": now_iso()}
        await self.audit.append(actor.id, "sar.update_draft", case_id=draft.case_id, target=f"SarDraft:{draft_id}")
        rows = await self.runner.run(
            "MATCH (d:SarDraft {id: $id}) WHERE d.state = 'draft' SET d += $update RETURN d.id AS id",
            {"id": draft_id, "update": update},
        )
        if not rows:
            raise SarError(f"SAR draft {draft_id} can no longer be edited")
        return draft.model_copy(update=update)

    async def get_draft(self, draft_id: str, actor: Actor) -> SarDraft:
        draft = await self._draft(draft_id, actor, "sar.view_draft")
        await self.audit.append(actor.id, "sar.view_draft", case_id=draft.case_id, target=f"SarDraft:{draft_id}")
        return draft

    async def drafts(self, case_id: str, actor: Actor) -> list[SarDraft]:
        await self.cases.require_access(case_id, actor, attempted="sar.list_drafts")
        await self.audit.append(actor.id, "sar.list_drafts", case_id=case_id)
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:HAS]->(d:SarDraft) RETURN d {.*} AS d ORDER BY d.drafted_at", {"id": case_id}
        )
        return [SarDraft.model_validate(r["d"]) for r in rows]

    # ───────────────────────────── approval and submission ──────────────────────────

    async def approve_and_submit(self, draft_id: str, approver: Actor) -> SarSubmission:
        target = f"SarDraft:{draft_id}"
        if not approver.has(NOMINATED_OFFICER):
            await self.audit.append(approver.id, "sar.approval_denied", target=target, detail={"why": "role"})
            raise PermissionDenied("only a nominated officer approves a SAR")
        draft = await self._draft(draft_id, approver, "sar.approve")
        case_id = draft.case_id
        if approver.id == draft.drafted_by:
            await self.audit.append(
                approver.id, "sar.approval_denied", case_id=case_id, target=target, detail={"why": "self_approval"}
            )
            raise PermissionDenied("a SAR is approved by a second person, not its drafter")
        if draft.state != "draft":
            raise SarError(f"SAR draft {draft_id} is already {draft.state}")
        try:
            payload = validate_payload(draft.payload, self.glossary)
        except SarValidationError as exc:
            await self.audit.append(
                approver.id, "sar.validation_failed", case_id=case_id, target=target, detail={"errors": exc.errors}
            )
            raise
        frozen = payload.frozen_json()
        sha = hashlib.sha256(frozen.encode()).hexdigest()
        await self.audit.append(
            approver.id,
            "sar.approve",
            case_id=case_id,
            target=target,
            detail={"payload_sha256": sha, "drafted_by": draft.drafted_by, "route": payload.route.value},
        )
        claimed = await self.runner.run(
            "MATCH (d:SarDraft {id: $id}) WHERE d.state = 'draft' AND d.payload_json = $payload_json "
            "SET d.state = 'approved', d.approved_by = $by, d.approved_at = $at RETURN d.id AS id",
            {"id": draft_id, "payload_json": draft.payload_json, "by": approver.id, "at": now_iso()},
        )
        if not claimed:
            raise SarError(f"SAR draft {draft_id} changed while it was being approved")
        try:
            receipt = await self.client.submit(payload)
        except Exception as exc:
            await self.runner.run("MATCH (d:SarDraft {id: $id}) SET d.state = 'draft'", {"id": draft_id})
            await self.audit.append(
                approver.id, "sar.submit_failed", case_id=case_id, target=target, detail={"error": type(exc).__name__}
            )
            raise
        submission = SarSubmission(
            id=str(uuid.uuid7()),
            case_id=case_id,
            draft_id=draft_id,
            payload_json=frozen,
            payload_sha256=sha,
            reference=receipt.reference,
            mock=receipt.mock,
            route=payload.route.value,
            drafted_by=draft.drafted_by,
            approved_by=approver.id,
            submitted_at=now_iso(),
            acknowledged_at=receipt.acknowledged_at.astimezone(UTC).isoformat(),
        )
        await self.audit.append(
            approver.id,
            "sar.submit",
            case_id=case_id,
            target=f"SarSubmission:{submission.id}",
            detail={"reference": receipt.reference, "mock": receipt.mock, "payload_sha256": sha, "draft_id": draft_id},
        )
        async with self.cases.activity("sar.submit", approver, ActivityKind.SAR) as act:
            act.used(EntityRef(label="Case", key=case_id), EntityRef(label="SarDraft", key=draft_id))
            await self.runner.run(
                "MATCH (c:Case {id: $case_id}) MATCH (d:SarDraft {id: $draft_id}) "
                "CREATE (s:SarSubmission {id: $id, case_id: $case_id, draft_id: $draft_id, payload_json: $payload_json, "
                "payload_sha256: $payload_sha256, reference: $reference, mock: $mock, route: $route, "
                "drafted_by: $drafted_by, approved_by: $approved_by, submitted_at: $submitted_at, "
                "acknowledged_at: $acknowledged_at}) "
                "CREATE (c)-[:REPORTED]->(s) CREATE (s)-[:FROM_DRAFT]->(d) SET d.state = 'submitted'",
                submission.model_dump(),
            )
            act.generated(EntityRef(label="SarSubmission", key=submission.id))
        await self.flags.set_flag(case_id, approver, "sar_submitted", reason=f"SAR submitted: {receipt.reference}")
        return submission

    async def submissions(self, case_id: str, actor: Actor) -> list[SarSubmission]:
        """The stored copies, for nominated officers and auditors only."""
        if not actor.has(NOMINATED_OFFICER, AUDITOR):
            await self.audit.append(
                actor.id, "case.access_denied", case_id=case_id, detail={"attempted": "sar.submissions"}
            )
            raise PermissionDenied("SAR submissions are visible to nominated officers and auditors only")
        await self.audit.append(actor.id, "sar.submissions", case_id=case_id)
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:REPORTED]->(s:SarSubmission) RETURN s {.*} AS s ORDER BY s.submitted_at",
            {"id": case_id},
        )
        return [SarSubmission.model_validate(r["s"]) for r in rows]


def _json(fields: dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, default=str, ensure_ascii=False)
