"""Against a real Neo4j Enterprise (CI service) — the same migrations, the same ledger.

Verifies: UND-013
"""

import pytest

from uniondeb.store import load_migrations, migrate, status

pytestmark = pytest.mark.integration


async def test_migrations_on_enterprise(neo4j_runner):
    assert len(await migrate(neo4j_runner)) == len(load_migrations())
    assert await migrate(neo4j_runner) == []
    assert {r["state"] for r in await status(neo4j_runner)} == {"applied"}
    await neo4j_runner.run("CREATE (:Record {key: 'calls:1'})")
    with pytest.raises(Exception, match="(?i)constraint|already exists"):
        await neo4j_runner.run("CREATE (:Record {key: 'calls:1'})")
