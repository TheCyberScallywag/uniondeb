"""Receiver settings, from ``CENNAD_*`` environment variables.

The defaults fail closed: an empty allowlist rejects every peer, and without a certificate the
receiver refuses to start unless plaintext is explicitly allowed (a test or a loopback demo).

Implements: UND-026
"""

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "CENNAD_"
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class CennadSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8025, ge=0, le=65535)
    allow: tuple[str, ...] = Field(default=(), description="CIDRs of the journaling MTAs; empty rejects everyone")
    max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    store: Path = Path("var/cennad")
    kafka: str | None = Field(default=None, description="Bootstrap servers; unset writes NDJSON to stdout")
    allow_plaintext: bool = Field(default=False, description="Accept mail without STARTTLS (tests, loopback demos)")
    synthetic: bool = Field(default=False, description="Mark published documents as synthetic")

    @field_validator("allow", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        return tuple(p.strip() for p in v.split(",") if p.strip()) if isinstance(v, str) else v

    @field_validator("allow")
    @classmethod
    def _cidrs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(ipaddress.ip_network(c, strict=False)) for c in v)

    @property
    def networks(self) -> tuple[Network, ...]:
        return tuple(ipaddress.ip_network(c) for c in self.allow)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> CennadSettings:
        env = os.environ if env is None else env
        values: dict[str, object] = {}
        for name in ("host", "port", "allow", "max_bytes", "tls_cert", "tls_key", "store", "kafka"):
            if raw := env.get(ENV_PREFIX + name.upper()):
                values[name] = raw
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(values)
