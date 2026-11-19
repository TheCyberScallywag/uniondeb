"""End to end: generate a synthetic run, load it into an embedded store, check provenance.

Verifies: UND-017
"""

import csv
import json

import logfire
import pytest

from uniondeb.core.cli import main
from uniondeb.core.prov import EntityRef
from uniondeb.store import RelateRunner, StoreSettings, Tarddiad, migrate
from uniondeb.store.load import SyntheticIsolationError, load_run
from uniondeb.synth.generate import main as generate


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic") / "run"
    generate(
        [
            "--out",
            str(out),
            "--days",
            "2",
            "--employees",
            "10",
            "--externals",
            "16",
            "--rows-per-file",
            "200",
            "--error-rate",
            "0.03",
            "--seed",
            "7",
        ]
    )
    return out


@pytest.fixture
async def store():
    logfire.configure(send_to_logfire=False, console=False)
    runner = RelateRunner.open(":memory:")
    await migrate(runner)
    yield runner
    await runner.close()


SYNTH = StoreSettings(uri="relate::memory:", synthetic=True)


def _csv(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


async def test_load_matches_ground_truth(run_dir, store):
    result = await load_run(store, run_dir, SYNTH)
    expected = {
        (e["file"], int(e["row"]))
        for e in _csv(run_dir / "ground_truth" / "expected_errors.csv")
        if e["error_type"] != "duplicate_record"
    }
    rejected = {(f.path, r["row"]) for f in result.files for r in f.rejected}
    assert rejected == expected

    bad_keys = {
        f"{e['dataset']}:{e['record_key']}"
        for e in _csv(run_dir / "ground_truth" / "expected_errors.csv")
        if e["error_type"] != "duplicate_record"
    }
    labelled = {f"{lab['dataset']}:{lab['record_key']}" for lab in _csv(run_dir / "ground_truth" / "labels.csv")}
    [row] = await store.run("MATCH (r:Record) RETURN collect(r.key) AS keys")
    assert set(row["keys"]) == labelled - bad_keys
    assert result.summary()["database"] == "uniondeb_synthetic"


async def test_graph_shape_and_synthetic_flag(run_dir, store):
    await load_run(store, run_dir, SYNTH)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    [row] = await store.run(
        "MATCH (f:SourceFile) RETURN count(f) AS files, collect(DISTINCT f.synthetic) AS flags, "
        "min(COUNT { (f)-[:CONTAINED]->() }) AS min_contained"
    )
    assert row["files"] == len(manifest["files"]) and row["flags"] == [True]
    [shape] = await store.run(
        "MATCH (d:Device {kind: 'mobile'})-[:HAS_IDENTIFIER]->(i:Identifier) "
        "WITH count(DISTINCT d) AS mobiles, collect(DISTINCT i.kind) AS kinds "
        "MATCH (e:Record:Email)-[:FROM]->(:EmailAddress) "
        "RETURN mobiles, kinds, count(DISTINCT e) AS emails, COUNT { (:Person)-[:USES]->(:Device) } AS uses"
    )
    assert shape["mobiles"] > 0 and {"imei", "imsi", "iccid", "msisdn"} <= set(shape["kinds"])
    assert shape["emails"] > 0 and shape["uses"] > 0


async def test_every_record_traces_back_to_its_file_hash(run_dir, store):
    result = await load_run(store, run_dir, SYNTH)
    f = next(f for f in result.files if f.loaded)
    [row] = await store.run(
        "MATCH (:SourceFile {sha256: $sha})-[:CONTAINED]->(r:Record) RETURN r.key AS key LIMIT 1", {"sha": f.sha256}
    )
    lineage = await Tarddiad(store).lineage(EntityRef(label="Record", key=row["key"]))
    assert lineage.source_hashes == [f.sha256]
    assert lineage.steps[0].activity["kind"] == "Ingest" and lineage.steps[0].activity["agent"] == "uniondeb-load"


async def test_reload_is_idempotent_for_records(run_dir, store):
    first = (await load_run(store, run_dir, SYNTH)).summary()["records"]
    await load_run(store, run_dir, SYNTH)
    [row] = await store.run("MATCH (r:Record) RETURN count(r) AS c")
    assert row["c"] == first


async def test_synthetic_runs_never_load_into_the_real_database(run_dir, store):
    with pytest.raises(SyntheticIsolationError, match="synthetic"):
        await load_run(store, run_dir, StoreSettings(uri="relate::memory:"))


async def test_tampered_file_is_refused(run_dir, store, tmp_path):
    import shutil

    copy = tmp_path / "run"
    shutil.copytree(run_dir, copy)
    manifest = json.loads((copy / "manifest.json").read_text())
    target = copy / manifest["files"][0]["path"]
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="does not match the manifest"):
        await load_run(store, copy, SYNTH)


def test_cli(run_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UNIONDEB_NEO4J_URI", raising=False)
    assert main(["load", str(run_dir), "--uri", f"relate:{tmp_path / 's.rl'}", "--migrate"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["database"] == "uniondeb_synthetic" and out["records"] > 0 and out["rejected"] >= 0
