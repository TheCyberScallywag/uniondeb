"""Shared fixtures for uniondeb-store tests.

Verifies: UND-013
"""

import os

import pytest

from uniondeb.store import RelateRunner


@pytest.fixture
async def relate_runner():
    runner = RelateRunner.open(":memory:")
    yield runner
    await runner.close()


@pytest.fixture
async def neo4j_runner():
    """Real Neo4j Enterprise or Aura; CI provides one, local runs skip unless asked."""
    if os.environ.get("UNIONDEB_INTEGRATION") != "1":
        pytest.skip("set UNIONDEB_INTEGRATION=1 with UNIONDEB_NEO4J_* to run against Neo4j")
    from uniondeb.store import Neo4jRunner, StoreSettings

    runner = Neo4jRunner.from_settings(StoreSettings.from_env(database="uniondeb_it"))
    await runner.verify()
    await runner.run_system("CREATE OR REPLACE DATABASE uniondeb_it WAIT")
    yield runner
    await runner.run_system("DROP DATABASE uniondeb_it IF EXISTS")
    await runner.close()
