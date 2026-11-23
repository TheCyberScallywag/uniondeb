"""Extract contracts: what one agent may be given for one kind of job (design §3.4).

A contract is a YAML file, reviewed like code. It names the agent, the Aura role the queries run
under, a row cap, a time to live, the properties to pseudonymise and one or more *tables*. A table
is a read-only Cypher query whose rows become nodes or relationships in the extract. Nothing else
reaches the agent, so the contract is the whole of what it can see.

Validation happens when the registry loads, before any extract is built:

- queries must be read-only (no write clause, no procedure call);
- a query may not return, or even mention, a property its role is denied. The same lists drive
  the Aura privileges (``uniondeb.store.roles``), so the two cannot drift apart;
- every returned column is declared, and ``pseudonymise`` entries name declared columns.

The contract hash covers every field, so any edit is a new contract version in provenance.
"""

import hashlib
import json
import re
from collections.abc import Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from uniondeb.store.roles import CONTENT_PROPERTIES, IDENTITY_PROPERTIES, ROLE_NAMES

MAX_RECORDS = 250_000  # ADR 0002: keeps a read-only open under about 520 MB resident

# Properties a role may never be given, whatever the Cypher says. Aura enforces the same lists.
FORBIDDEN_BY_ROLE: dict[str, frozenset[str]] = {
    "svc_extract_metrics": frozenset({*CONTENT_PROPERTIES, "name"}),
    "svc_extract_content": frozenset({*IDENTITY_PROPERTIES, "name", "value", "address"}),
    "svc_ingest": frozenset(CONTENT_PROPERTIES),
    "auditor": frozenset(CONTENT_PROPERTIES),
}
EXTRACT_ROLES = frozenset(ROLE_NAMES) & {
    "svc_ingest",
    "svc_extract_content",
    "svc_extract_metrics",
    "svc_investigation",
    "auditor",
}

_WRITE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|FOREACH|LOAD\s+CSV|GRANT|DENY|REVOKE)\b|\bCALL\s+[a-z]", re.I
)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_PROPERTY = re.compile(r"\.\s*`?([A-Za-z_][A-Za-z0-9_]*)`?")
_PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class ContractError(ValueError):
    pass


class End(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node: Name
    column: Name


class Table(BaseModel):
    """Rows of ``cypher`` become nodes labelled ``node`` or relationships of type ``rel``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    cypher: str
    columns: tuple[Name, ...]
    node: Name | None = None
    key: Name | None = None  # nodes with a key are merged on it; without one, each row is a node
    rel: Name | None = None
    start: End | None = None
    end: End | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.node is None) == (self.rel is None):
            raise ValueError(f"table {self.name}: give exactly one of node or rel")
        if self.rel and not (self.start and self.end):
            raise ValueError(f"table {self.name}: a rel table needs start and end")
        needed = [c for c in (self.key, self.start and self.start.column, self.end and self.end.column) if c]
        if missing := [c for c in needed if c not in self.columns]:
            raise ValueError(f"table {self.name}: {missing} must be declared in columns")
        text = _STRINGS.sub("''", self.cypher)
        if m := _WRITE.search(text):
            raise ValueError(f"table {self.name}: contract queries are read-only, found {m.group(0).strip()!r}")
        if not re.search(r"\bRETURN\b", text, re.I):
            raise ValueError(f"table {self.name}: the query must RETURN its columns")
        return self

    @property
    def mentioned_properties(self) -> set[str]:
        return set(_PROPERTY.findall(_STRINGS.sub("''", self.cypher)))

    @property
    def parameters(self) -> set[str]:
        return set(_PARAM.findall(self.cypher))


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z]+\.[a-z][a-z0-9-]*\.v\d+$")]
    purpose: str
    agent: Name
    aura_role: str
    max_rows: Annotated[int, Field(gt=0, le=MAX_RECORDS)] = MAX_RECORDS
    ttl_minutes: Annotated[int, Field(gt=0, le=24 * 60)] = 30
    params: tuple[Name, ...] = ()
    defaults: dict[str, Any] = Field(default_factory=dict)
    pseudonymise: tuple[str, ...] = ()  # "Label.column" in the extract
    forbid: tuple[Name, ...] = ()  # extra denied properties, on top of the role's
    tables: tuple[Table, ...]

    @model_validator(mode="after")
    def _least_privilege(self) -> Self:
        if self.aura_role not in EXTRACT_ROLES:
            raise ValueError(f"aura_role {self.aura_role!r} is not an extract role: {sorted(EXTRACT_ROLES)}")
        names = [t.name for t in self.tables]
        if len(set(names)) != len(names) or not names:
            raise ValueError("table names must be unique and there must be at least one")
        denied = FORBIDDEN_BY_ROLE.get(self.aura_role, frozenset()) | set(self.forbid)
        for t in self.tables:
            if bad := sorted((set(t.columns) | t.mentioned_properties) & denied):
                raise ValueError(f"table {t.name}: {self.aura_role} may not read {bad}")
            if unknown := sorted(t.parameters - set(self.params) - set(self.defaults)):
                raise ValueError(f"table {t.name}: undeclared parameters {unknown}")
        nodes = {t.node: t for t in self.tables if t.node}
        if len(nodes) != sum(1 for t in self.tables if t.node):
            raise ValueError("each node label may be filled by one table only")
        for t in self.tables:
            for end in (t.start, t.end):
                if end and (end.node not in nodes or nodes[end.node].key is None):
                    raise ValueError(f"table {t.name}: {end.node} is not a keyed node table in this contract")
        for entry in self.pseudonymise:
            label, _, column = entry.partition(".")
            if label not in nodes or column not in nodes[label].columns:
                raise ValueError(f"pseudonymise {entry!r} does not name a column of a node table")
        return self

    @property
    def hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def pseudonymised(self, table: Table) -> set[str]:
        """Columns of ``table`` to pseudonymise, following keys into relationship ends."""
        marked = {tuple(e.split(".", 1)) for e in self.pseudonymise}
        if table.node:
            return {c for label, c in marked if label == table.node}
        keys = {t.node: t.key for t in self.tables if t.node}
        return {e.column for e in (table.start, table.end) if e and (e.node, keys[e.node]) in marked}


class ContractRegistry:
    def __init__(self, contracts: list[Contract]) -> None:
        self._by_id: dict[str, Contract] = {}
        for c in contracts:
            if c.id in self._by_id:
                raise ContractError(f"duplicate contract id {c.id}")
            self._by_id[c.id] = c

    @classmethod
    def load(cls, source: Traversable | Path | None = None) -> ContractRegistry:
        """Every ``*.yaml`` in ``source`` (the packaged contracts by default). One bad file fails the load."""
        source = source or files("uniondeb.extract") / "contracts"
        out = []
        for path in sorted(source.iterdir(), key=lambda p: p.name):
            if path.name.endswith((".yaml", ".yml")):
                out.append(parse_contract(path.read_text("utf-8"), origin=path.name))
        return cls(out)

    def get(self, contract_id: str) -> Contract:
        try:
            return self._by_id[contract_id]
        except KeyError:
            raise ContractError(f"no extract contract {contract_id!r}") from None

    def __iter__(self) -> Iterator[Contract]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


def parse_contract(text: str, *, origin: str = "<string>") -> Contract:
    try:
        return Contract.model_validate(yaml.safe_load(text))
    except (ValidationError, yaml.YAMLError) as exc:
        raise ContractError(f"{origin}: {exc}") from exc
