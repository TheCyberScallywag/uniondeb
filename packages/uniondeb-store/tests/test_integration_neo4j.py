"""Against a real Neo4j Enterprise (CI service) — the same migrations, the same ledger."""

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


async def test_metrics_role_cannot_read_content(neo4j_runner):
    """A user holding svc_extract_metrics sees records but not their content."""
    from neo4j import AsyncGraphDatabase

    from uniondeb.store import StoreSettings, roles

    db = neo4j_runner.database
    await migrate(neo4j_runner)
    await neo4j_runner.run("CREATE (:Record {key: 'sms:1', dataset: 'sms', body: 'meet at the dock'})")
    await roles.apply(neo4j_runner, db)
    await neo4j_runner.run_system(
        "CREATE OR REPLACE USER it_metrics SET PASSWORD 'it-metrics-pass-1' CHANGE NOT REQUIRED"
    )
    await neo4j_runner.run_system("GRANT ROLE svc_extract_metrics TO it_metrics")
    try:
        settings = StoreSettings.from_env()
        async with AsyncGraphDatabase.driver(settings.uri, auth=("it_metrics", "it-metrics-pass-1")) as d:
            records, _, _ = await d.execute_query(
                "MATCH (r:Record) RETURN r.key AS key, r.dataset AS dataset, r.body AS body", database_=db
            )
        assert [r.data() for r in records] == [{"key": "sms:1", "dataset": "sms", "body": None}]
        again = await roles.apply(neo4j_runner, db)  # reconcile is repeatable
        assert set(again) == set(roles.ROLE_NAMES)
    finally:
        await neo4j_runner.run_system("DROP USER it_metrics IF EXISTS")
