"""Jobs run asynchronously with retries; scheduled slots run once; retention honours legal hold."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from uniondeb.agents.porthor import register_upload
from uniondeb.cases import Actor
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.tags import TagWrite, append_tag
from uniondeb.worker import JobQueue, Services, UploadStore, Worker
from uniondeb.worker.retention import RetentionSchedule, run_retention
from uniondeb.worker.services import UploadTooLarge
from uniondeb.worker.worker import schedule_due


def services(runner, tmp_path) -> Services:
    return Services(
        runner=runner,
        cipher=FieldCipher(FieldCipher.generate_key()),
        uploads=UploadStore(tmp_path / "uploads"),
        extract_root=tmp_path / "extracts",
        pseudonym_key=b"k" * 32,
    )


async def test_claim_is_exclusive_retries_back_off_and_exhausted_jobs_are_parked(empty_store):
    q = JobQueue(empty_store, backoff_s=60)
    job_id = await q.enqueue("ingest", {"sha256": "ab"}, max_attempts=2)
    assert await q.enqueue("x", unique_key="k") == await q.enqueue("x", unique_key="k")
    now = datetime.now(UTC)
    job = await q.claim("w1", now)
    assert job.id == job_id and job.attempts == 1 and job.args == {"sha256": "ab"}
    assert (await q.claim("w2", now)).kind == "x" and await q.claim("w3", now) is None
    assert await q.fail(job, "boom", now) == "queued"
    assert await q.claim("w1", now + timedelta(seconds=30)) is None  # backing off
    again = await q.claim("w1", now + timedelta(seconds=61))
    assert again.attempts == 2 and await q.fail(again, "boom again", now) == "dead"
    stored = await q.get(job_id)
    assert (stored.state, stored.error) == ("dead", "boom again") and (await q.counts())["dead"] == 1


async def test_a_job_whose_worker_died_is_requeued(empty_store):
    q = JobQueue(empty_store, stale_after_s=600)
    await q.enqueue("reap")
    now = datetime.now(UTC)
    assert await q.claim("w1", now) and await q.claim("w2", now + timedelta(seconds=300)) is None
    assert (await q.claim("w2", now + timedelta(seconds=601))).attempts == 2


async def test_upload_is_ingested_and_tagged_by_the_worker(small_run, empty_store, tmp_path):
    run, _ = small_run
    s = services(empty_store, tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    entry = next(f for f in manifest["files"] if f["dataset"] == "messages" and f["rows"] > 20)
    with (run / entry["path"]).open("rb") as fh:
        sha, size, new = s.uploads.save(fh, max_bytes=10_000_000)
    assert (sha, size, new) == (entry["sha256"], entry["bytes"], True)
    with (run / entry["path"]).open("rb") as fh:
        assert s.uploads.save(fh, max_bytes=10_000_000)[2] is False  # stored once
    with pytest.raises(UploadTooLarge):
        s.uploads.save(io.BytesIO(b"x" * 100), max_bytes=10)
    assert not list(s.uploads.root.glob(".upload-*"))
    await register_upload(empty_store, s.tarddiad, s.uploads.path(sha), uploader="u", filename="m.x")
    job = await s.queue.enqueue("ingest", {"sha256": sha}, unique_key=f"ingest:{sha}")
    assert await Worker(s).drain() == 2  # ingest, then the tag job it queued
    done = await s.queue.get(job)
    assert done.state == "succeeded" and done.result["loaded"] > 0 and done.result["rows"] == entry["rows"]
    [n] = await empty_store.run(
        "MATCH (r:Record) WHERE NOT EXISTS { (:Tag {current: true})-[:ON]->(r) } RETURN count(r) AS n"
    )
    assert n["n"] == 0


async def test_failing_handler_is_retried_then_dead_and_unknown_kinds_fail(empty_store, tmp_path):
    s = services(empty_store, tmp_path)
    s.queue.backoff_s = 0
    calls = []

    async def flaky(_s, args):
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("transient")
        return {"ok": True}

    w = Worker(s, handlers={"flaky": flaky})
    a = await s.queue.enqueue("flaky")
    b = await s.queue.enqueue("mystery", max_attempts=1)
    assert await w.drain() == 3
    assert (await s.queue.get(a)).state == "succeeded" and (await s.queue.get(a)).attempts == 2
    dead = await s.queue.get(b)
    assert dead.state == "dead" and "no handler" in dead.error


async def test_scheduled_slots_are_enqueued_exactly_once(empty_store, tmp_path):
    s = services(empty_store, tmp_path)
    monday = datetime(2026, 9, 14, 5, 5, tzinfo=UTC)
    first = await schedule_due(s, monday)
    assert {n.split(":")[0] + ":" + n.split(":")[1] for n in first} == {
        "reap:all",
        "scan:daily",
        "scan:weekly",
        "retention:all",
        "audit_verify:all",
    }
    assert await schedule_due(s, monday + timedelta(minutes=3)) == []
    assert [n.split(":")[0] for n in await schedule_due(s, monday + timedelta(minutes=10))] == ["reap"]
    assert await schedule_due(s, datetime(2026, 9, 15, 0, 30, tzinfo=UTC)) == ["reap:all:2026-09-15T00:3"]
    assert await Worker(s).drain() > 0 and (await s.queue.counts()).get("dead", 0) == 0  # every periodic job runs clean
    [scans] = await empty_store.run("MATCH (s:Scan) RETURN count(s) AS n")
    assert scans["n"] == 2


async def test_retention_purges_content_by_tag_keeps_metadata_and_honours_legal_hold(small_store, tmp_path):
    s = services(small_store, tmp_path)
    schedule = RetentionSchedule.load().model_copy(
        update={"content_days": {"clean": 30, "question": 180, "dirty": 2190, "untagged": 30}}
    )
    rows = await small_store.run(
        "MATCH (p:Person {kind: 'employee'})-[:USES]->(:Device)<-[:FROM|TO]-(r:Record) WHERE r.body IS NOT NULL "
        "RETURN p.id AS person, collect(DISTINCT r.key)[..3] AS keys ORDER BY size(keys) DESC LIMIT 2"
    )
    held_person, held_keys = rows[0]["person"], rows[0]["keys"]
    question_key = rows[1]["keys"][0]
    await append_tag(
        small_store,
        TagWrite(record_key=question_key, label="question", method="trigger", created_by="t", trigger_hits=["cash"]),
    )
    officer = Actor(id="no1", roles=frozenset({"nominated_officer"}))
    case = await s.cases.open_manual(held_person, officer, "test hold")
    await s.flags.set_flag(case.id, officer, "legal_hold", reason="litigation")

    now = datetime(2026, 9, 2, tzinfo=UTC) + timedelta(days=40)  # clean content is due, question is not
    result = await run_retention(small_store, s.tarddiad, s.flags, schedule=schedule, now=now)
    assert result.purged["untagged"] > 0 and result.held >= len(held_keys) and result.purged["question"] == 0
    kept = await small_store.run(
        "UNWIND $k AS k MATCH (r:Record {key: k}) RETURN r.body AS body, r.content_purged_at AS at",
        {"k": [*held_keys, question_key]},
    )
    assert all(r["body"] and r["at"] is None for r in kept)
    [purged] = await small_store.run(
        "MATCH (r:Record) WHERE r.content_purged_at IS NOT NULL "
        "RETURN count(r) AS n, count(r.body) AS bodies, count(r.transcript) AS transcripts, count(r.occurred_at) AS dated, count(r.key) AS keyed"
    )
    assert purged["n"] == result.purged["untagged"] and purged["bodies"] == purged["transcripts"] == 0
    assert purged["dated"] == purged["keyed"] == purged["n"]  # metadata stays
    [prov] = await small_store.run(
        "MATCH (a:Activity {kind: 'Retention'})-[:USED]->(r:Record) RETURN count(DISTINCT r) AS n, collect(DISTINCT a.cfg_retention_schedule) AS v"
    )
    assert prov == {"n": purged["n"], "v": ["draft-0"]}
    again = await run_retention(small_store, s.tarddiad, s.flags, schedule=schedule, now=now)
    assert sum(again.purged.values()) == 0  # idempotent
    partial = tmp_path / "retention.yaml"
    partial.write_text("version: x\ncontent_days: {clean: 1}\ningest_error_raw_days: 1\n")
    with pytest.raises(ValueError, match="no period"):
        RetentionSchedule.load(partial)


async def test_the_environment_selects_the_synthetic_database_when_the_caller_does_not(monkeypatch, tmp_path):
    """`worker run` passes synthetic=None: UNIONDEB_SYNTHETIC=1 must still take effect (it did not)."""
    from contextlib import asynccontextmanager

    from uniondeb.store.crypto import FieldCipher
    from uniondeb.worker import services as module

    seen = []

    @asynccontextmanager
    async def fake_open(settings):
        seen.append(settings.target_database)
        yield object()

    monkeypatch.setattr(module, "open_runner", fake_open)
    monkeypatch.setattr(module.Services, "__post_init__", lambda self: None)
    env = {
        "UNIONDEB_PSEUDONYM_KEY": "k" * 32, "UNIONDEB_FIELD_KEY": FieldCipher.generate_key(),
        "UNIONDEB_DATA_DIR": str(tmp_path), "UNIONDEB_EXTRACT_ROOT": str(tmp_path / "x"),
    }  # fmt: skip
    for extra, kwargs, expected in (
        ({"UNIONDEB_SYNTHETIC": "1"}, {"synthetic": None}, "uniondeb-synthetic"),
        ({"UNIONDEB_SYNTHETIC": "1"}, {}, "uniondeb-synthetic"),
        ({}, {"synthetic": None}, "neo4j"),
        ({}, {"synthetic": True}, "uniondeb-synthetic"),
    ):
        async with module.Services.from_env({**env, **extra}, **kwargs):
            pass
        assert seen[-1] == expected, (extra, kwargs)
