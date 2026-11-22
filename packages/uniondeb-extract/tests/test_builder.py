"""Extracts hold only what the contract allows, are read-only, traceable and expire.

Verifies: UND-032
"""

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from uniondeb.core.prov import EntityRef
from uniondeb.extract import ContractRegistry, Echdynnwr, ExtractError, ExtractTooLarge
from uniondeb.extract.contracts import parse_contract
from uniondeb.store import Tarddiad
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.roles import CONTENT_PROPERTIES, IDENTITY_PROPERTIES

WINDOW = {"from": "2026-08-01", "to": "2026-12-01"}


@pytest.fixture
def echdynnwr(small_store, tmp_path):
    cipher = FieldCipher(FieldCipher.generate_key())
    return Echdynnwr(small_store, Tarddiad(small_store), ContractRegistry.load(), root=tmp_path / "x", cipher=cipher)


async def _a_file(store) -> str:
    [row] = await store.run(
        "MATCH (f:SourceFile {dataset: 'messages'})-[:CONTAINED]->(r:Record) "
        "RETURN f.sha256 AS sha, count(r) AS n ORDER BY n DESC LIMIT 1"
    )
    return row["sha"]


async def test_content_extract_has_content_but_no_identities(echdynnwr, small_store):
    handle = await echdynnwr.build("didolwr.tag-batch.v1", {"file": await _a_file(small_store)})
    async with echdynnwr.open(handle) as reader:
        records, parties = await reader.rows("records"), await reader.rows("parties")
        assert records and any(r["body"] for r in records)
        assert parties and all(p["id"].startswith("P-") for p in parties)
        assert {p["party_kind"] for p in parties} <= {"employee", "external", "unknown"}
        links = await reader.rows("involves")
        assert {link["party"] for link in links} <= {p["id"] for p in parties}
        # nothing in the file but what the contract declares: no names, numbers, IMEIs, addresses
        props = await reader.graph.run("MATCH (n) UNWIND keys(n) AS k RETURN collect(DISTINCT k) AS ks")
        assert not set(props[0]["ks"]) & {*IDENTITY_PROPERTIES, "name", "owner", "address", "value"}
        labels = await reader.graph.run("MATCH (n) UNWIND labels(n) AS l RETURN collect(DISTINCT l) AS ls")
        assert set(labels[0]["ls"]) == {"Record", "Party"}
    [real] = await small_store.run("MATCH (d:Device) RETURN collect(d.id) AS ids")
    raw = Path(handle.path).read_bytes()
    assert not any(i.encode() in raw for i in real["ids"])


async def test_metrics_extract_has_no_content_properties(echdynnwr):
    handle = await echdynnwr.build("gwyliwr.scan.v1", WINDOW)
    async with echdynnwr.open(handle) as reader:
        props = await reader.graph.run("MATCH (n) UNWIND keys(n) AS k RETURN collect(DISTINCT k) AS ks")
        rel_props = await reader.graph.run("MATCH ()-[r]->() UNWIND keys(r) AS k RETURN collect(DISTINCT k) AS ks")
        assert not (set(props[0]["ks"]) | set(rel_props[0]["ks"])) & {*CONTENT_PROPERTIES, "name"}
        daily = await reader.rows("daily")
        assert daily and all(d["pid"].startswith("P-") and d["records"] > 0 for d in daily)
        assert {d["pid"] for d in daily} <= {p["pid"] for p in await reader.rows("people")}
        assert await reader.rows("contacts") and await reader.rows("mails")


async def test_extract_is_read_only_private_and_hashed(echdynnwr, small_store):
    handle = await echdynnwr.build("porthor.dedupe.v1", WINDOW)
    path = Path(handle.path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    async with echdynnwr.open(handle) as reader:
        assert len(await reader.rows("records")) == handle.rows > 0
        with pytest.raises(Exception, match="(?i)read.?only"):
            await reader.graph.run("CREATE (:Record {key: 'x'})")
    again = await echdynnwr.build("porthor.dedupe.v1", WINDOW)
    assert again.content_sha256 == handle.content_sha256 and again.id != handle.id
    await small_store.run("CREATE (:Record {key: 'calls:new', occurred_at: '2026-09-01T00:00:00Z'})")
    assert (await echdynnwr.build("porthor.dedupe.v1", WINDOW)).content_sha256 != handle.content_sha256


async def test_provenance_links_extract_to_contract_version(echdynnwr, small_store):
    handle = await echdynnwr.build("porthor.dedupe.v1", WINDOW)
    lineage = await Tarddiad(small_store).lineage(EntityRef(label="Extract", key=handle.id))
    [step] = lineage.steps
    assert step.activity["kind"] == "Extract" and step.activity["agent"] == "echdynnwr"
    assert step.used == [EntityRef(label="ExtractContract", key=handle.contract_hash)]
    [x] = await small_store.run("MATCH (x:Extract {id: $id}) RETURN x {.*} AS x", {"id": handle.id})
    assert x["x"]["sha256"] == handle.sha256 and x["x"]["rows"] == handle.rows and "path" not in x["x"]


async def test_cap_parameters_and_failures(echdynnwr, small_store, tmp_path):
    with pytest.raises(ExtractError, match="needs parameters"):
        await echdynnwr.build("porthor.dedupe.v1", {"from": "2026-01-01"})
    with pytest.raises(ExtractError, match="not"):
        await echdynnwr.build("porthor.dedupe.v1", {**WINDOW, "extra": 1})
    tiny = parse_contract(
        "id: porthor.tiny.v1\npurpose: t\nagent: porthor\naura_role: svc_ingest\nmax_rows: 3\n"
        "tables:\n  - name: records\n    node: Record\n    key: key\n    columns: [key]\n"
        "    cypher: MATCH (r:Record) RETURN r.key AS key\n"
    )
    leaky = parse_contract(
        "id: porthor.leaky.v1\npurpose: t\nagent: porthor\naura_role: svc_ingest\n"
        "tables:\n  - name: records\n    node: Record\n    key: key\n    columns: [key]\n"
        "    cypher: MATCH (r:Record) RETURN r.key AS key, r.dataset AS dataset\n"
    )
    builder = Echdynnwr(small_store, Tarddiad(small_store), ContractRegistry([tiny, leaky]), root=tmp_path / "y")
    with pytest.raises(ExtractTooLarge, match="cap is 3"):
        await builder.build("porthor.tiny.v1")
    with pytest.raises(ExtractError, match="undeclared columns"):
        await builder.build("porthor.leaky.v1")
    assert list((tmp_path / "y").iterdir()) == []  # a failed build leaves no file behind
    failed = await small_store.run("MATCH (a:Activity {kind: 'Extract', status: 'failed'}) RETURN count(a) AS n")
    assert failed[0]["n"] == 1


async def test_ttl_reaper_deletes_and_records(echdynnwr, small_store):
    handle = await echdynnwr.build("porthor.dedupe.v1", WINDOW)
    assert await echdynnwr.reap() == []
    later = datetime.now(UTC) + timedelta(minutes=31)
    assert await echdynnwr.reap(later) == [handle.id]
    assert not Path(handle.path).exists() and await echdynnwr.reap(later) == []
    with pytest.raises(ExtractError, match="gone"):
        echdynnwr.open(handle)
    [x] = await small_store.run("MATCH (x:Extract {id: $id}) RETURN x.deleted_at AS d", {"id": handle.id})
    assert x["d"]
    [n] = await small_store.run(
        "MATCH (a:Activity {kind: 'Expire'})-[:USED]->(:Extract {id: $id}) RETURN count(a) AS n", {"id": handle.id}
    )
    assert n["n"] == 1
    expired = handle.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(ExtractError, match="expired"):
        echdynnwr.open(expired)


async def test_pseudonyms_resolve_only_on_the_trusted_side(echdynnwr, small_store):
    handle = await echdynnwr.build("didolwr.tag-batch.v1", {"file": await _a_file(small_store)})
    async with echdynnwr.open(handle) as reader:
        pseudonyms = [p["id"] for p in await reader.rows("parties")]
    resolved = await echdynnwr.resolve(handle.id, pseudonyms)
    [real] = await small_store.run("MATCH (d:Device) RETURN collect(d.id) AS ids")
    assert set(resolved) == set(pseudonyms) and set(resolved.values()) <= set(real["ids"])
    [x] = await small_store.run("MATCH (x:Extract {id: $id}) RETURN x.pseudonyms_enc AS enc", {"id": handle.id})
    assert not any(v in x["enc"] for v in resolved.values())
    other = await echdynnwr.build("didolwr.tag-batch.v1", {"file": await _a_file(small_store)})
    async with echdynnwr.open(other) as reader:
        assert not {p["id"] for p in await reader.rows("parties")} & set(pseudonyms)  # a fresh key per extract


async def test_fallback_backend_serves_the_same_tables(small_store, tmp_path):
    relate = Echdynnwr(small_store, Tarddiad(small_store), ContractRegistry.load(), root=tmp_path / "r")
    parquet = Echdynnwr(
        small_store, Tarddiad(small_store), ContractRegistry.load(), root=tmp_path / "p", backend="parquet"
    )
    a, b = await relate.build("porthor.dedupe.v1", WINDOW), await parquet.build("porthor.dedupe.v1", WINDOW)
    assert a.content_sha256 == b.content_sha256
    async with relate.open(a) as ra, parquet.open(b) as rb:
        key = lambda r: r["key"]  # noqa: E731
        assert sorted(await ra.rows("records"), key=key) == sorted(await rb.rows("records"), key=key)
        with pytest.raises(ExtractError, match="no Cypher"):
            _ = rb.graph
