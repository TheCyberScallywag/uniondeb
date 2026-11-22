"""Flags from a controlled, versioned vocabulary, and the legal hold they can place (plan §7.3).

A flag is a node saying who set which label on what, when, why and under which case, so its
history is never lost: a flag is not edited, and clearing one only marks it inactive with who and
why. Labels come from a packaged YAML vocabulary; anything outside it is refused and free text
goes in case notes.

Three audiences read flags and get different things. Inside the case, everything. Outside it
(``public_flags``), never a restricted flag and never the case, the person who set it or the
reason: person and case flags must not tip anyone off. Gwyliwr (``device_hypotheses``) gets device
flags only, marked as the hypotheses they are. A case with an active ``legal_hold`` puts every
record involving the subject's devices on hold for the retention job.

Implements: UND-072
"""

import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from uniondeb.cases.audit import AuditLog
from uniondeb.cases.cases import CaseService
from uniondeb.cases.models import Actor, FlagError, NotFound, now_iso, required
from uniondeb.core.prov import EntityRef
from uniondeb.store.runner import CypherRunner

FlagKind = Literal["person", "device", "case"]
TARGET_LABELS: dict[str, frozenset[str]] = {
    "person": frozenset({"Person"}),
    "device": frozenset({"Device", "Identifier"}),
    "case": frozenset({"Case"}),
}


class FlagDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    kind: FlagKind
    restricted: bool = False
    hypothesis: bool = False
    param: Literal["person"] | None = None  # what the flag's parameter must name, if it takes one
    description: str = ""

    @model_validator(mode="after")
    def _kind_rules(self) -> FlagDef:
        if self.kind in ("person", "case") and not self.restricted:
            raise ValueError(f"{self.kind} flag {self.label!r} must be restricted")
        if self.kind == "device" and not self.hypothesis:
            raise ValueError(f"device flag {self.label!r} must be marked hypothesis")
        return self


class FlagVocabulary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    flags: tuple[FlagDef, ...]

    @field_validator("version", mode="before")
    @classmethod
    def _version_text(cls, v: Any) -> str:
        return str(v)

    @model_validator(mode="after")
    def _unique_labels(self) -> FlagVocabulary:
        labels = [f.label for f in self.flags]
        if dupes := sorted({x for x in labels if labels.count(x) > 1}):
            raise ValueError(f"duplicate flag labels: {dupes}")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> FlagVocabulary:
        source = Path(path) if path else files("uniondeb.cases") / "config" / "flags.v1.yaml"
        return cls.model_validate(yaml.safe_load(source.read_text("utf-8")))

    def check(self, label: str, target: EntityRef, param: str | None = None) -> FlagDef:
        """The definition of ``label``, or ``FlagError`` if it may not be set this way."""
        definition = next((f for f in self.flags if f.label == label), None)
        if definition is None:
            raise FlagError(f"{label!r} is not in flags vocabulary v{self.version}")
        if target.label not in TARGET_LABELS[definition.kind]:
            allowed = " or ".join(sorted(TARGET_LABELS[definition.kind]))
            raise FlagError(f"{label!r} is a {definition.kind} flag: it goes on a {allowed}, not a {target.label}")
        if definition.param and not (param and param.strip()):
            raise FlagError(f"{label!r} needs a {definition.param} parameter")
        if param is not None and not definition.param:
            raise FlagError(f"{label!r} takes no parameter")
        return definition


class Flag(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    kind: FlagKind
    vocabulary_version: str
    restricted: bool
    hypothesis: bool
    param: str | None = None
    reason: str
    set_by: str
    set_at: str
    expires_at: str | None = None
    case_id: str
    active: bool = True
    target_label: str
    target_key: str
    cleared_by: str | None = None
    cleared_at: str | None = None
    cleared_reason: str | None = None

    @property
    def live(self) -> bool:
        return self.active and (self.expires_at is None or datetime.fromisoformat(self.expires_at) > datetime.now(UTC))


class PublicFlag(BaseModel):
    """A flag as seen outside its case: no case, no author, no reason."""

    model_config = ConfigDict(frozen=True)

    label: str
    kind: FlagKind
    hypothesis: bool
    param: str | None = None
    target: EntityRef
    set_at: str
    expires_at: str | None = None

    @classmethod
    def of(cls, flag: Flag) -> PublicFlag:
        target = EntityRef(label=flag.target_label, key=flag.target_key)
        return cls(target=target, **flag.model_dump(include=set(cls.model_fields) - {"target"}))


class FlagService:
    def __init__(
        self, runner: CypherRunner, audit: AuditLog, cases: CaseService, vocabulary: FlagVocabulary | None = None
    ) -> None:
        self.runner = runner
        self.audit = audit
        self.cases = cases
        self.vocabulary = vocabulary or FlagVocabulary.load()

    async def set_flag(
        self,
        case_id: str,
        actor: Actor,
        label: str,
        *,
        target: EntityRef | None = None,
        reason: str,
        param: str | None = None,
        expires_at: datetime | None = None,
    ) -> Flag:
        """Set ``label`` on ``target`` (the case itself when omitted) under the case."""
        reason = required(reason)
        await self.cases.require_access(case_id, actor, attempted="flag.set")
        target = target or EntityRef(label="Case", key=case_id)
        definition = self.vocabulary.check(label, target, param)
        if definition.kind == "case" and target.key != case_id:
            raise FlagError("a case flag goes on its own case")
        if expires_at is not None and expires_at.tzinfo is None:
            raise FlagError("expires_at must be timezone-aware")
        if not await self._exists(target):
            raise FlagError(f"{target} does not exist")
        if definition.param == "person":
            param = await self._person(param or "")
        flag = Flag(
            id=str(uuid.uuid7()),
            label=label,
            kind=definition.kind,
            vocabulary_version=self.vocabulary.version,
            restricted=definition.restricted,
            hypothesis=definition.hypothesis,
            param=param,
            reason=reason,
            set_by=actor.id,
            set_at=now_iso(),
            expires_at=expires_at.astimezone(UTC).isoformat() if expires_at else None,
            case_id=case_id,
            target_label=target.label,
            target_key=target.key,
        )
        detail = {"flag_id": flag.id, "label": label, "param": param, "expires_at": flag.expires_at}
        await self.audit.append(actor.id, "flag.set", case_id=case_id, target=str(target), reason=reason, detail=detail)
        async with self.cases.activity("flag.set", actor) as act:
            act.used(EntityRef(label="Case", key=case_id))
            await self.runner.run(
                f"MATCH (c:Case {{id: $case_id}}) MATCH (t:`{target.label}` {{`{target.key_property}`: $target_key}}) "
                "WITH c, t LIMIT 1 "
                "CREATE (f:Flag {id: $id, label: $label, kind: $kind, vocabulary_version: $vocabulary_version, "
                "restricted: $restricted, hypothesis: $hypothesis, param: $param, reason: $reason, set_by: $set_by, "
                "set_at: $set_at, expires_at: $expires_at, case_id: $case_id, active: true, "
                "target_label: $target_label, target_key: $target_key}) "
                "CREATE (c)-[:HAS]->(f) CREATE (f)-[:ON]->(t)",
                flag.model_dump(),
            )
            act.generated(EntityRef(label="Flag", key=flag.id))
        return flag

    async def _exists(self, ref: EntityRef) -> bool:
        rows = await self.runner.run(
            f"MATCH (t:`{ref.label}` {{`{ref.key_property}`: $key}}) RETURN count(t) AS n", {"key": ref.key}
        )
        return bool(rows and rows[0]["n"])

    async def _person(self, ref: str) -> str:
        rows = await self.runner.run(
            "MATCH (p:Person) WHERE p.id = $ref OR p.person_id = $ref RETURN p.id AS id", {"ref": ref.strip()}
        )
        if len(rows) != 1:
            raise FlagError(f"parameter {ref!r} does not name one person")
        return rows[0]["id"]

    async def clear_flag(self, flag_id: str, actor: Actor, reason: str) -> Flag:
        """Mark a flag inactive. This is the only change a flag ever sees, and the node remains."""
        reason = required(reason)
        rows = await self.runner.run("MATCH (f:Flag {id: $id}) RETURN f {.*} AS f", {"id": flag_id})
        if not rows:
            await self.audit.append(actor.id, "case.access_denied", detail={"attempted": "flag.clear", "exists": False})
            raise NotFound(f"flag {flag_id} not found")
        flag = Flag.model_validate(rows[0]["f"])
        try:
            await self.cases.require_access(flag.case_id, actor, attempted="flag.clear")
        except NotFound:
            raise NotFound(f"flag {flag_id} not found") from None
        if not flag.active:
            raise FlagError(f"flag {flag_id} is already cleared")
        cleared = {"active": False, "cleared_by": actor.id, "cleared_at": now_iso(), "cleared_reason": reason}
        await self.audit.append(
            actor.id,
            "flag.clear",
            case_id=flag.case_id,
            target=f"Flag:{flag_id}",
            reason=reason,
            detail={"label": flag.label},
        )
        async with self.cases.activity("flag.clear", actor) as act:
            act.used(EntityRef(label="Case", key=flag.case_id), EntityRef(label="Flag", key=flag_id))
            await self.runner.run("MATCH (f:Flag {id: $id}) SET f += $cleared", {"id": flag_id, "cleared": cleared})
        return flag.model_copy(update=cleared)

    async def flags_for_case(self, case_id: str, actor: Actor) -> list[Flag]:
        """Every flag of the case, cleared and expired ones included (``Flag.live`` tells them apart)."""
        await self.cases.require_access(case_id, actor, attempted="flag.list")
        await self.audit.append(actor.id, "flag.list", case_id=case_id)
        rows = await self.runner.run(
            "MATCH (:Case {id: $id})-[:HAS]->(f:Flag) RETURN f {.*} AS f ORDER BY f.set_at", {"id": case_id}
        )
        return [Flag.model_validate(r["f"]) for r in rows]

    async def public_flags(self, target: EntityRef) -> list[PublicFlag]:
        """What dashboards and reviewers may see on ``target``: never a restricted flag."""
        rows = await self.runner.run(
            f"MATCH (f:Flag {{active: true, restricted: false}})-[:ON]->"
            f"(:`{target.label}` {{`{target.key_property}`: $key}}) RETURN f {{.*}} AS f ORDER BY f.set_at",
            {"key": target.key},
        )
        return self._public(rows)

    async def device_hypotheses(self) -> list[PublicFlag]:
        """Device and identifier flags for the security scanner: hypotheses, never facts."""
        rows = await self.runner.run(
            "MATCH (f:Flag {active: true, restricted: false, kind: 'device', hypothesis: true}) "
            "RETURN f {.*} AS f ORDER BY f.set_at"
        )
        return self._public(rows)

    @staticmethod
    def _public(rows: list[dict[str, Any]]) -> list[PublicFlag]:
        flags = (Flag.model_validate(r["f"]) for r in rows)
        return [PublicFlag.of(f) for f in flags if f.live and not f.restricted]

    # ───────────────────────────── legal hold ───────────────────────────────────────

    async def records_on_hold(self) -> set[str]:
        """Keys of every record the retention job must not purge.

        A case with a live ``legal_hold`` holds each record involving a device its subject uses,
        within the case window when it has one.
        """
        rows = await self.runner.run(
            "MATCH (c:Case)-[:HAS]->(f:Flag {label: 'legal_hold', active: true}) RETURN c {.*} AS c, f {.*} AS f"
        )
        held: set[str] = set()
        for case in {r["c"]["id"]: r["c"] for r in rows if Flag.model_validate(r["f"]).live}.values():
            records = await self.runner.run(
                "MATCH (:Case {id: $id})-[:SUBJECT]->(:Person)-[:USES]->(:Device)<-[:FROM|TO|ON|SENT_FROM]-(r:Record) "
                "RETURN DISTINCT r.key AS key, r.occurred_at AS at",
                {"id": case["id"]},
            )
            lo, hi = _when(case.get("window_from")), _when(case.get("window_to"))
            held.update(r["key"] for r in records if _within(_when(r["at"]), lo, hi))
        return held

    async def is_on_hold(self, record_key: str) -> bool:
        return record_key in await self.records_on_hold()


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _within(at: datetime | None, lo: datetime | None, hi: datetime | None) -> bool:
    if at is None:
        return True  # an undated record cannot be shown to be outside the window: hold it
    return (lo is None or at >= lo) and (hi is None or at <= hi)
