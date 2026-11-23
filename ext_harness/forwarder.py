"""Send spooled files to Uniondeb and follow them through ingest.

``POST /uploads`` answers 202 with an ingest job, or 200 when it already holds the same bytes. A
transport error or a 5xx is retried with backoff; a 4xx is Uniondeb's verdict and is not. After a
202 the job and the file are polled until the job succeeds or dies.
"""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx

from ext_harness.deliveries import Delivery, Registry
from ext_harness.settings import Settings

log = logging.getLogger("ext_harness")
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def spool_path(settings: Settings, delivery: Delivery) -> Path:
    return settings.spool / f"{delivery.id}{Path(delivery.filename).suffix.lower()}"


class Forwarder:
    def __init__(self, settings: Settings, registry: Registry, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.client = client or httpx.AsyncClient(
            base_url=settings.uniondeb_url, timeout=httpx.Timeout(60.0, connect=5.0)
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._work()) for _ in range(self.settings.workers)]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.client.aclose()

    def submit(self, delivery: Delivery) -> None:
        self._queue.put_nowait(delivery.id)

    async def idle(self) -> None:
        """Return once everything submitted so far has reached a final state."""
        await self._queue.join()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.bearer()}"}

    async def whoami(self) -> dict[str, Any]:
        try:
            response = await self.client.get("/whoami", headers=self._headers())
        except (httpx.HTTPError, RuntimeError) as exc:
            return {"reachable": False, "error": str(exc) or type(exc).__name__}
        ok = response.status_code == 200
        body = response.json() if ok else {"error": f"HTTP {response.status_code}"}
        return {"reachable": True, "authorised": ok, **body}

    async def _work(self) -> None:
        while True:
            delivery_id = await self._queue.get()
            try:
                if delivery := self.registry.get(delivery_id):
                    await self.deliver(delivery)
            except Exception as exc:  # one bad delivery must not stop the worker
                log.exception("delivery %s failed", delivery_id)
                if delivery := self.registry.get(delivery_id):
                    self.registry.update(delivery, state="undeliverable", detail=f"harness error: {exc}")
            finally:
                self._queue.task_done()

    async def deliver(self, delivery: Delivery) -> Delivery:
        path = spool_path(self.settings, delivery)
        if not path.exists():
            return self.registry.update(delivery, state="undeliverable", detail="the spooled file is gone")
        form = {k: v for k, v in (("dataset", delivery.dataset), ("source_system", delivery.source_system)) if v}
        response = None
        for attempt in range(1, self.settings.retries + 1):
            self.registry.update(delivery, state="forwarding", attempts=delivery.attempts + 1, detail=None)
            try:
                with path.open("rb") as fh:
                    response = await self.client.post(
                        "/uploads",
                        headers=self._headers(),
                        data=form,
                        files={"file": (delivery.filename, fh, "application/octet-stream")},
                    )
                problem = f"Uniondeb answered HTTP {response.status_code}"
            except RuntimeError as exc:  # no credentials to present
                return self.registry.update(delivery, state="undeliverable", detail=str(exc))
            except httpx.HTTPError as exc:
                response, problem = None, f"Uniondeb unreachable: {type(exc).__name__}"
            if response is not None and response.status_code not in RETRY_STATUS:
                break
            if attempt < self.settings.retries:
                self.registry.update(delivery, detail=f"{problem}; retrying")
                await asyncio.sleep(self.settings.retry_seconds * 2 ** (attempt - 1))
        else:
            return self.registry.update(delivery, state="undeliverable", detail=problem)

        body = _json(response)
        if response.status_code == 200 and body.get("duplicate"):
            self._discard(path)
            return self.registry.update(delivery, state="duplicate", detail="Uniondeb already holds this file")
        if response.status_code != 202:
            detail = body.get("detail") or response.text[:300]
            return self.registry.update(delivery, state="rejected", detail=f"HTTP {response.status_code}: {detail}")
        self.registry.update(delivery, state="queued", job=body.get("job"))
        return await self.follow(delivery)

    async def follow(self, delivery: Delivery) -> Delivery:
        """Poll the ingest job and the file until the job ends; publish each change."""
        deadline = asyncio.get_running_loop().time() + self.settings.poll_timeout
        while asyncio.get_running_loop().time() < deadline:
            job = await self._get(f"/jobs/{delivery.job}") if delivery.job else None
            file = await self._get(f"/uploads/{delivery.sha256}") or {}
            job_state = (job or {}).get("state")
            if job_state == "succeeded" or (not delivery.job and file.get("state") in ("ingested", "unreadable")):
                self._discard(spool_path(self.settings, delivery))
                if file.get("state") == "unreadable":
                    detail = "Uniondeb could not parse the file; it is in the ingest review queue"
                    return self.registry.update(delivery, state="unreadable", uniondeb=file, detail=detail)
                return self.registry.update(delivery, state="ingested", uniondeb=file)
            if job_state == "dead":
                return self.registry.update(delivery, state="failed", uniondeb=file, detail=(job or {}).get("error"))
            if job_state == "running":
                self.registry.update(delivery, state="ingesting", uniondeb=file)
            elif job_state == "queued" and (job or {}).get("error"):
                self.registry.update(delivery, state="queued", detail=f"ingest will retry: {job['error']}")
            await asyncio.sleep(self.settings.poll_seconds)
        return self.registry.update(delivery, detail="stopped watching: Uniondeb has not finished ingest")

    async def _get(self, url: str) -> dict[str, Any] | None:
        try:
            response = await self.client.get(url, headers=self._headers())
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    def _discard(self, path: Path) -> None:
        if not self.settings.keep_delivered:
            path.unlink(missing_ok=True)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
