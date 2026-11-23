"""Connection settings for AuraDB, a local Neo4j Enterprise, or an embedded Relate file.

Settings come from ``UNIONDEB_NEO4J_*`` environment variables. ``relate:<path>`` (or
``relate::memory:``) selects the embedded Relate engine, used for local tests and agent extracts.
"""

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

NEO4J_SCHEMES = ("neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc")
ENV_PREFIX = "UNIONDEB_NEO4J_"


class StoreSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str = "neo4j://localhost:7687"
    user: str = "neo4j"
    password: SecretStr = SecretStr("")
    database: str = "neo4j"
    synthetic_database: str = "uniondeb-synthetic"
    synthetic: bool = Field(default=False, description="Target the synthetic database instead")

    @field_validator("uri")
    @classmethod
    def _known_scheme(cls, v: str) -> str:
        if v.startswith("relate:"):
            return v
        scheme = v.split("://", 1)[0]
        if "://" not in v or scheme not in NEO4J_SCHEMES:
            raise ValueError(f"unsupported URI {v!r}: use one of {', '.join(NEO4J_SCHEMES)} or relate:<path>")
        return v

    @property
    def backend(self) -> Literal["neo4j", "relate"]:
        return "relate" if self.uri.startswith("relate:") else "neo4j"

    @property
    def relate_path(self) -> str:
        return self.uri.removeprefix("relate:").removeprefix("//") or ":memory:"

    @property
    def is_aura(self) -> bool:
        return ".databases.neo4j.io" in self.uri

    @property
    def target_database(self) -> str:
        """Synthetic runs never share a database with real data."""
        return self.synthetic_database if self.synthetic else self.database

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> StoreSettings:
        env = os.environ if env is None else env
        values: dict[str, object] = {}
        for field in ("uri", "user", "password", "database", "synthetic_database"):
            if (raw := env.get(ENV_PREFIX + field.upper())) is not None:
                values[field] = raw
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(values)
