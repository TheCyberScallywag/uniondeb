"""The whole pipeline on Neo4j Enterprise: the eval gate's suites, run against a real server.

Everything else in the suite runs on the embedded engine. This proves the same Cypher (ingest,
extract contracts, tagging, scans, review, cases, SAR, audit chain) on the database that stands in
for AuraDB. Skipped unless ``UNIONDEB_INTEGRATION=1``.

Verifies: UND-021, UND-032, UND-042, UND-061, UND-071, UND-081, UND-092
"""

import os

import pytest

pytestmark = pytest.mark.integration
DATABASE, RESTORED = "uniondeb-it-gate", "uniondeb-it-restore"


@pytest.fixture
async def neo4j():
    if os.environ.get("UNIONDEB_INTEGRATION") != "1":
        pytest.skip("set UNIONDEB_INTEGRATION=1 with UNIONDEB_NEO4J_* to run against Neo4j")
    from uniondeb.store import Neo4jRunner, StoreSettings

    runner = Neo4jRunner.from_settings(StoreSettings.from_env(database=DATABASE))
    await runner.verify()
    await runner.run_system(f"CREATE OR REPLACE DATABASE `{DATABASE}` WAIT")
    yield runner
    for name in (DATABASE, RESTORED):
        await runner.run_system(f"DROP DATABASE `{name}` IF EXISTS")
    await runner.close()


async def test_gate_passes_on_neo4j_and_the_result_survives_backup_and_restore(neo4j, scenario_run, tmp_path):
    from uniondeb.cases import AuditLog
    from uniondeb.evals.gate import run_gate
    from uniondeb.store import Neo4jRunner
    from uniondeb.store.maintenance import backup, restore

    report = await run_gate(scenario_run[0], runner=neo4j)
    assert report.passed, [s.model_dump() for s in report.suites if not s.passed]

    summary = await backup(neo4j, tmp_path / "backup.jsonl.gz")
    await neo4j.run_system(f"CREATE OR REPLACE DATABASE `{RESTORED}` WAIT")
    target = Neo4jRunner(neo4j.driver, RESTORED)
    assert await restore(target, tmp_path / "backup.jsonl.gz") == summary
    assert (await AuditLog(target).verify()).ok
