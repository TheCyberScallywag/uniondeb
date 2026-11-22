"""A restore drill: back up a working system, restore it elsewhere, prove nothing changed.

Verifies: UND-092
"""

import gzip
import json

import pytest

from uniondeb.cases import Actor, AuditLog, CaseService
from uniondeb.core.cli import main
from uniondeb.core.prov import EntityRef
from uniondeb.store import RelateRunner, Tarddiad, status
from uniondeb.store.crypto import CipherError, FieldCipher
from uniondeb.store.maintenance import RestoreError, backup, digest, restore, rotate_field_key


async def _with_audit(store):
    audit = AuditLog(store)
    cases = CaseService(store, audit, Tarddiad(store))
    officer = Actor(id="no1", roles=frozenset({"nominated_officer"}))
    [p] = await store.run("MATCH (p:Person {kind: 'employee'}) RETURN p.id AS id LIMIT 1")
    case = await cases.open_manual(p["id"], officer, "restore drill")
    await cases.add_note(case.id, officer, "a note")
    return case


async def test_backup_restores_to_an_identical_graph_with_a_verifying_audit_chain(tagged_store, tmp_path):
    case = await _with_audit(tagged_store)
    file = tmp_path / "backup.jsonl.gz"
    summary = await backup(tagged_store, file)
    assert summary["nodes"] > 1000 and summary["relationships"] > 1000 and oct(file.stat().st_mode)[-3:] == "600"

    target = RelateRunner.open(":memory:")
    try:
        assert await restore(target, file) == summary == await digest(target)
        assert (await AuditLog(target).verify()).ok
        assert {m["state"] for m in await status(target)} == {"applied"}  # the ledger came back; nothing re-runs
        with pytest.raises(Exception, match="(?i)constraint|uniqu"):  # constraints are back too
            await target.run("MATCH (r:Record) WITH r LIMIT 1 CREATE (:Record {key: r.key})")
        [m] = await target.run("MATCH (m:Migration) RETURN m.applied_at AS at LIMIT 1")
        assert not isinstance(m["at"], str)  # datetimes survive as datetimes
        [tag] = await target.run(
            "MATCH (t:Tag {proposed: true})-[:ON]->(r:Record) RETURN t.id AS id, r.key AS key LIMIT 1"
        )
        lineage = await Tarddiad(target).lineage(EntityRef(label="Record", key=tag["key"]))
        assert len(lineage.source_hashes) == 1  # provenance still reaches a source-file hash
        assert await target.run(
            "MATCH (c:Case {id: $id})-[:HAS]->(n:CaseNote) RETURN n.text AS t", {"id": case.id}
        ) == [{"t": "a note"}]
        with pytest.raises(RestoreError, match="empty database"):
            await restore(target, file)
        assert await target.run("MATCH (n:RestoreTmp) RETURN count(n) AS n") == [{"n": 0}]
    finally:
        await target.close()


async def test_a_damaged_backup_is_refused(small_store, tmp_path):
    file = tmp_path / "b.gz"
    await backup(small_store, file)
    with gzip.open(file, "rt") as fh:
        lines = fh.read().splitlines()
    with gzip.open(tmp_path / "short.gz", "wt") as fh:
        fh.write("\n".join(lines[:-5]) + "\n")
    (tmp_path / "other.gz").write_bytes(gzip.compress(b'{"something": "else"}\n'))
    for name, message in (("short.gz", "does not match"), ("other.gz", "not a Uniondeb backup")):
        target = RelateRunner.open(":memory:")
        try:
            with pytest.raises(RestoreError, match=message):
                await restore(target, tmp_path / name)
        finally:
            await target.close()


async def test_field_key_rotation_is_complete_and_resumable(empty_store):
    old, new, wrong = (FieldCipher(FieldCipher.generate_key()) for _ in range(3))
    await empty_store.run(
        "CREATE (:IngestError {id: 'e1', raw_enc: $a}), (:IngestError {id: 'e2', raw_enc: $b}), (:CasePack {id: 'p1', pack_enc: $c}), (:IngestError {id: 'e3'})",
        {"a": old.encrypt_json({"row": 1}), "b": new.encrypt_json({"row": 2}), "c": old.encrypt("pack")},
    )
    counts = await rotate_field_key(empty_store, old, new)
    assert counts == {"IngestError.raw_enc": 1, "CasePack.pack_enc": 1, "Extract.pseudonyms_enc": 0}
    rows = await empty_store.run(
        "MATCH (e:IngestError) WHERE e.raw_enc IS NOT NULL RETURN e.raw_enc AS v ORDER BY e.id"
    )
    assert [new.decrypt_json(r["v"]) for r in rows] == [{"row": 1}, {"row": 2}]
    with pytest.raises(CipherError):
        old.decrypt(rows[0]["v"])
    assert sum((await rotate_field_key(empty_store, old, new)).values()) == 0  # re-running changes nothing
    await empty_store.run("CREATE (:IngestError {id: 'e4', raw_enc: $x})", {"x": wrong.encrypt("lost")})
    with pytest.raises(CipherError):
        await rotate_field_key(empty_store, old, new)


def test_cli_backup_and_restore(small_run, tmp_path, capsys, monkeypatch):
    import shutil

    monkeypatch.delenv("UNIONDEB_NEO4J_URI", raising=False)
    source = tmp_path / "source.rl"
    shutil.copy(small_run[1], source)
    file = tmp_path / "b.jsonl.gz"
    assert main(["store", "--uri", f"relate:{source}", "--file", str(file), "backup"]) == 0
    saved = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["backup"]
    assert main(["store", "--uri", f"relate:{tmp_path / 'target.rl'}", "--file", str(file), "restore"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["restore"] == saved
