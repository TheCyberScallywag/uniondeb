"""The audit chain: appends, concurrent appends, every kind of tampering, anchors, no edit path.

Verifies: UND-071
"""

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

import uniondeb.cases
from uniondeb.cases import GENESIS, AuditLog
from uniondeb.cases.audit import APPEND, event_hash
from uniondeb.cases.cli import KEY_ENV, register
from uniondeb.store import RelateRunner, migrate

KEY = b"anchor-key"


async def fill(audit: AuditLog, n: int = 5) -> None:
    for i in range(n):
        await audit.append("ifan", "case.view", case_id="c-1", target=f"Record:{i}", detail={"i": i})


async def test_chain_links_and_verifies(store, audit):
    assert (await audit.verify()).model_dump() == {
        "ok": True,
        "checked": 0,
        "head_seq": 0,
        "head_hash": GENESIS,
        "broken_at": None,
        "reason": None,
    }
    first = await audit.append("olwen", "case.open", case_id="c-1", reason="why", detail={"origin": "manual"})
    assert (first.seq, first.prev_hash, first.detail) == (1, GENESIS, {"origin": "manual"})
    await fill(audit, 30)
    result = await audit.verify()
    head = await audit.head()
    assert (result.ok, result.checked, result.head_seq, result.head_hash) == (True, 31, 31, head.hash)
    assert (await audit.verify(from_seq=20)).checked == 12
    rows = await store.run("MATCH (e:AuditEvent)-[:PREV]->(p:AuditEvent) RETURN e.seq AS e, p.seq AS p")
    assert sorted((r["e"], r["p"]) for r in rows) == [(n, n - 1) for n in range(2, 32)]


async def test_event_links_to_an_existing_case_only(store, audit):
    await store.run("CREATE (:Case {id: 'c-1'})")
    await audit.append("ifan", "case.view", case_id="c-1")
    await audit.append("ifan", "case.access_denied", case_id="nope")
    rows = await store.run("MATCH (e:AuditEvent)-[:ABOUT]->(c:Case) RETURN e.seq AS seq, c.id AS id")
    assert rows == [{"seq": 1, "id": "c-1"}]


async def test_concurrent_appends_stay_contiguous(store, audit):
    events = await asyncio.gather(*(audit.append(f"user-{i % 4}", "case.view", detail={"i": i}) for i in range(60)))
    assert sorted(e.seq for e in events) == list(range(1, 61))
    result = await audit.verify()
    assert (result.ok, result.checked) == (True, 60)


class RacingRunner:
    """Lets a writer in "another process" take the seq just before our first append lands."""

    def __init__(self, runner):
        self.runner, self.other, self.raced = runner, AuditLog(runner), False

    async def run(self, query, params=None):
        if query == APPEND and not self.raced:
            self.raced = True
            await self.other.append("rival", "case.view")
        return await self.runner.run(query, params)


async def test_append_retries_when_another_writer_wins_the_seq(store):
    audit = AuditLog(RacingRunner(store))
    event = await audit.append("ifan", "case.view")
    assert event.seq == 2
    assert [e.actor for e in await audit.events()] == ["ifan", "rival"]
    assert (await audit.verify()).ok


async def test_append_failure_that_is_not_a_race_is_raised(store, audit):
    class Broken:
        async def run(self, query, params=None):
            if query == APPEND:
                raise RuntimeError("disk full")
            return await store.run(query, params)

    with pytest.raises(RuntimeError, match="disk full"):
        await AuditLog(Broken()).append("ifan", "case.view")
    assert await audit.head() is None


async def test_events_filter_by_case_and_actor(audit):
    await audit.append("ifan", "case.view", case_id="c-1")
    await audit.append("olwen", "case.view", case_id="c-2")
    await audit.append("ifan", "case.note", case_id="c-2")
    assert [e.seq for e in await audit.events(case_id="c-2")] == [3, 2]
    assert [e.seq for e in await audit.events(actor="ifan")] == [3, 1]
    assert [e.seq for e in await audit.events(limit=1)] == [3]


# ───────────────────────────── tampering ─────────────────────────────────────────────


async def test_edited_field_is_detected(store, audit):
    await fill(audit)
    await store.run("MATCH (e:AuditEvent {seq: 3}) SET e.actor = 'someone-else'")
    result = await audit.verify()
    assert (result.ok, result.broken_at, result.checked) == (False, 3, 2)
    assert "edited" in result.reason


async def test_edit_with_recomputed_hash_breaks_the_next_link(store, audit):
    await fill(audit)
    raw = (await store.run("MATCH (e:AuditEvent {seq: 3}) RETURN e {.*} AS e"))[0]["e"] | {"reason": "rewritten"}
    await store.run(
        "MATCH (e:AuditEvent {seq: 3}) SET e.reason = $reason, e.hash = $hash",
        {"reason": "rewritten", "hash": event_hash(raw)},
    )
    result = await audit.verify()
    assert (result.ok, result.broken_at) == (False, 4)
    assert "prev_hash" in result.reason


async def test_removed_field_is_detected(store, audit):
    await fill(audit)
    await store.run("MATCH (e:AuditEvent {seq: 2}) REMOVE e.actor")
    assert (await audit.verify()).broken_at == 2


async def test_deleted_event_is_detected(store, audit):
    await fill(audit)
    await store.run("MATCH (e:AuditEvent {seq: 3}) DETACH DELETE e")
    result = await audit.verify()
    assert (result.ok, result.broken_at) == (False, 3)
    assert "missing" in result.reason
    assert (await audit.verify(from_seq=4)).broken_at == 3


async def test_deleted_genesis_is_detected(store, audit):
    await fill(audit)
    await store.run("MATCH (e:AuditEvent {seq: 1}) DETACH DELETE e")
    assert (await audit.verify()).broken_at == 1


async def test_reordered_events_are_detected(store, audit):
    await fill(audit)
    for old, new in ((2, 99), (3, 2), (99, 3)):
        await store.run("MATCH (e:AuditEvent {seq: $old}) SET e.seq = $new", {"old": old, "new": new})
    result = await audit.verify()
    assert (result.ok, result.broken_at) == (False, 2)


async def test_relinked_chain_is_detected(store, audit):
    await fill(audit)
    await store.run(
        "MATCH (e:AuditEvent {seq: 4})-[r:PREV]->() MATCH (p:AuditEvent {seq: 2}) DELETE r CREATE (e)-[:PREV]->(p)"
    )
    result = await audit.verify()
    assert (result.ok, result.broken_at) == (False, 4)
    assert "PREV" in result.reason


async def test_forged_tail_is_detected(store, audit):
    await fill(audit)
    forged = {"id": "forged", "seq": 6, "at": "2026-01-01T00:00:00+00:00", "actor": "ifan", "action": "case.view"}
    forged |= {"detail_json": "{}", "prev_hash": "f" * 64}
    await store.run("CREATE (e:AuditEvent) SET e = $props", {"props": forged | {"hash": event_hash(forged)}})
    result = await audit.verify()
    assert (result.ok, result.broken_at, result.checked) == (False, 6, 5)

    # Even with the right prev_hash, a tail that was not appended through the log has no PREV link.
    head_hash = (await store.run("MATCH (e:AuditEvent {seq: 5}) RETURN e.hash AS h"))[0]["h"]
    forged |= {"prev_hash": head_hash}
    await store.run("MATCH (e:AuditEvent {seq: 6}) SET e = $props", {"props": forged | {"hash": event_hash(forged)}})
    assert (await audit.verify()).broken_at == 6


# ───────────────────────────── anchors ───────────────────────────────────────────────


async def test_anchor_round_trip(audit, tmp_path):
    await fill(audit)
    path = tmp_path / "anchor.json"
    anchor = await audit.anchor(path, KEY)
    head = await audit.head()
    assert json.loads(path.read_text()) == anchor.model_dump()
    assert (anchor.seq, anchor.hash) == (5, head.hash)
    assert set(anchor.model_dump()) == {"seq", "hash", "at", "hmac"}
    await fill(audit, 3)  # the chain growing does not invalidate an anchor
    assert (await audit.check_anchor(path, KEY)).ok
    assert not (await audit.check_anchor(path, b"wrong-key")).ok
    assert not (await audit.check_anchor(tmp_path / "missing.json", KEY)).ok


async def test_anchor_catches_a_replaced_tail(store, audit, tmp_path):
    """Drop the last event and append a different one: the chain verifies, the anchor does not."""
    await fill(audit)
    path = tmp_path / "anchor.json"
    await audit.anchor(path, KEY)
    await store.run("MATCH (e:AuditEvent {seq: 5}) DETACH DELETE e")
    await audit.append("ifan", "case.view", detail={"innocent": True})
    assert (await audit.verify()).ok
    check = await audit.check_anchor(path, KEY)
    assert not check.ok and "anchored hash" in check.reason


async def test_anchor_catches_truncation_and_a_tampered_file(store, audit, tmp_path):
    await fill(audit)
    path = tmp_path / "anchor.json"
    await audit.anchor(path, KEY)
    await store.run("MATCH (e:AuditEvent) WHERE e.seq >= 4 DETACH DELETE e")
    assert (await audit.verify()).ok
    check = await audit.check_anchor(path, KEY)
    assert not check.ok and "no longer contains" in check.reason
    path.write_text(json.dumps(json.loads(path.read_text()) | {"seq": 3}))
    assert "signature" in (await audit.check_anchor(path, KEY)).reason


# ───────────────────────────── CLI ───────────────────────────────────────────────────


def run_cli(*argv: str) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["audit", *argv])
    return args.func(args)


def test_cli_verify_and_anchor(tmp_path, monkeypatch, capsys):
    uri, anchor = f"relate:{tmp_path / 'audit.rl'}", tmp_path / "anchor.json"

    async def prepare(tamper: bool = False) -> None:
        runner = RelateRunner.open(uri.removeprefix("relate:"))
        if tamper:
            await runner.run("MATCH (e:AuditEvent {seq: 2}) SET e.action = 'nothing.happened'")
        else:
            await migrate(runner)
            await fill(AuditLog(runner), 3)
        await runner.close()

    asyncio.run(prepare())
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert run_cli("verify", "--uri", uri) == 0
    assert json.loads(capsys.readouterr().out)["verify"]["checked"] == 3
    with pytest.raises(SystemExit, match=KEY_ENV):
        run_cli("anchor", "--uri", uri, "--out", str(anchor))
    with pytest.raises(SystemExit, match="--out"):
        run_cli("anchor", "--uri", uri)

    monkeypatch.setenv(KEY_ENV, "secret")
    assert run_cli("anchor", "--uri", uri, "--out", str(anchor)) == 0
    assert json.loads(anchor.read_text())["seq"] == 3
    assert run_cli("verify", "--uri", uri, "--anchor", str(anchor)) == 0

    asyncio.run(prepare(tamper=True))
    capsys.readouterr()
    assert run_cli("verify", "--uri", uri) == 1
    assert json.loads(capsys.readouterr().out)["verify"]["broken_at"] == 2
    assert run_cli("anchor", "--uri", uri, "--out", str(tmp_path / "second.json")) == 1
    assert not (tmp_path / "second.json").exists()


# ───────────────────────────── no update or delete path ──────────────────────────────

SOURCE = Path(uniondeb.cases.__file__).parent
CYPHER = re.compile(r"\b(MATCH|CREATE|MERGE)\b")
DOCUMENTED = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
MUTATES = re.compile(r"\b(SET|DELETE|REMOVE|MERGE)\b")


def cypher_strings() -> list[tuple[str, str]]:
    """Every string constant in the package that reads like Cypher, docstrings aside."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {ast.get_docstring(n, clean=False) for n in ast.walk(tree) if isinstance(n, DOCUMENTED)}
        for node in ast.walk(tree):
            text = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""
            if CYPHER.search(text) and text not in docstrings:
                found.append((path.name, text))
    return found


def test_no_cypher_updates_or_deletes_an_audit_event():
    statements = cypher_strings()
    touching = [(name, text) for name, text in statements if "AuditEvent" in text]
    assert len(touching) >= 4 and {name for name, _ in touching} == {"audit.py"}
    assert sum("CREATE (e:AuditEvent {" in text for _, text in touching) == 1
    for name, text in touching:
        assert not MUTATES.search(text), f"{name}: {text}"
    # Nothing in the package deletes anything at all, so no unlabelled statement can reach the log.
    for name, text in statements:
        assert not re.search(r"\b(DELETE|REMOVE)\b", text), f"{name}: {text}"


def test_audit_log_has_no_update_or_delete_method():
    public = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert public == {"append", "head", "events", "verify", "anchor", "check_anchor"}
