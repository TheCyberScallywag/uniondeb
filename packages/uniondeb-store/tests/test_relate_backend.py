"""The real migrations run on an embedded Relate engine and enforce the constraints they declare.

Verifies: UND-013
"""

import pytest

from uniondeb.store import RelateRunner, StoreSettings, load_migrations, migrate, open_runner, status


async def test_migrations_apply_on_relate_and_are_idempotent(relate_runner):
    ran = await migrate(relate_runner)
    assert len(ran) == len(load_migrations())
    assert await migrate(relate_runner) == []
    assert {r["state"] for r in await status(relate_runner)} == {"applied"}
    names = (await relate_runner.run("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS n"))[0]["n"]
    assert {"migration_version", "record_key", "identifier_kind_value", "audit_event_seq"} <= set(names)


async def test_constraints_are_enforced(relate_runner):
    await migrate(relate_runner)
    await relate_runner.run("CREATE (:Record {key: 'calls:1'})")
    with pytest.raises(Exception, match="(?i)constraint|uniqu"):
        await relate_runner.run("CREATE (:Record {key: 'calls:1'})")
    await relate_runner.run("MERGE (:Identifier {kind: 'IMEI', value: '35'})")
    await relate_runner.run("MERGE (:Identifier {kind: 'IMSI', value: '35'})")  # same value, other kind: fine
    rows = await relate_runner.run("MATCH (i:Identifier) RETURN count(i) AS c")
    assert rows == [{"c": 2}]


async def test_params_round_trip(relate_runner):
    await relate_runner.run(
        "UNWIND $rows AS r MERGE (n:Device {id: r.id}) SET n.kind = r.kind",
        {"rows": [{"id": "d1", "kind": "mobile"}, {"id": "d2", "kind": "landline"}]},
    )
    rows = await relate_runner.run("MATCH (n:Device) RETURN n.id AS id, n.kind AS kind ORDER BY id")
    assert rows == [{"id": "d1", "kind": "mobile"}, {"id": "d2", "kind": "landline"}]


async def test_file_backed_extract_can_be_reopened_read_only(tmp_path):
    path = tmp_path / "extract.rl"
    async with open_runner(StoreSettings(uri=f"relate:{path}")) as w:
        await migrate(w)
        await w.run("CREATE (:Record {key: 'sms:1'})")
    ro = RelateRunner.open(str(path), read_only=True)
    try:
        assert await ro.run("MATCH (r:Record) RETURN r.key AS k") == [{"k": "sms:1"}]
        with pytest.raises(Exception, match="(?i)read.?only"):
            await ro.run("CREATE (:Record {key: 'sms:2'})")
    finally:
        await ro.close()
