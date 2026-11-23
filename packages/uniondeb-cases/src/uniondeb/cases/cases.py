"""Cases: how one opens, who may work it, and when the subject's name is shown (design §7.1).

Investigators do not browse the population. A case exists only because a reviewer escalated an
alert or the nominated officer opened one with a written reason; the nominated officer assigns it,
and the conflict check refuses an investigator who is the subject, is on the subject's team, manages
or is managed by the subject, or shares a household landline with them. A case shows a pseudonym
until someone with access asks for the identity and says why.

Everything here is audited. A read is audited before anything is returned. A change to an existing
case is audited before it is written, so the log can hold an attempt whose write then failed (the
provenance activity is stored as ``failed``) but never misses a change; only opening a case writes
first, because the event links to the case and a case nobody is assigned to is inert. A case the
actor may not see raises the same ``NotFound`` as one that does not exist.
"""

import secrets
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, NoReturn, get_args

from pydantic import BaseModel, ConfigDict

from uniondeb.cases.audit import AuditLog
from uniondeb.cases.models import (
    CASE_ADMIN,
    INVESTIGATOR,
    NOMINATED_OFFICER,
    REVIEWER,
    Actor,
    CaseError,
    ConflictError,
    NotFound,
    PermissionDenied,
    now_iso,
    required,
)
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.store.provenance import ActivityHandle, Tarddiad
from uniondeb.store.runner import CypherRunner

YMCHWILIAD = AgentRef(name="ymchwiliad", version="0.1.0")
Outcome = Literal["closed_no_action", "closed_conduct_process", "sar_submitted", "referred"]
OUTCOMES: tuple[str, ...] = get_args(Outcome)

CASE_ROWS = (
    "MATCH (c:Case)-[:SUBJECT]->(p:Person) {where} "
    "OPTIONAL MATCH (c)-[r:ASSIGNED]->(i:Investigator) WHERE r.ended_at IS NULL "
    "RETURN c {{.*}} AS c, p.person_id AS subject_person_id, collect(i.id) AS investigators ORDER BY c.opened_at"
)


class Case(BaseModel):
    """What the portal shows of a case: the subject appears only as a pseudonym."""

    model_config = ConfigDict(frozen=True)

    id: str
    state: Literal["open", "closed"]
    origin: Literal["alert", "manual"]
    alert_id: str | None = None
    subject_pseudonym: str
    opened_by: str
    opened_reason: str
    opened_at: str
    window_from: str | None = None
    window_to: str | None = None
    outcome: str | None = None
    closed_by: str | None = None
    closed_at: str | None = None
    retention_review: str | None = None
    investigators: tuple[str, ...] = ()


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    person_ref: str  # the Person node's id
    name: str | None = None
    person_id: str | None = None
    team: str | None = None
    role: str | None = None


class CaseNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    case_id: str
    text: str
    author: str
    at: str


def _iso(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class CaseService:
    def __init__(self, runner: CypherRunner, audit: AuditLog, tarddiad: Tarddiad) -> None:
        self.runner = runner
        self.audit = audit
        self.tarddiad = tarddiad

    def activity(
        self, action: str, actor: Actor, kind: ActivityKind = ActivityKind.INVESTIGATE
    ) -> AbstractAsyncContextManager[ActivityHandle]:
        """The provenance activity for one state change made by ``actor``."""
        return self.tarddiad.activity(kind, agent=YMCHWILIAD, trigger=f"{action} by {actor.id}")

    # ───────────────────────────── opening ──────────────────────────────────────────

    async def open_from_alert(self, alert_id: str, actor: Actor, reason: str) -> Case:
        """A reviewer escalates an alert. The caller vouches that the four-eyes review happened."""
        reason = required(reason)
        if not actor.has(REVIEWER, NOMINATED_OFFICER):
            await self._refuse(actor, "case.open_from_alert", target=f"Alert:{alert_id}")
        rows = await self.runner.run(
            "MATCH (a:Alert {id: $id})-[:ABOUT]->(p:Person) OPTIONAL MATCH (c:Case {alert_id: $id}) "
            "RETURN a {.*} AS a, p.id AS person, count(c) AS cases",
            {"id": alert_id},
        )
        if not rows:
            raise NotFound(f"alert {alert_id} not found")
        alert = rows[0]["a"]
        if rows[0]["cases"]:
            raise CaseError(f"alert {alert_id} has already been escalated")
        return await self._open(
            actor,
            reason,
            person=rows[0]["person"],
            origin="alert",
            alert_id=alert_id,
            pseudonym=alert.get("subject_pseudonym") or _pseudonym(),
            window_from=_iso(alert.get("window_from")),
            window_to=_iso(alert.get("window_to")),
        )

    async def open_manual(
        self,
        person_ref: str,
        actor: Actor,
        reason: str,
        *,
        window_from: str | None = None,
        window_to: str | None = None,
    ) -> Case:
        """The nominated officer opens a case on a person (``Person.id`` or roster ``person_id``)."""
        reason = required(reason)
        if not actor.has(NOMINATED_OFFICER):
            await self._refuse(actor, "case.open_manual")
        rows = await self.runner.run(
            "MATCH (p:Person) WHERE p.id = $ref OR p.person_id = $ref RETURN p.id AS person", {"ref": person_ref}
        )
        if len(rows) != 1:
            raise NotFound(f"person {person_ref} not found" if not rows else f"person {person_ref} is ambiguous")
        return await self._open(
            actor,
            reason,
            person=rows[0]["person"],
            origin="manual",
            alert_id=None,
            pseudonym=_pseudonym(),
            window_from=window_from,
            window_to=window_to,
        )

    async def _open(self, actor: Actor, reason: str, *, person: str, **props: Any) -> Case:
        case_id = str(uuid.uuid7())
        params = props | {"id": case_id, "person": person, "by": actor.id, "reason": reason, "at": now_iso()}
        async with self.activity("case.open", actor) as act:
            if props["alert_id"]:
                act.used(EntityRef(label="Alert", key=props["alert_id"]))
            rows = await self.runner.run(
                "MATCH (p:Person {id: $person}) "
                "CREATE (c:Case {id: $id, state: 'open', origin: $origin, alert_id: $alert_id, "
                "subject_pseudonym: $pseudonym, opened_by: $by, opened_reason: $reason, opened_at: $at, "
                "window_from: $window_from, window_to: $window_to}) "
                "CREATE (c)-[:SUBJECT]->(p) RETURN c {.*} AS c",
                params,
            )
            if not rows:
                raise NotFound(f"person {person} not found")
            act.generated(EntityRef(label="Case", key=case_id))
        detail = {"origin": props["origin"], "alert_id": props["alert_id"]}
        await self.audit.append(actor.id, "case.open", case_id=case_id, reason=reason, detail=detail)
        return Case.model_validate(rows[0]["c"])

    # ───────────────────────────── access ───────────────────────────────────────────

    async def _cases(self, case_id: str | None = None) -> list[tuple[Case, str | None]]:
        where = "WHERE c.id = $id" if case_id else ""
        rows = await self.runner.run(CASE_ROWS.format(where=where), {"id": case_id})
        return [
            (Case.model_validate(r["c"] | {"investigators": sorted(r["investigators"])}), r["subject_person_id"])
            for r in rows
        ]

    @staticmethod
    def _allowed(case: Case, subject_person_id: str | None, actor: Actor) -> bool:
        if actor.person_id and actor.person_id == subject_person_id:
            return False  # nobody works, or learns of, a case about themselves
        if actor.has(NOMINATED_OFFICER, CASE_ADMIN):
            return True
        return actor.has(INVESTIGATOR) and actor.id in case.investigators

    async def can_access(self, case_id: str, actor: Actor) -> bool:
        """A pure check for the layer above; it is not audited and returns no content."""
        found = await self._cases(case_id)
        return bool(found) and self._allowed(*found[0], actor)

    async def require_access(self, case_id: str, actor: Actor, *, attempted: str = "case.access") -> Case:
        """The case, or ``NotFound`` (audited) whether it is missing or merely not the actor's."""
        found = await self._cases(case_id)
        if found and self._allowed(*found[0], actor):
            return found[0][0]
        await self.audit.append(
            actor.id, "case.access_denied", case_id=case_id, detail={"attempted": attempted, "exists": bool(found)}
        )
        raise NotFound(f"case {case_id} not found")

    async def _refuse(self, actor: Actor, attempted: str, *, case_id: str | None = None, **kw: Any) -> NoReturn:
        await self.audit.append(actor.id, "case.access_denied", case_id=case_id, detail={"attempted": attempted}, **kw)
        raise PermissionDenied(f"{attempted} is not permitted for roles {sorted(actor.roles)}")

    async def _require_open(self, case_id: str, actor: Actor, attempted: str) -> Case:
        case = await self.require_access(case_id, actor, attempted=attempted)
        if case.state != "open":
            raise CaseError(f"case {case_id} is closed")
        return case

    # ───────────────────────────── reading ──────────────────────────────────────────

    async def list_cases(self, actor: Actor) -> list[Case]:
        cases = [case for case, subject in await self._cases() if self._allowed(case, subject, actor)]
        await self.audit.append(actor.id, "case.list", detail={"count": len(cases)})
        return cases

    async def view(self, case_id: str, actor: Actor) -> Case:
        case = await self.require_access(case_id, actor, attempted="case.view")
        await self.audit.append(actor.id, "case.view", case_id=case_id)
        return case

    async def reveal_identity(self, case_id: str, actor: Actor, reason: str) -> Identity:
        reason = required(reason)
        await self.require_access(case_id, actor, attempted="case.reveal_identity")
        if not actor.has(INVESTIGATOR, NOMINATED_OFFICER):
            await self._refuse(actor, "case.reveal_identity", case_id=case_id)
        await self.audit.append(actor.id, "case.reveal_identity", case_id=case_id, reason=reason)
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:SUBJECT]->(p:Person) "
            "RETURN p.id AS person_ref, p.name AS name, p.person_id AS person_id, p.team AS team, p.role AS role",
            {"id": case_id},
        )
        return Identity(case_id=case_id, **rows[0])

    async def notes(self, case_id: str, actor: Actor) -> list[CaseNote]:
        await self.require_access(case_id, actor, attempted="case.notes")
        await self.audit.append(actor.id, "case.notes", case_id=case_id)
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:HAS]->(n:CaseNote) RETURN n {.*} AS n ORDER BY n.at", {"id": case_id}
        )
        return [CaseNote.model_validate(r["n"]) for r in rows]

    # ───────────────────────────── changing ─────────────────────────────────────────

    async def assign(self, case_id: str, investigator: Actor, by: Actor) -> Case:
        if not by.has(NOMINATED_OFFICER):
            await self._refuse(by, "case.assign", case_id=case_id, target=investigator.id)
        case = await self._require_open(case_id, by, "case.assign")
        if not investigator.has(INVESTIGATOR):
            raise PermissionDenied(f"{investigator.id} does not hold the investigator role")
        if investigator.id in case.investigators:
            raise CaseError(f"{investigator.id} is already assigned to case {case_id}")
        if rules := await self._conflicts(case_id, investigator):
            await self.audit.append(
                by.id, "case.assign_blocked", case_id=case_id, target=investigator.id, detail={"rules": rules}
            )
            raise ConflictError(rules)
        await self.audit.append(by.id, "case.assign", case_id=case_id, target=investigator.id)
        async with self.activity("case.assign", by) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run(
                "MATCH (c:Case {id: $id}) MERGE (i:Investigator {id: $investigator}) "
                "CREATE (c)-[:ASSIGNED {by: $by, at: $at}]->(i)",
                {"id": case_id, "investigator": investigator.id, "by": by.id, "at": now_iso()},
            )
        return case.model_copy(update={"investigators": (*case.investigators, investigator.id)})

    async def _conflicts(self, case_id: str, investigator: Actor) -> list[str]:
        """Every conflict rule that fires. An investigator who cannot be checked is refused."""
        if not investigator.person_id:
            return ["no_person_id"]
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:SUBJECT]->(s:Person) MATCH (i:Person {person_id: $pid}) "
            "OPTIONAL MATCH (s)-[:USES]->(d:Device {kind: 'landline'})<-[:USES]-(i) "
            "RETURN s {.id, .person_id, .team, .manager_id} AS s, i {.id, .person_id, .team, .manager_id} AS i, "
            "count(d) AS landlines",
            {"id": case_id, "pid": investigator.person_id},
        )
        if not rows:
            return ["not_in_roster"]
        rules: set[str] = set()
        for row in rows:
            s, i = row["s"], row["i"]
            same = s["id"] == i["id"]
            checks = {
                "is_subject": same,
                "same_team": s.get("team") is not None and s.get("team") == i.get("team"),
                "line_manager_of_subject": s.get("manager_id") is not None and s.get("manager_id") == i["person_id"],
                "managed_by_subject": i.get("manager_id") is not None and i.get("manager_id") == s.get("person_id"),
                "shared_household_landline": not same and row["landlines"] > 0,
            }
            rules.update(name for name, fired in checks.items() if fired)
        return sorted(rules)

    async def unassign(self, case_id: str, investigator_id: str, by: Actor, reason: str) -> Case:
        """End an assignment. The relationship stays, with ``ended_at``: history is not deleted."""
        reason = required(reason)
        if not by.has(NOMINATED_OFFICER):
            await self._refuse(by, "case.unassign", case_id=case_id, target=investigator_id)
        case = await self.require_access(case_id, by, attempted="case.unassign")
        if investigator_id not in case.investigators:
            raise CaseError(f"{investigator_id} is not assigned to case {case_id}")
        await self.audit.append(by.id, "case.unassign", case_id=case_id, target=investigator_id, reason=reason)
        async with self.activity("case.unassign", by) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run(
                "MATCH (:Case {id: $id})-[r:ASSIGNED]->(:Investigator {id: $investigator}) WHERE r.ended_at IS NULL "
                "SET r.ended_at = $at, r.ended_by = $by, r.ended_reason = $reason",
                {"id": case_id, "investigator": investigator_id, "by": by.id, "at": now_iso(), "reason": reason},
            )
        return case.model_copy(update={"investigators": tuple(i for i in case.investigators if i != investigator_id)})

    async def add_note(self, case_id: str, actor: Actor, text: str) -> CaseNote:
        text = required(text, "note")
        await self._require_open(case_id, actor, "case.note")
        note = CaseNote(id=str(uuid.uuid7()), case_id=case_id, text=text, author=actor.id, at=now_iso())
        await self.audit.append(actor.id, "case.note", case_id=case_id, target=f"CaseNote:{note.id}")
        async with self.activity("case.note", actor) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run(
                "MATCH (c:Case {id: $case_id}) "
                "CREATE (n:CaseNote {id: $id, case_id: $case_id, text: $text, author: $author, at: $at}) "
                "CREATE (c)-[:HAS]->(n)",
                note.model_dump(),
            )
            act.generated(EntityRef(label="CaseNote", key=note.id))
        return note

    async def close(self, case_id: str, actor: Actor, outcome: str, retention_review: str) -> Case:
        if not actor.has(NOMINATED_OFFICER):
            await self._refuse(actor, "case.close", case_id=case_id)
        if outcome not in OUTCOMES:
            raise CaseError(f"outcome must be one of {', '.join(OUTCOMES)}")
        retention_review = required(retention_review, "retention review")
        case = await self._require_open(case_id, actor, "case.close")
        if outcome == "sar_submitted" and not await self.runner.run(
            "MATCH (:Case {id: $id})-[:REPORTED]->(s:SarSubmission) RETURN s.id AS id LIMIT 1", {"id": case_id}
        ):
            raise CaseError("no SAR has been submitted for this case: approve a draft first, or choose another outcome")
        closed = {"state": "closed", "outcome": outcome, "closed_by": actor.id, "closed_at": now_iso()}
        closed["retention_review"] = retention_review
        await self.audit.append(
            actor.id, "case.close", case_id=case_id, reason=retention_review, detail={"outcome": outcome}
        )
        async with self.activity("case.close", actor) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run("MATCH (c:Case {id: $id}) SET c += $closed", {"id": case_id, "closed": closed})
        return case.model_copy(update=closed)


def _pseudonym() -> str:
    """Random, so it cannot be reversed by hashing candidate names; the graph holds the link."""
    return f"SUBJ-{secrets.token_hex(5).upper()}"
