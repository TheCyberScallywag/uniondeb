"""The gateway: authentication fails closed, uploads are hashed, limited, deduplicated and queued."""

import json
import time

import jwt
import pytest

from uniondeb.api import AuthSettings, mint
from uniondeb.core.prov import EntityRef
from uniondeb.store.load import read_rows
from uniondeb.worker import Worker

from .conftest import SECRET, token


async def test_authentication_is_required_verified_and_role_checked(api):
    _, client = api
    assert (await client.get("/review/queues")).status_code == 401
    assert (await client.get("/review/queues", headers={"Authorization": "Bearer nonsense"})).status_code == 401
    forged = jwt.encode(
        {"sub": "ann", "roles": ["reviewer"], "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256"
    )
    assert (await client.get("/review/queues", headers={"Authorization": f"Bearer {forged}"})).status_code == 401
    expired = mint(SECRET, "ann", ["reviewer"], ttl_s=-10)
    assert (await client.get("/review/queues", headers={"Authorization": f"Bearer {expired}"})).status_code == 401
    unsigned = jwt.encode({"sub": "ann", "roles": ["reviewer"], "exp": int(time.time()) + 60}, None, algorithm="none")
    assert (await client.get("/review/queues", headers={"Authorization": f"Bearer {unsigned}"})).status_code == 401
    assert (await client.get("/review/queues", headers=token("uploader"))).status_code == 403
    assert (await client.get("/review/queues", headers=token("ann"))).status_code == 200
    made_up = mint(SECRET, "eve", ["superuser", "reviewer"])
    assert (await client.get("/whoami", headers={"Authorization": f"Bearer {made_up}"})).json()["roles"] == ["reviewer"]
    for path in ("/docs", "/openapi.json"):
        assert (await client.get(path)).status_code in (401, 404)


def test_auth_settings_fail_closed():
    with pytest.raises(RuntimeError, match="OIDC"):
        AuthSettings.from_env({})
    with pytest.raises(RuntimeError, match="32"):
        AuthSettings.from_env({"UNIONDEB_AUTH_SECRET": "short"})
    assert (
        AuthSettings.from_env(
            {"UNIONDEB_OIDC_JWKS_URL": "https://idp.example/jwks", "UNIONDEB_OIDC_AUDIENCE": "uniondeb"}
        ).audience
        == "uniondeb"
    )


async def test_cookie_authenticated_writes_must_be_same_origin(api):
    _, client = api
    cookie = {"Cookie": "uniondeb_token=" + mint(SECRET, "compliance", ["compliance"])}
    assert (await client.post("/scans", json={}, headers=cookie)).status_code == 403
    assert (
        await client.post("/scans", json={}, headers={**cookie, "Origin": "https://evil.example"})
    ).status_code == 403
    assert (
        await client.post("/scans", json={}, headers={**cookie, "Origin": "http://uniondeb.test"})
    ).status_code == 202
    assert (await client.get("/dashboards/concern", headers=cookie)).status_code == 200


def _file(run, pick):
    manifest = json.loads((run / "manifest.json").read_text())
    entry = next(f for f in manifest["files"] if pick(f))
    return entry, (run / entry["path"]).read_bytes()


async def test_upload_is_hashed_recorded_queued_ingested_and_deduplicated(api, small_run):
    s, client = api
    entry, data = _file(
        small_run[0], lambda f: f["dataset"] == "messages" and f["rows"] > 20 and f["format"] == "jsonl"
    )
    files = {"file": ("export.jsonl", data, "application/x-ndjson")}
    assert (await client.post("/uploads", files=files, headers=token("ann"))).status_code == 403
    r = await client.post("/uploads", files=files, data={"source_system": "smsc-1"}, headers=token("uploader"))
    assert r.status_code == 202 and r.json()["sha256"] == entry["sha256"] and r.json()["bytes"] == len(data)
    [f] = await s.runner.run(
        "MATCH (a:Activity {kind: 'Upload'})-[:GENERATED]->(f:SourceFile) RETURN f {.*} AS f, a.trigger AS by"
    )
    assert (
        f["f"]["sha256"] == entry["sha256"] and f["by"] == "uploader:uploader" and f["f"]["source_system"] == "smsc-1"
    )

    job = r.json()["job"]
    assert (await client.get(f"/jobs/{job}", headers=token("uploader"))).json()["state"] == "queued"
    await Worker(s).drain()
    done = (await client.get(f"/jobs/{job}", headers=token("uploader"))).json()
    assert done["state"] == "succeeded" and done["result"]["rows"] == entry["rows"]
    status = (await client.get(f"/uploads/{entry['sha256']}", headers=token("uploader"))).json()
    assert status["state"] == "ingested" and status["loaded"] + status["in_review"] + status["linked"] == status["rows"]

    again = await client.post("/uploads", files=files, headers=token("uploader"))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert (await s.queue.counts()).get("queued", 0) == 0
    assert (await client.get("/jobs/nope", headers=token("uploader"))).status_code == 404


async def test_upload_limits(api):
    _, client = api
    h = token("uploader")
    big = {"file": ("big.csv", b"a,b\n" + b"1,2\n" * 2_000_000, "text/csv")}
    assert (await client.post("/uploads", files=big, headers=h)).status_code == 413
    assert (
        await client.post("/uploads", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=h)
    ).status_code == 415
    assert (await client.post("/uploads", files={"file": ("x.csv", b"", "text/csv")}, headers=h)).status_code == 422
    assert (
        await client.post(
            "/uploads", files={"file": ("x.csv", b"a\n1\n", "text/csv")}, data={"dataset": "payroll"}, headers=h
        )
    ).status_code == 422
    traversal = await client.post(
        "/uploads", files={"file": ("../../etc/passwd.csv", b"a,b\n1,2\n", "text/csv")}, headers=h
    )
    assert traversal.status_code == 202


async def test_reviewer_fixes_an_ingest_error_through_the_api(api, small_run):
    s, client = api
    run = small_run[0]
    entry, _ = _file(run, lambda f: (f["dataset"], f["format"], f["dialect"]) == ("messages", "jsonl", "canonical"))
    good = next(r for r in read_rows(run / entry["path"], "jsonl") if r.get("sent_at") and r.get("body"))
    broken = {k: v for k, v in good.items() if k != "sent_at"}
    files = {"file": ("one.jsonl", (json.dumps(broken) + "\n").encode(), "application/x-ndjson")}
    await client.post("/uploads", files=files, headers=token("uploader"))
    await Worker(s).drain()

    [item] = (await client.get("/review/items?kind=ingest_error", headers=token("ann"))).json()
    detail = (await client.get(f"/review/items/{item['id']}", headers=token("ann"))).json()
    assert (
        detail["error"]["raw_row"] == broken
        and "sent_at" in detail["error"]["reason"]
        and "fix_resubmit" in detail["options"]
    )
    url = f"/review/items/{item['id']}/decisions"
    assert (
        await client.post(url, json={"decision": "fix_resubmit", "rationale": ""}, headers=token("ann"))
    ).status_code == 422
    still = await client.post(
        url, json={"decision": "fix_resubmit", "rationale": "x", "fixed_row": broken}, headers=token("ann")
    )
    assert (
        still.status_code == 422
        and (await client.get(f"/review/items/{item['id']}", headers=token("ann"))).json()["state"] == "open"
    )
    ok = await client.post(
        url,
        json={"decision": "fix_resubmit", "rationale": "timestamp restored from the SMSC log", "fixed_row": good},
        headers=token("ann"),
    )
    assert ok.status_code == 200 and ok.json()["state"] == "decided"
    key = f"messages:{good['msg_id']}"
    lineage = await s.tarddiad.lineage(EntityRef(label="Record", key=key))
    assert lineage.steps[0].activity["trigger"] == "reviewer:ann" and len(lineage.source_hashes) == 1
    await Worker(s).drain()
    [tagged] = await s.runner.run(
        "MATCH (t:Tag {current: true})-[:ON]->(:Record {key: $k}) RETURN t.label AS label", {"k": key}
    )
    assert tagged["label"] in ("clean", "question")


async def test_writeback_is_a_narrow_typed_door(api):
    s, client = api
    await s.runner.run("CREATE (:Record {key: 'messages:1'})")
    h = token("agent")
    base = {"record_key": "messages:1", "created_by": "didolwr"}
    first = await client.post(
        "/writeback/tags", json={**base, "label": "question", "method": "trigger", "trigger_hits": ["cash"]}, headers=h
    )
    assert first.status_code == 201
    assert (
        await client.post("/writeback/tags", json={**base, "label": "clean", "method": "llm"}, headers=h)
    ).status_code == 422
    assert (
        await client.post("/writeback/tags", json={**base, "label": "dirty", "method": "llm"}, headers=h)
    ).status_code == 422
    assert (
        await client.post("/writeback/tags", json={**base, "label": "dirty", "method": "human"}, headers=h)
    ).status_code == 422
    assert (
        await client.post(
            "/writeback/tags", json={**base, "label": "question", "method": "trigger"}, headers=token("ann")
        )
    ).status_code == 403
    proposal = await client.post(
        "/writeback/tags",
        json={**base, "label": "dirty", "method": "llm", "proposed": True, "clauses": ["CE1"]},
        headers=h,
    )
    assert proposal.status_code == 201 and proposal.json()["review_item"]
    [cur] = await s.runner.run("MATCH (t:Tag {current: true})-[:ON]->(:Record {key: 'messages:1'}) RETURN t.label AS l")
    assert cur["l"] == "question"
