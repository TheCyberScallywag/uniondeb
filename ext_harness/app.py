"""The delivery platform: files and streams in, Uniondeb uploads out, status as server-sent events.

POST /files                      one or more files (multipart field ``files``), optional ``dataset``, ``source_system``
WS   /ws/stream/{dataset}        one JSON record per text frame; cut into JSONL files and delivered
GET  /events                     SSE: every delivery and stream event (replay with Last-Event-ID or ?after=)
GET  /deliveries/{id}/events     SSE: one delivery, until it reaches a final state
GET  /deliveries, /deliveries/{id}, POST /deliveries/{id}/retry, GET /health, GET / (a status page)
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse

from ext_harness.deliveries import RETRYABLE, Delivery, Hub, Registry
from ext_harness.forwarder import Forwarder, spool_path
from ext_harness.settings import DATASETS, EXTENSIONS, Settings

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
PAGE = Path(__file__).with_name("status.html")


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> FastAPI:
    """``client`` replaces the HTTP client used to reach Uniondeb (tests point it at an in-process app)."""
    settings = settings or Settings.from_env()
    hub = Hub()
    registry = Registry(hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.spool.mkdir(parents=True, exist_ok=True)
        app.state.forwarder = Forwarder(settings, registry, client)
        await app.state.forwarder.start()
        try:
            yield
        finally:
            await app.state.forwarder.stop()

    app = FastAPI(title="Uniondeb delivery harness", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.hub, app.state.registry = settings, hub, registry

    def allowed(presented: str | None) -> bool:
        return settings.api_key is None or hmac.compare_digest((presented or "").encode(), settings.api_key.encode())

    def guard(request: Request) -> None:
        if not allowed(request.headers.get("x-api-key") or request.query_params.get("key")):
            raise HTTPException(401, "present the harness key as X-API-Key (or ?key= for SSE and WebSocket clients)")

    def accept(path: Path, *, source: str, filename: str, sha: str, size: int, **fields: Any) -> Delivery:
        """Move a finished spool file under its delivery id, record the delivery and queue it."""
        delivery = Delivery(source=source, filename=filename, sha256=sha, bytes=size, **fields)
        os.replace(path, spool_path(settings, delivery))
        registry.add(delivery)
        app.state.forwarder.submit(delivery)
        return delivery

    # ───────────────────────────── files ────────────────────────────────────────────

    @app.post("/files", status_code=202, dependencies=[Depends(guard)])
    async def files(
        files: list[UploadFile],
        dataset: str | None = Form(None),
        source_system: str | None = Form(None),
    ) -> dict[str, Any]:
        """Accept files for delivery. Each becomes one delivery; a refused file does not stop the others."""
        if dataset is not None and dataset not in DATASETS:
            raise HTTPException(422, f"dataset must be one of {sorted(DATASETS)}")
        accepted, refused = [], []
        for upload in files:
            name = PurePath(upload.filename or "upload").name
            if PurePath(name).suffix.lower() not in EXTENSIONS:
                refused.append({"filename": name, "reason": f"send a {', '.join(sorted(EXTENSIONS))} file"})
                continue
            digest, size = hashlib.sha256(), 0
            fd, tmp = tempfile.mkstemp(dir=settings.spool, prefix=".incoming-")
            try:
                with os.fdopen(fd, "wb") as out:
                    while chunk := await upload.read(1 << 20):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            break
                        digest.update(chunk)
                        out.write(chunk)
                if size > settings.max_upload_bytes:
                    refused.append({"filename": name, "reason": f"larger than {settings.max_upload_bytes} bytes"})
                elif size == 0:
                    refused.append({"filename": name, "reason": "the file is empty"})
                else:
                    delivery = accept(
                        Path(tmp), source="file", filename=name, sha=digest.hexdigest(), size=size,
                        dataset=dataset, source_system=source_system,
                    )  # fmt: skip
                    accepted.append(delivery.model_dump(include={"id", "filename", "sha256", "bytes", "state"}))
            finally:
                Path(tmp).unlink(missing_ok=True)
        if not accepted:
            raise HTTPException(422, {"refused": refused})
        return {"accepted": accepted, "refused": refused}

    # ───────────────────────────── deliveries ───────────────────────────────────────

    def found(delivery_id: str) -> Delivery:
        if (delivery := registry.get(delivery_id)) is None:
            raise HTTPException(404, "no such delivery")
        return delivery

    @app.get("/deliveries", dependencies=[Depends(guard)])
    async def deliveries(state: str | None = None, limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
        return {"counts": registry.counts(), "deliveries": [d.model_dump() for d in registry.list(state)[:limit]]}

    @app.get("/deliveries/{delivery_id}", dependencies=[Depends(guard)])
    async def delivery(delivery_id: str) -> Delivery:
        return found(delivery_id)

    @app.post("/deliveries/{delivery_id}/retry", status_code=202, dependencies=[Depends(guard)])
    async def retry(delivery_id: str) -> Delivery:
        """Send a delivery again after it was rejected, failed or could not be delivered."""
        item = found(delivery_id)
        if item.state not in RETRYABLE:
            raise HTTPException(409, f"a delivery in state {item.state!r} cannot be retried")
        if not spool_path(settings, item).exists():
            raise HTTPException(410, "the spooled file is gone; send it again")
        registry.update(item, state="received", detail="retry requested", job=None)
        app.state.forwarder.submit(item)
        return item

    # ───────────────────────────── status: server-sent events ───────────────────────

    def last_seen(request: Request, after: int | None) -> int:
        header = request.headers.get("last-event-id", "")
        return after if after is not None else int(header) if header.isdigit() else 0

    @app.get("/events", dependencies=[Depends(guard)])
    async def events(request: Request, after: int | None = None, replay: bool = True) -> StreamingResponse:
        """Every event, oldest first. A reconnecting client resumes after its Last-Event-ID."""

        async def stream() -> AsyncIterator[str]:
            yield "retry: 3000\n\n"
            async for event in hub.subscribe(last_seen(request, after), replay=replay):
                yield event.sse() if event else ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/deliveries/{delivery_id}/events", dependencies=[Depends(guard)])
    async def delivery_events(delivery_id: str) -> StreamingResponse:
        """One delivery's events; the response ends when the delivery reaches a final state."""
        item = found(delivery_id)

        async def stream() -> AsyncIterator[str]:
            if item.done:
                yield f"event: delivery\ndata: {item.model_dump_json()}\n\n"
                return
            async for event in hub.subscribe():
                if event is None:
                    yield ": keep-alive\n\n"
                elif event.type == "delivery" and event.data.get("id") == delivery_id:
                    yield event.sse()
                    if item.done:
                        return

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    # ───────────────────────────── streams: WebSocket ───────────────────────────────

    @app.websocket("/ws/stream/{dataset}")
    async def stream(ws: WebSocket, dataset: str) -> None:
        """Records in, batches out.

        Each text frame is one JSON object (a record) or a JSON array of them. ``{"$control": "flush"}``
        cuts a batch now; ``{"$control": "close"}`` flushes and ends the stream. A batch is also cut every
        ``batch`` records and after ``batch_seconds`` without a new record. The server answers each batch
        with ``{"type": "batch", "delivery_id": …}`` and each unusable frame with ``{"type": "rejected", …}``.
        """
        if not allowed(ws.headers.get("x-api-key") or ws.query_params.get("key")):
            await ws.close(code=4401, reason="harness key required")
            return
        if dataset not in DATASETS:
            await ws.close(code=4404, reason=f"dataset must be one of {sorted(DATASETS)}")
            return
        await ws.accept()
        source_system = ws.query_params.get("source_system")
        size = max(1, min(int(ws.query_params.get("batch") or settings.batch_records), 100_000))
        stream_id = os.urandom(4).hex()
        lines: list[str] = []
        received = rejected = batches = 0
        hub.publish("stream", {"id": stream_id, "dataset": dataset, "state": "open"})

        async def flush() -> None:
            nonlocal batches
            if not lines:
                return
            body = ("\n".join(lines) + "\n").encode()
            count = len(lines)
            lines.clear()
            batches += 1
            fd, tmp = tempfile.mkstemp(dir=settings.spool, prefix=".stream-")
            with os.fdopen(fd, "wb") as out:
                out.write(body)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            delivery = accept(
                Path(tmp), source="stream", filename=f"stream-{dataset}-{stamp}-{stream_id}-{batches:04d}.jsonl",
                sha=hashlib.sha256(body).hexdigest(), size=len(body), records=count,
                dataset=dataset, source_system=source_system,
            )  # fmt: skip
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_json(
                    {"type": "batch", "delivery_id": delivery.id, "records": count, "received": received}
                )

        try:
            while True:
                try:
                    message = await asyncio.wait_for(ws.receive(), settings.batch_seconds)
                except TimeoutError:
                    await flush()
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text") or (message.get("bytes") or b"").decode("utf-8", "replace")
                try:
                    doc = json.loads(raw)
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and "$control" in doc:
                    await flush()
                    if doc["$control"] == "close":
                        break
                    continue
                records = doc if isinstance(doc, list) else [doc]
                for record in records:
                    if not isinstance(record, dict) or not record:
                        rejected += 1
                        await ws.send_json(
                            {"type": "rejected", "reason": "a record must be a JSON object", "frame": raw[:200]}
                        )
                        continue
                    received += 1
                    lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    if len(lines) >= size:
                        await flush()
        except WebSocketDisconnect:
            pass
        finally:
            await flush()
            summary = {"id": stream_id, "dataset": dataset, "state": "closed", "received": received, "rejected": rejected, "batches": batches}  # fmt: skip
            hub.publish("stream", summary)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_json({"type": "closed", **summary})
                await ws.close()

    # ───────────────────────────── health and the status page ───────────────────────

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "harness": "ok",
            "uniondeb_url": settings.uniondeb_url,
            "uniondeb": await app.state.forwarder.whoami(),
            "deliveries": registry.counts(),
        }

    @app.get("/", include_in_schema=False)
    async def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    return app
