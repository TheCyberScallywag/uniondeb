"""The alias registry: header mappings approved by a human, kept as versioned data.

The alias layer in ``uniondeb.core.resolve`` is code and covers the dialects known when it was
written. When a source system names a column in a way it does not know, Porthor proposes a mapping,
a reviewer approves it, and it lands here as registry version N+1. The registry only *renames*
columns to names the contract already has: it cannot add a field or change a validation rule.

Implements: UND-023
"""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from uniondeb.core.mail import EmailEvent
from uniondeb.core.resolve import normalize
from uniondeb.core.schemas import DATASETS
from uniondeb.store.runner import CypherRunner


def canonical_fields(dataset: str) -> dict[str, str]:
    """Names a column may be mapped to, with what each means."""
    if dataset == "email":
        return {n: (f.description or n) for n, f in EmailEvent.model_fields.items()}
    return {c.name: c.description for c in DATASETS[dataset].columns}


class AliasRegistry(BaseModel):
    version: int = 0
    mappings: dict[str, dict[str, str]] = Field(default_factory=dict)  # dataset → normalised column → canonical

    def apply(self, dataset: str, row: dict[str, Any]) -> dict[str, Any]:
        table = self.mappings.get(dataset)
        if not table:
            return row
        out: dict[str, Any] = {}
        for key, value in row.items():
            target = table.get(normalize(key))
            # An approved alias never overrides a column the source already sends under its real name.
            out[target if target and target not in row else key] = value
        return out

    @classmethod
    async def current(cls, runner: CypherRunner) -> AliasRegistry:
        rows = await runner.run(
            "MATCH (a:AliasRegistry) RETURN a.version AS version, a.mappings_json AS m ORDER BY version DESC LIMIT 1"
        )
        return cls(version=rows[0]["version"], mappings=json.loads(rows[0]["m"])) if rows else cls()

    async def extend(
        self, runner: CypherRunner, dataset: str, new: dict[str, str], *, approved_by: str
    ) -> AliasRegistry:
        """Store version N+1 with ``new`` (column → canonical field) added for ``dataset``."""
        allowed = canonical_fields(dataset)
        if bad := sorted(set(new.values()) - set(allowed)):
            raise ValueError(f"{bad} are not fields of the {dataset} contract")
        merged = {k: dict(v) for k, v in self.mappings.items()}
        merged.setdefault(dataset, {}).update({normalize(c): f for c, f in new.items()})
        nxt = AliasRegistry(version=self.version + 1, mappings=merged)
        await runner.run(
            "CREATE (a:AliasRegistry {id: 'v' + toString($version), version: $version, mappings_json: $m, parent: $parent, "
            "approved_by: $by, created_at: $at, dataset: $dataset, added_json: $added})",
            {
                "version": nxt.version,
                "m": json.dumps(merged, sort_keys=True),
                "parent": self.version,
                "by": approved_by,
                "at": datetime.now(UTC).isoformat(),
                "dataset": dataset,
                "added": json.dumps(new, sort_keys=True),
            },
        )
        return nxt
