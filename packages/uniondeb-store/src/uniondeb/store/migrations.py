"""Ordered, idempotent Cypher migrations with a ledger in the database.

Migrations live in ``uniondeb/store/migrations/NNNN_<name>.cypher``; statements are separated by a
line ending in ``;``. Each applied migration is recorded as ``(:Migration {version, name, checksum,
applied_at})``. A recorded migration whose file has since changed is drift and stops the run:
fix forward with a new migration instead of editing an applied one.

Implements: UND-013
"""

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from uniondeb.store.runner import CypherRunner

FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.cypher$")
LEDGER_CONSTRAINT = "CREATE CONSTRAINT migration_version IF NOT EXISTS FOR (m:Migration) REQUIRE m.version IS UNIQUE"


class MigrationDriftError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str

    @classmethod
    def parse(cls, filename: str, text: str) -> Migration:
        m = FILENAME.match(filename)
        if not m:
            raise ValueError(f"migration file {filename!r} must be named NNNN_name.cypher")
        statements = split_statements(text)
        if not statements:
            raise ValueError(f"migration {filename!r} has no statements")
        checksum = hashlib.sha256("\n;\n".join(statements).encode()).hexdigest()
        return cls(int(m.group(1)), m.group(2), statements, checksum)


def split_statements(text: str) -> tuple[str, ...]:
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return tuple(s.strip() for s in re.split(r";\s*(?:\n|$)", body) if s.strip())


def load_migrations(source: Traversable | None = None) -> list[Migration]:
    source = source or files("uniondeb.store") / "migrations"
    found = [Migration.parse(p.name, p.read_text("utf-8")) for p in source.iterdir() if p.name.endswith(".cypher")]
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions: {versions}")
    return found


async def applied(runner: CypherRunner) -> dict[int, dict[str, Any]]:
    rows = await runner.run(
        "MATCH (m:Migration) RETURN m.version AS version, m.name AS name, m.checksum AS checksum ORDER BY version"
    )
    return {int(r["version"]): r for r in rows}


async def status(runner: CypherRunner, migrations: list[Migration] | None = None) -> list[dict[str, Any]]:
    migrations = load_migrations() if migrations is None else migrations
    done = await applied(runner)
    out = []
    for m in migrations:
        rec = done.get(m.version)
        state = "pending" if rec is None else ("applied" if rec["checksum"] == m.checksum else "drift")
        out.append({"version": m.version, "name": m.name, "state": state})
    return out


async def migrate(
    runner: CypherRunner, migrations: list[Migration] | None = None, *, applied_by: str = "uniondeb-store"
) -> list[Migration]:
    """Apply pending migrations in order; return the ones applied. Safe to run repeatedly."""
    migrations = load_migrations() if migrations is None else migrations
    await runner.run(LEDGER_CONSTRAINT)
    done = await applied(runner)
    drift = [m for m in migrations if m.version in done and done[m.version]["checksum"] != m.checksum]
    if drift:
        names = ", ".join(f"{m.version:04d}_{m.name}" for m in drift)
        raise MigrationDriftError(f"applied migrations changed on disk: {names}")
    ran = []
    for m in migrations:
        if m.version in done:
            continue
        for stmt in m.statements:
            await runner.run(stmt)
        await runner.run(
            "CREATE (:Migration {version: $version, name: $name, checksum: $checksum, "
            "applied_by: $applied_by, applied_at: datetime()})",
            {"version": m.version, "name": m.name, "checksum": m.checksum, "applied_by": applied_by},
        )
        ran.append(m)
    return ran
