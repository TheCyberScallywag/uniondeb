"""Migrations: parsing, ordering, ledger, idempotency and drift.

Verifies: UND-013
"""

from dataclasses import replace

import pytest

from uniondeb.store import Migration, MigrationDriftError, load_migrations, migrate, status
from uniondeb.store.migrations import LEDGER_CONSTRAINT, split_statements


class FakeRunner:
    """Records every statement; understands just enough to keep a migration ledger."""

    def __init__(self):
        self.statements: list[str] = []
        self.ledger: dict[int, dict] = {}

    async def run(self, query, params=None):
        self.statements.append(query)
        if query.startswith("MATCH (m:Migration)"):
            return [dict(v, version=k) for k, v in sorted(self.ledger.items())]
        if query.startswith("CREATE (:Migration"):
            self.ledger[params["version"]] = {"name": params["name"], "checksum": params["checksum"]}
        return []

    async def close(self):
        pass


def test_split_statements_ignores_comments_and_blank_lines():
    text = "// header\nCREATE A;\n\n  // note\nCREATE B\n  WITH x;\n"
    assert split_statements(text) == ("CREATE A", "CREATE B\n  WITH x")


def test_bundled_migrations_are_ordered_contiguous_and_idempotent_ddl():
    ms = load_migrations()
    assert [m.version for m in ms] == list(range(1, len(ms) + 1))
    for m in ms:
        for stmt in m.statements:
            assert "IF NOT EXISTS" in stmt, f"{m.name}: schema statements must be re-runnable: {stmt}"


def test_bundled_migrations_cover_the_plan_model():
    ddl = "\n".join(s for m in load_migrations() for s in m.statements)
    for label in [
        "SourceFile",
        "Record",
        "Device",
        "Identifier",
        "Person",
        "Activity",
        "SoftwareAgent",
        "Tag",
        "ReviewItem",
        "Alert",
        "Email",
        "Case",
        "Flag",
        "SarSubmission",
        "AuditEvent",
    ]:
        assert f"(n:{label})" in ddl, label


def test_bad_filename_rejected():
    with pytest.raises(ValueError):
        Migration.parse("1_bad.cypher", "CREATE X;")


async def test_migrate_applies_in_order_then_is_a_no_op():
    r = FakeRunner()
    ran = await migrate(r)
    assert [m.version for m in ran] == [m.version for m in load_migrations()]
    assert r.statements[0] == LEDGER_CONSTRAINT
    first = len(r.statements)
    assert await migrate(r) == []
    assert r.statements[first:] == [LEDGER_CONSTRAINT, r.statements[1]]  # ledger check + read only


async def test_drift_is_detected_and_nothing_runs():
    r = FakeRunner()
    ms = load_migrations()
    await migrate(r, ms[:1])
    changed = [replace(ms[0], checksum="0" * 64), *ms[1:]]
    before = len(r.statements)
    with pytest.raises(MigrationDriftError, match="0001_"):
        await migrate(r, changed)
    assert not any(s.startswith("CREATE (:Migration") for s in r.statements[before:])
    states = {row["version"]: row["state"] for row in await status(r, changed)}
    assert states[1] == "drift" and all(v == "pending" for k, v in states.items() if k > 1)
