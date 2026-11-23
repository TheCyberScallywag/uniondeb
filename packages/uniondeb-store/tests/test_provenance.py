"""Tarddiad on an embedded Relate store: activities, failures and lineage back to the file hash."""

import logfire
import pytest

from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.store import ProvenanceError, Tarddiad, migrate

PORTHOR = AgentRef(name="porthor", version="0.1.0")
DIDOLWR = AgentRef(name="didolwr", version="0.2.0")
SHA = "9f" * 32


@pytest.fixture
async def store(relate_runner):
    logfire.configure(send_to_logfire=False, console=False)
    await migrate(relate_runner)
    return relate_runner


async def _ingest(store, tarddiad):
    await store.run("CREATE (:SourceFile {sha256: $sha, synthetic: true})", {"sha": SHA})
    async with tarddiad.activity(ActivityKind.INGEST, agent=PORTHOR, config_versions={"alias_registry": "v3"}) as act:
        act.used(EntityRef(label="SourceFile", key=SHA))
        await store.run("UNWIND $keys AS k CREATE (:Record {key: k})", {"keys": ["sms:1", "sms:2"]})
        act.generated(EntityRef(label="Record", key="sms:1"), EntityRef(label="Record", key="sms:2"))
    return act


async def test_activity_records_agent_versions_trace_and_edges(store):
    t = Tarddiad(store)
    act = await _ingest(store, t)
    [row] = await store.run(
        "MATCH (a:Activity {id: $id})-[:WAS_ASSOCIATED_WITH]->(sa:SoftwareAgent) "
        "RETURN a {.*} AS a, sa.name AS agent, COUNT { (a)-[:GENERATED]->() } AS gen, COUNT { (a)-[:USED]->() } AS used",
        {"id": act.id},
    )
    assert row["agent"] == "porthor" and row["gen"] == 2 and row["used"] == 1
    a = row["a"]
    assert a["status"] == "succeeded" and a["agent_version"] == "0.1.0" and a["cfg_alias_registry"] == "v3"
    assert len(a["trace_id"]) == 32


async def test_lineage_walks_from_a_tag_back_to_the_file_hash(store):
    t = Tarddiad(store)
    await _ingest(store, t)
    await store.run("CREATE (:Tag {id: 't1', label: 'question', current: true})")
    async with t.activity(
        ActivityKind.TAG, agent=DIDOLWR, model="test", prompt_version="tag-v1", config_versions={"trigger_list": "v7"}
    ) as act:
        act.used(EntityRef(label="Record", key="sms:1"))
        act.generated(EntityRef(label="Tag", key="t1"))
    lineage = await t.lineage(EntityRef(label="Tag", key="t1"))
    assert lineage.source_hashes == [SHA]
    assert [s.activity["kind"] for s in lineage.steps] == ["Tag", "Ingest"]
    assert lineage.steps[0].activity["prompt_version"] == "tag-v1"
    assert (await t.lineage(EntityRef(label="SourceFile", key=SHA))).source_hashes == [SHA]


async def test_failed_activity_is_stored_without_generated_claims(store):
    t = Tarddiad(store)
    await store.run("CREATE (:SourceFile {sha256: $sha})", {"sha": SHA})
    with pytest.raises(ValueError, match="bad row"):
        async with t.activity(ActivityKind.INGEST, agent=PORTHOR) as act:
            act.used(EntityRef(label="SourceFile", key=SHA))
            act.generated(EntityRef(label="Record", key="never-written"))
            raise ValueError("bad row 7")
    [row] = await store.run(
        "MATCH (a:Activity {id: $id}) RETURN a.status AS s, a.error AS e, "
        "COUNT { (a)-[:USED]->() } AS used, COUNT { (a)-[:GENERATED]->() } AS gen",
        {"id": act.id},
    )
    assert row == {"s": "failed", "e": "ValueError: bad row 7", "used": 1, "gen": 0}


async def test_references_to_missing_entities_are_refused(store):
    t = Tarddiad(store)
    with pytest.raises(ProvenanceError, match="missing SourceFile"):
        async with t.activity(ActivityKind.INGEST, agent=PORTHOR) as act:
            act.used(EntityRef(label="SourceFile", key="nope"))
