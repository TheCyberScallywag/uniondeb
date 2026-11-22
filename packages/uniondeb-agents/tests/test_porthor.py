"""Porthor: every row becomes a record or a review item, replay is idempotent, mappings need approval.

Verifies: UND-021, UND-022, UND-023, UND-027
"""

import csv
import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from uniondeb.agents.models import ModelNotConfigured
from uniondeb.agents.porthor import AliasRegistry, ingest_file, register_upload
from uniondeb.agents.porthor.detect import DetectionError, detect_dataset, detect_format
from uniondeb.agents.porthor.mapping import mapping_effect, mask, propose_mappings
from uniondeb.agents.porthor.stream import consume_dead_letter, consume_mail
from uniondeb.core.prov import EntityRef
from uniondeb.store import Tarddiad
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.load import read_rows
from uniondeb.store.review import Reviewer, decide, get_item, list_items

CIPHER = FieldCipher(FieldCipher.generate_key())


def _csv(path):
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


async def _ingest(store, run, entry, **kw):
    path = run / entry["path"]
    t = Tarddiad(store)
    await register_upload(store, t, path, uploader="tester", filename=entry["path"], synthetic=True)
    return await ingest_file(store, t, CIPHER, path, **kw)


def test_format_and_dataset_are_detected_for_every_file(small_run):
    run, _ = small_run
    manifest = json.loads((run / "manifest.json").read_text())
    for entry in manifest["files"]:
        path = run / entry["path"]
        assert detect_format(path) == entry["format"], entry["path"]
        assert detect_dataset(read_rows(path, entry["format"])) == entry["dataset"], entry["path"]
    with pytest.raises(DetectionError):
        detect_dataset([{"colour": "red", "size": 3}])


async def test_replay_every_valid_row_accepted_every_fault_in_review(small_run, empty_store):
    run, _ = small_run
    manifest = json.loads((run / "manifest.json").read_text())
    expected = _csv(run / "ground_truth" / "expected_errors.csv")
    faults = {(e["file"], int(e["row"])) for e in expected if e["error_type"] != "duplicate_record"}
    duplicates = sum(1 for e in expected if e["error_type"] == "duplicate_record")
    rows = loaded = linked = errors = 0
    for entry in manifest["files"]:
        r = await _ingest(empty_store, run, entry)  # no hints: format and dataset are detected
        assert (r.dataset, r.format) == (entry["dataset"], entry["format"])
        rows, loaded, linked, errors = rows + r.rows, loaded + r.loaded, linked + r.linked, errors + r.errors
    assert rows == sum(f["rows"] for f in manifest["files"]) == loaded + linked + errors  # zero silent drops
    assert linked == duplicates
    found = await empty_store.run(
        "MATCH (f:SourceFile)-[:HAD_ERROR]->(e:IngestError)<-[:ABOUT]-(i:ReviewItem {kind: 'ingest_error', state: 'open'}) "
        "RETURN f.path AS file, e.row AS row, e.raw_enc AS raw, e.reason AS reason"
    )
    assert {(f["file"], f["row"]) for f in found} == faults and len(found) == len(faults) == errors
    labelled = {f"{x['dataset']}:{x['record_key']}" for x in _csv(run / "ground_truth" / "labels.csv")}
    bad = {f"{e['dataset']}:{e['record_key']}" for e in expected if e["error_type"] != "duplicate_record"}
    [keys] = await empty_store.run("MATCH (r:Record) RETURN collect(r.key) AS k")
    assert set(keys["k"]) == labelled - bad
    # the raw row is kept for the reviewer, encrypted; the stored reason carries no values
    one = found[0]
    assert isinstance(CIPHER.decrypt_json(one["raw"]), dict) and "{" not in one["raw"]
    assert "31/02/2026" not in json.dumps([f["reason"] for f in found])


async def test_replay_is_idempotent_and_provenance_reaches_the_file_hash(small_run, empty_store):
    run, _ = small_run
    manifest = json.loads((run / "manifest.json").read_text())
    entry = next(f for f in manifest["files"] if f["dataset"] == "messages" and f["rows"] > 20)
    first = await _ingest(empty_store, run, entry)
    again = await _ingest(empty_store, run, entry)
    assert again.loaded == 0 and again.linked == first.loaded + first.linked and again.errors == first.errors
    [n] = await empty_store.run("MATCH (i:ReviewItem) RETURN count(i) AS n")
    assert n["n"] == first.errors
    lineage = await Tarddiad(empty_store).lineage(EntityRef(label="Record", key=first.record_keys[0]))
    assert lineage.source_hashes == [first.sha256] and lineage.steps[0].activity["agent"] == "porthor"
    [f] = await empty_store.run("MATCH (f:SourceFile {sha256: $s}) RETURN f {.*} AS f", {"s": first.sha256})
    assert f["f"]["rows"] == first.rows and f["f"]["state"] == "ingested" and f["f"]["uploaded_by"] == "tester"


async def test_same_key_with_different_content_goes_to_review_and_never_overwrites(empty_store, tmp_path, small_run):
    run, _ = small_run
    manifest = json.loads((run / "manifest.json").read_text())
    entry = next(
        f for f in manifest["files"] if (f["dataset"], f["format"], f["dialect"]) == ("messages", "jsonl", "canonical")
    )
    await _ingest(empty_store, run, entry)
    rows = read_rows(run / entry["path"], "jsonl")
    good = next(r for r in rows if r.get("body") and r.get("sent_at"))
    changed = {**good, "body": "edited later", "body_chars": 12}
    other = tmp_path / "later.jsonl"
    other.write_text(json.dumps(changed) + "\n")
    t = Tarddiad(empty_store)
    await register_upload(empty_store, t, other, uploader="tester", filename="later.jsonl")
    r = await ingest_file(empty_store, t, CIPHER, other)
    assert (r.loaded, r.errors) == (0, 1) and "differs from the stored record" in next(iter(r.by_reason))
    [stored] = await empty_store.run(
        "MATCH (r:Record {key: $k}) RETURN r.body AS body", {"k": f"messages:{good['msg_id']}"}
    )
    assert stored["body"] == good["body"]


async def test_unreadable_file_is_one_review_item(empty_store, tmp_path):
    bad = tmp_path / "x.parquet"
    bad.write_bytes(b"PAR1 not really parquet")
    t = Tarddiad(empty_store)
    await register_upload(empty_store, t, bad, uploader="tester", filename="x.parquet")
    r = await ingest_file(empty_store, t, CIPHER, bad)
    assert r.by_reason == {"unreadable_file": 1}
    [item] = await list_items(empty_store, kind="ingest_error")
    assert item["about_label"] == "SourceFile"


def _dialect_file(run, tmp_path):
    """A canonical messages file with one column renamed to something no alias knows."""
    manifest = json.loads((run / "manifest.json").read_text())
    entry = next(
        f for f in manifest["files"] if (f["dataset"], f["format"], f["dialect"]) == ("messages", "jsonl", "canonical")
    )
    rows = [r for r in read_rows(run / entry["path"], "jsonl") if r.get("sent_at")]
    out = tmp_path / "vendor.jsonl"
    out.write_text(
        "".join(json.dumps({("Zeitstempel" if k == "sent_at" else k): v for k, v in r.items()}) + "\n" for r in rows)
    )
    return out, len(rows)


def _model(*calls):
    def respond(messages, info):
        assert [t.name for t in info.function_tools] == ["propose_mapping"]
        return ModelResponse(
            parts=[ToolCallPart("propose_mapping", args) for args in calls] or [TextPart("nothing fits")]
        )

    return FunctionModel(respond)


async def test_unknown_dialect_is_proposed_approved_and_replayed(small_run, empty_store, tmp_path):
    run, _ = small_run
    path, n = _dialect_file(run, tmp_path)
    t = Tarddiad(empty_store)
    await register_upload(empty_store, t, path, uploader="tester", filename="vendor.jsonl")
    first = await ingest_file(empty_store, t, CIPHER, path)
    assert (first.loaded, first.errors) == (0, n) and "Zeitstempel" in first.unknown_columns
    with pytest.raises(ModelNotConfigured):
        await propose_mappings(empty_store, t, first)
    model = _model(
        {"column": "Zeitstempel", "canonical_field": "sent_at", "confidence": 0.93},
        {"column": "Zeitstempel", "canonical_field": "sent_at", "confidence": 0.5},  # repeated: ignored
        {"column": "Zeitstempel", "canonical_field": "ssn", "confidence": 0.9},  # invented field: ignored
        {"column": "not_in_file", "canonical_field": "body", "confidence": 0.9},  # invented column: ignored
    )
    [item_id] = await propose_mappings(empty_store, t, first, model=model)
    item = await get_item(empty_store, item_id)
    assert item["payload"]["canonical_field"] == "sent_at" and (await AliasRegistry.current(empty_store)).version == 0
    assert all(set(e) <= set("9aA-:.+TZ ") for e in item["payload"]["examples"])  # shapes, not values

    effects = {"mapping_proposal": mapping_effect(empty_store, t, CIPHER, lambda sha: path)}
    out = await decide(
        empty_store, t, item_id, Reviewer(id="ann"), "approve", "German export, timestamp", effects=effects
    )
    assert out.state == "decided"
    registry = await AliasRegistry.current(empty_store)
    assert registry.version == 1 and registry.mappings == {"messages": {"zeitstempel": "sent_at"}}
    [counts] = await empty_store.run(
        "MATCH (f:SourceFile {sha256: $s}) RETURN COUNT { (f)-[:CONTAINED]->() } AS records, "
        "COUNT { (f)-[:HAD_ERROR]->(e) WHERE e.resolved_at IS NULL } AS unresolved",
        {"s": first.sha256},
    )
    assert counts == {"records": n, "unresolved": 0}
    assert await list_items(empty_store, kind="ingest_error") == []
    [m] = await empty_store.run("MATCH (m:MappingProposal) RETURN m.state AS s, m.model AS model")
    assert m["s"] == "approved" and m["model"].startswith("function")


async def test_rejected_mapping_changes_nothing_and_registry_refuses_unknown_fields(small_run, empty_store, tmp_path):
    run, _ = small_run
    path, n = _dialect_file(run, tmp_path)
    t = Tarddiad(empty_store)
    await register_upload(empty_store, t, path, uploader="tester", filename="vendor.jsonl")
    first = await ingest_file(empty_store, t, CIPHER, path)
    [item_id] = await propose_mappings(
        empty_store, t, first, model=_model({"column": "Zeitstempel", "canonical_field": "sent_at", "confidence": 0.6})
    )
    effects = {"mapping_proposal": mapping_effect(empty_store, t, CIPHER, lambda sha: path)}
    await decide(empty_store, t, item_id, Reviewer(id="ann"), "reject", "not sure", effects=effects)
    assert (await AliasRegistry.current(empty_store)).version == 0
    assert len(await list_items(empty_store, kind="ingest_error", limit=500)) == n
    assert await propose_mappings(empty_store, t, first, model=_model()) == []
    with pytest.raises(ValueError, match="not fields"):
        await AliasRegistry().extend(empty_store, "messages", {"x": "ssn"}, approved_by="ann")
    assert mask("CF10 3AT") == "AA99 9AA" and mask("Siân") == "Aaaa"


async def test_mail_stream_is_idempotent_by_message_id_and_dead_letters_reach_review(small_run, empty_store):
    from uniondeb.synth.stream import replay_docs

    run, _ = small_run
    t = Tarddiad(empty_store)
    outcomes = [await consume_mail(empty_store, t, doc, {"synthetic": "true"}) for doc in replay_docs(run)]
    assert outcomes.count("ingested") > 0 and outcomes.count("review") > 0  # the run has injected email faults
    [n] = await empty_store.run("MATCH (r:Record:Email) RETURN count(r) AS n")
    assert n["n"] == outcomes.count("ingested")
    doc = next(d for d, o in zip(replay_docs(run), outcomes, strict=True) if o == "ingested")
    assert await consume_mail(empty_store, t, doc, {"raw_sha256": "a" * 64}) == "duplicate"
    key = "email:" + (doc.get("message_id") or doc["internetMessageId"])
    [r] = await empty_store.run(
        "MATCH (f:SourceFile)-[:CONTAINED]->(r:Record {key: $k}) RETURN count(f) AS files", {"k": key}
    )
    assert r["files"] == 2  # both raw copies are linked to the one record
    lineage = await t.lineage(EntityRef(label="Record", key=key))
    assert len(lineage.source_hashes) == 1 and lineage.steps[0].activity["trigger"].startswith("mail-journal:")
    dlq = {"raw_sha256": "b" * 64, "error": "Date: missing", "peer_ip": "192.0.2.1"}
    item = await consume_dead_letter(empty_store, dlq)
    assert item and await consume_dead_letter(empty_store, dlq) is None
    assert len(await list_items(empty_store, kind="mail_dlq")) == outcomes.count("review") + 1
