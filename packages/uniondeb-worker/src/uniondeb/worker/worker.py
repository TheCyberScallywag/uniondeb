"""The worker loop and the scheduler.

``Worker.run_once`` claims one job, runs its handler inside a Logfire span and records the outcome.
``schedule_due`` enqueues the periodic jobs whose time slot has arrived; each slot has a unique key,
so any number of workers can run the scheduler and each slot is still enqueued exactly once.
"""

import asyncio
import contextlib
import socket
from collections.abc import Callable
from datetime import UTC, datetime

import logfire

from uniondeb.worker.handlers import HANDLERS, Handler
from uniondeb.worker.services import Services

# (job kind, args, slot function: the key of the slot ``now`` falls in, or None if not due yet)
PERIODIC: tuple[tuple[str, dict, Callable[[datetime], str | None]], ...] = (
    ("reap", {}, lambda now: f"{now:%Y-%m-%dT%H}:{now.minute // 10}"),
    ("scan", {"kind": "daily"}, lambda now: f"{now:%Y-%m-%d}" if now.hour >= 2 else None),
    ("scan", {"kind": "weekly"}, lambda now: f"{now:%G-W%V}" if (now.weekday(), now.hour >= 3) >= (0, True) else None),
    ("retention", {}, lambda now: f"{now:%Y-%m-%d}" if now.hour >= 4 else None),
    ("audit_verify", {}, lambda now: f"{now:%Y-%m-%d}" if now.hour >= 1 else None),
)


async def schedule_due(services: Services, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    queued = []
    for kind, args, slot in PERIODIC:
        if (key := slot(now)) is not None:
            name = f"{kind}:{args.get('kind', 'all')}:{key}"
            before = await services.runner.run("MATCH (j:Job {unique_key: $k}) RETURN count(j) AS n", {"k": name})
            await services.queue.enqueue(kind, args, unique_key=name, once=True)
            if not before[0]["n"]:
                queued.append(name)
    return queued


class Worker:
    def __init__(
        self, services: Services, *, handlers: dict[str, Handler] | None = None, name: str | None = None
    ) -> None:
        self.services, self.handlers = services, handlers or HANDLERS
        self.name = name or f"{socket.gethostname()}:{id(self):x}"

    async def run_once(self, now: datetime | None = None) -> str | None:
        """Run one due job. Returns its final state, or ``None`` if nothing was due."""
        queue = self.services.queue
        job = await queue.claim(self.name, now)
        if job is None:
            return None
        with logfire.span("job {kind}", kind=job.kind, job_id=job.id, attempt=job.attempts):
            handler = self.handlers.get(job.kind)
            try:
                if handler is None:
                    raise LookupError(f"no handler for job kind {job.kind!r}")
                await queue.complete(job, await handler(self.services, job.args))
                return "succeeded"
            except Exception as exc:
                return await queue.fail(job, f"{type(exc).__name__}: {exc}", now)

    async def drain(self, limit: int = 1000) -> int:
        """Run until nothing is due (tests, one-shot runs). Returns the number of jobs run."""
        n = 0
        while n < limit and await self.run_once() is not None:
            n += 1
        return n

    async def run_forever(self, stop: asyncio.Event, *, idle_s: float = 2.0, schedule: bool = True) -> None:
        while not stop.is_set():
            if schedule:
                await schedule_due(self.services)
            if await self.run_once() is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_s)
