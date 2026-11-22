"""PROV-O shaped provenance types (Tarddiad, "origin").

An ``Activity`` used some entities, generated others, and was associated with one software agent
at a given version, with the model, prompt version and config versions it ran under, and the
Logfire trace id. Entities are referred to by label and natural key, never by internal node id,
so provenance survives export, extracts and restores.

Implements: UND-015
"""

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Natural key of each entity label (plan §4). Lineage walks these, so every entity label is here.
KEY_PROPERTIES: dict[str, str] = {
    "SourceFile": "sha256",
    "Record": "key",
    "Device": "id",
    "Identifier": "value",
    "Person": "id",
    "Email": "message_id",
    "Attachment": "sha256",
    "Tag": "id",
    "ReviewItem": "id",
    "Alert": "id",
    "Extract": "id",
    "CasePack": "id",
    "Flag": "id",
    "SarDraft": "id",
    "SarSubmission": "id",
    "IngestError": "id",
    "MappingProposal": "id",
    "AliasRegistry": "id",
    "ExtractContract": "hash",
    "TriggerList": "version",
    "Case": "id",
    "CaseNote": "id",
    "AuditEvent": "id",
    "EvalReport": "id",
    "Scan": "id",
}
ROOT_LABELS = frozenset({"SourceFile", "Email", "Attachment"})  # where lineage ends: hashed inputs


class ActivityKind(StrEnum):
    UPLOAD = "Upload"
    INGEST = "Ingest"
    RESOLVE = "Resolve"
    VALIDATE = "Validate"
    TAG = "Tag"
    SCAN = "Scan"
    REVIEW = "Review"
    EXTRACT = "Extract"
    CASE_PACK = "CasePack"
    INVESTIGATE = "Investigate"
    SAR = "Sar"
    MIGRATE = "Migrate"
    MAPPING = "Mapping"
    RETENTION = "Retention"
    EXPIRE = "Expire"
    EVAL = "Eval"


class EntityRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    key: str

    @field_validator("label")
    @classmethod
    def _known_label(cls, v: str) -> str:
        if v not in KEY_PROPERTIES:
            raise ValueError(f"unknown entity label {v!r}; add it to KEY_PROPERTIES")
        return v

    @property
    def key_property(self) -> str:
        return KEY_PROPERTIES[self.label]

    def __str__(self) -> str:
        return f"{self.label}:{self.key}"


class AgentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    version: str


VERSION_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class ActivityRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid7()))
    kind: ActivityKind
    agent: AgentRef
    model: str | None = None
    prompt_version: str | None = None
    config_versions: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    status: str = "running"
    error: str | None = None
    trigger: str | None = None
    used: list[EntityRef] = Field(default_factory=list)
    generated: list[EntityRef] = Field(default_factory=list)

    @field_validator("config_versions")
    @classmethod
    def _flat_keys(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if not VERSION_KEY.match(k)]
        if bad:
            raise ValueError(f"config version keys must be snake_case identifiers: {bad}")
        return v

    def node_properties(self) -> dict[str, object]:
        """Flat properties for the ``:Activity`` node (graph properties cannot be maps)."""
        props: dict[str, object] = {
            "id": self.id,
            "kind": self.kind.value,
            "agent": self.agent.name,
            "agent_version": self.agent.version,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "status": self.status,
            "error": self.error,
            "trigger": self.trigger,
        }
        props.update({f"cfg_{k}": v for k, v in self.config_versions.items()})
        return {k: v for k, v in props.items() if v is not None}
