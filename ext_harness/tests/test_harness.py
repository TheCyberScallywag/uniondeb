"""The delivery harness against an in-process Uniondeb API (embedded store) and against a scripted one."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from ext_harness.app import create_app
from ext_harness.deliveries import TERMINAL, Hub
from ext_harness.settings import Settings
from uniondeb.api import AuthSettings
from uniondeb.api import create_app as create_uniondeb
from uniondeb.store.crypto import FieldCipher
from uniondeb.worker import Services, UploadStore
from uniondeb.worker.worker import Worker

SECRET = "s" * 40


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "auth_secret": SECRET, "spool": tmp_path / "spool", "poll_seconds": 0.02, "retry_seconds": 0.01,
        "retries": 2, "batch_seconds": 0.2, "poll_timeout": 20.0,
    }  # fmt: skip
    return Settings(**{**values, **overrides})


def uniondeb(runner, tmp_path: Path) -> tuple[Services, httpx.AsyncClient]:
    services = Services(
        runner=runner,
        cipher=FieldCipher(FieldCipher.generate_key()),
        uploads=UploadStore(tmp_path / "uploads"),
        extract_root=tmp_path / "extracts",
        pseudonym_key=b"k" * 32,
    )
    app = create_uniondeb(services, AuthSettings(secret=SECRET))
    return services, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://uniondeb.test")


def scripted(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://uniondeb.test")


@asynccontextmanager
async def running(config: Settings, upstream: httpx.AsyncClient):
    app = create_app(config, upstream)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://harness") as http,
    ):
        yield app, http


async def settle(http: httpx.AsyncClient, delivery_id: str, services: Services | None = None) -> dict:
    """Wait for a delivery to finish, running Uniondeb's queued jobs meanwhile."""
    for _ in range(2000):
        delivery = (await http.get(f"/deliveries/{delivery_id}")).json()
        if delivery["state"] in TERMINAL:
            return delivery
        if services is not None:
            await Worker(services).run_once()
        await asyncio.sleep(0.01)
    raise AssertionError(f"delivery still {delivery['state']}")


def a_file(run: Path, dataset: str = "messages", suffix: str = ".jsonl") -> Path:
    return sorted((run / "data" / "normal" / dataset).rglob(f"*{suffix}"))[0]


async def test_a_file_is_delivered_ingested_and_reported(small_run, empty_store, tmp_path):
    services, upstream = uniondeb(empty_store, tmp_path)
    path = a_file(small_run[0])
    async with running(settings(tmp_path), upstream) as (_, http):
        r = await http.post("/files", data={"dataset": "messages"}, files={"files": (path.name, path.read_bytes())})
        assert r.status_code == 202, r.text
        accepted = r.json()["accepted"][0]
        delivery = await settle(http, accepted["id"], services)
        assert delivery["state"] == "ingested"
        assert delivery["uniondeb"]["state"] == "ingested" and delivery["uniondeb"]["loaded"] > 0
        assert not list((tmp_path / "spool").iterdir())  # delivered files leave the spool

        events = (await http.get(f"/deliveries/{accepted['id']}/events")).text
        assert "event: delivery" in events and '"state":"ingested"' in events

        again = await http.post("/files", files={"files": (path.name, path.read_bytes())})
        assert (await settle(http, again.json()["accepted"][0]["id"], services))["state"] == "duplicate"
    stored = await empty_store.run(
        "MATCH (f:SourceFile {sha256: $sha}) RETURN f.uploaded_by AS by", {"sha": accepted["sha256"]}
    )
    assert stored and stored[0]["by"] == "ext-harness"


async def test_files_the_harness_refuses(tmp_path):
    async with running(settings(tmp_path), scripted(lambda request: httpx.Response(200, json={"duplicate": True}))) as (
        _,
        http,
    ):
        r = await http.post("/files", files=[("files", ("empty.jsonl", b"")), ("files", ("notes.txt", b"hello"))])
        assert r.status_code == 422
        assert {x["filename"] for x in r.json()["detail"]["refused"]} == {"empty.jsonl", "notes.txt"}
        r = await http.post("/files", files=[("files", ("ok.csv", b"a,b\n1,2\n")), ("files", ("notes.txt", b"hello"))])
        assert r.status_code == 202 and len(r.json()["accepted"]) == 1 and len(r.json()["refused"]) == 1
        assert (
            await http.post("/files", data={"dataset": "nope"}, files={"files": ("ok.csv", b"a\n1\n")})
        ).status_code == 422
        tiny = settings(tmp_path, max_upload_bytes=4)
    async with running(tiny, scripted(lambda request: httpx.Response(500))) as (_, http):
        r = await http.post("/files", files={"files": ("big.csv", b"a,b\n1,2\n")})
        assert r.status_code == 422 and "larger than" in r.json()["detail"]["refused"][0]["reason"]
        assert not [p for p in (tmp_path / "spool").iterdir()]


async def test_an_unreachable_uniondeb_is_retried_then_reported_and_can_be_retried_by_hand(tmp_path):
    calls, up = [], False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/uploads":
            calls.append(request)
            if not up:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"sha256": "x", "duplicate": True})
        return httpx.Response(404)

    async with running(settings(tmp_path), scripted(handler)) as (_, http):
        r = await http.post("/files", files={"files": ("a.csv", b"a,b\n1,2\n")})
        delivery = await settle(http, r.json()["accepted"][0]["id"])
        assert delivery["state"] == "undeliverable" and "unreachable" in delivery["detail"]
        assert len(calls) == 2 and delivery["attempts"] == 2
        assert len(list((tmp_path / "spool").iterdir())) == 1  # kept for the retry
        up = True
        assert (await http.post(f"/deliveries/{delivery['id']}/retry")).status_code == 202
        assert (await settle(http, delivery["id"]))["state"] == "duplicate"
        assert (await http.post(f"/deliveries/{delivery['id']}/retry")).status_code == 409


async def test_a_refusal_from_uniondeb_is_final_and_carries_its_reason(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(403, json={"detail": "needs one of the roles: uploader"})

    async with running(settings(tmp_path), scripted(handler)) as (_, http):
        r = await http.post("/files", files={"files": ("a.csv", b"a,b\n1,2\n")})
        delivery = await settle(http, r.json()["accepted"][0]["id"])
    assert delivery["state"] == "rejected" and "uploader" in delivery["detail"] and len(calls) == 1


async def test_a_dead_ingest_job_is_reported_as_failed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/uploads":
            return httpx.Response(202, json={"sha256": "x", "job": "j1", "duplicate": False})
        if request.url.path == "/jobs/j1":
            return httpx.Response(200, json={"id": "j1", "state": "dead", "error": "boom"})
        return httpx.Response(200, json={"state": "uploaded"})

    async with running(settings(tmp_path), scripted(handler)) as (_, http):
        r = await http.post("/files", files={"files": ("a.csv", b"a,b\n1,2\n")})
        delivery = await settle(http, r.json()["accepted"][0]["id"])
    assert delivery["state"] == "failed" and delivery["detail"] == "boom" and delivery["job"] == "j1"


async def test_the_harness_key_is_required_when_one_is_set(tmp_path):
    config = settings(tmp_path, api_key="k" * 24)
    async with running(config, scripted(lambda request: httpx.Response(200, json={"duplicate": True}))) as (_, http):
        assert (await http.get("/deliveries")).status_code == 401
        assert (await http.get("/deliveries", headers={"X-API-Key": "wrong"})).status_code == 401
        assert (await http.get("/deliveries", headers={"X-API-Key": "k" * 24})).status_code == 200
        assert (await http.get("/deliveries", params={"key": "k" * 24})).status_code == 200
        assert (await http.get("/health")).status_code == 200


def test_a_stream_is_cut_into_batches_and_each_batch_is_delivered(tmp_path):
    uploads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/uploads":
            uploads.append(request.content)
        return httpx.Response(200, json={"duplicate": True})

    config = settings(tmp_path, api_key="k" * 24)
    with TestClient(create_app(config, scripted(handler))) as client:
        with client.websocket_connect("/ws/stream/messages?batch=2&source_system=sms-gw&key=" + "k" * 24) as ws:
            for n in range(3):
                ws.send_text(json.dumps({"msg_id": f"m{n}"}))
            ws.send_text("not json")
            ws.send_text(json.dumps([{"msg_id": "m3"}, {"msg_id": "m4"}]))
            ws.send_text(json.dumps({"$control": "close"}))
            replies = []
            while (reply := ws.receive_json())["type"] != "closed":
                replies.append(reply)
        assert [r["records"] for r in replies if r["type"] == "batch"] == [2, 2, 1]
        assert [r["type"] for r in replies].count("rejected") == 1
        assert reply["received"] == 5 and reply["rejected"] == 1 and reply["batches"] == 3
        for _ in range(200):
            listing = client.get("/deliveries", headers={"X-API-Key": "k" * 24}).json()
            if listing["counts"] == {"duplicate": 3}:
                break
            asyncio.run(asyncio.sleep(0.01))
        assert listing["counts"] == {"duplicate": 3}
        assert all(d["source"] == "stream" and d["filename"].endswith(".jsonl") for d in listing["deliveries"])
    assert len(uploads) == 3
    assert sum(body.count(b'{"msg_id":') for body in uploads) == 5
    assert all(b'name="dataset"\r\n\r\nmessages' in body and b"sms-gw" in body for body in uploads)


def test_a_stream_flushes_when_it_goes_quiet_and_when_the_client_leaves(tmp_path):
    with TestClient(
        create_app(settings(tmp_path), scripted(lambda r: httpx.Response(200, json={"duplicate": True})))
    ) as client:
        with client.websocket_connect("/ws/stream/calls") as ws:
            ws.send_text(json.dumps({"call_id": "c1"}))
            assert ws.receive_json()["records"] == 1  # nothing else arrived within batch_seconds
            ws.send_text(json.dumps({"call_id": "c2"}))
        for _ in range(200):
            if client.get("/deliveries").json()["counts"].get("duplicate") == 2:
                break
            asyncio.run(asyncio.sleep(0.01))
        assert client.get("/deliveries").json()["counts"] == {"duplicate": 2}


def test_a_stream_needs_a_known_dataset_and_the_key(tmp_path):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    config = settings(tmp_path, api_key="k" * 24)
    with TestClient(create_app(config, scripted(lambda r: httpx.Response(500)))) as client:
        for url, code in (("/ws/stream/calls", 4401), ("/ws/stream/nope?key=" + "k" * 24, 4404)):
            with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(url):
                pass
            assert refused.value.code == code


async def test_events_replay_from_the_last_seen_id_then_continue_live():
    hub = Hub()
    for n in range(3):
        hub.publish("delivery", {"n": n})
    stream = hub.subscribe(after=1)
    assert [(await anext(stream)).data["n"] for _ in range(2)] == [1, 2]
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    hub.publish("stream", {"n": 3})
    event = await pending
    assert event.id == 4 and event.sse().startswith("id: 4\nevent: stream\ndata: {")
    await stream.aclose()
