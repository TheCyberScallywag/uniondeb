"""A durable job queue kept in the system of record.

Jobs are ``(:Job)`` nodes: enqueueing an upload and recording its ``SourceFile`` then live in the
same database, with no second system to keep consistent, back up or secure, and a job's history is
queryable next to the provenance it produced. Throughput is bounded by the agents, not the queue.
(The plan's decision D6 suggested arq with Redis; ``JobQueue`` is small enough to swap for it.)

A job is claimed by writing a lock property first and re-checking its state after, so two workers
cannot both take it. A failed job is retried with exponential backoff until ``max_attempts``, then
parked as ``dead`` for a person to look at. A job whose worker died is requeued after a timeout.
Handlers must therefore be idempotent, which every pipeline step is.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from uniondeb.store.runner import CypherRunner

ACTIVE = ("queued", "running")


class Job(BaseModel):
    id: str
    kind: str
    args: dict[str, Any]
    state: str
    attempts: int = 0
    max_attempts: int = 3
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    unique_key: str | None = None


def _job(node: dict[str, Any]) -> Job:
    return Job(
        **{
            k: node.get(k)
            for k in ("id", "kind", "state", "created_at", "started_at", "finished_at", "error", "unique_key")
        },
        attempts=node.get("attempts", 0),
        max_attempts=node.get("max_attempts", 3),
        args=json.loads(node.get("args_json") or "{}"),
        result=json.loads(node["result_json"]) if node.get("result_json") else None,
    )


class JobQueue:
    def __init__(self, runner: CypherRunner, *, backoff_s: float = 30.0, stale_after_s: float = 1800.0) -> None:
        self.runner, self.backoff_s, self.stale_after_s = runner, backoff_s, stale_after_s

    async def enqueue(
        self,
        kind: str,
        args: dict[str, Any] | None = None,
        *,
        unique_key: str | None = None,
        once: bool = False,
        max_attempts: int = 3,
        delay_s: float = 0.0,
    ) -> str:
        """Add a job and return its id. With ``unique_key``, an active job with that key is returned
        instead of adding another; with ``once`` as well, so is a finished one (scheduled slots)."""
        if unique_key:
            states = None if once else list(ACTIVE)
            found = await self.runner.run(
                "MATCH (j:Job {unique_key: $key}) WHERE $states IS NULL OR j.state IN $states RETURN j.id AS id LIMIT 1",
                {"key": unique_key, "states": states},
            )
            if found:
                return found[0]["id"]
        now = datetime.now(UTC)
        job_id = str(uuid.uuid7())
        await self.runner.run(
            "CREATE (j:Job) SET j = $props",
            {
                "props": {
                    "id": job_id,
                    "kind": kind,
                    "args_json": json.dumps(args or {}, default=str),
                    "state": "queued",
                    "attempts": 0,
                    "max_attempts": max_attempts,
                    "created_at": now.isoformat(),
                    "run_after_ts": now.timestamp() + delay_s,
                    "unique_key": unique_key,
                }
            },
        )
        return job_id

    async def claim(self, worker: str, now: datetime | None = None) -> Job | None:
        now = now or datetime.now(UTC)
        await self.runner.run(
            "MATCH (j:Job {state: 'running'}) WHERE j.started_ts < $cutoff "
            "SET j.state = 'queued', j.error = 'worker lost; requeued', j.run_after_ts = $now",
            {"cutoff": now.timestamp() - self.stale_after_s, "now": now.timestamp()},
        )
        for _ in range(5):
            due = await self.runner.run(
                "MATCH (j:Job {state: 'queued'}) WHERE j.run_after_ts <= $now "
                "RETURN j.id AS id ORDER BY j.run_after_ts, j.id LIMIT 1",
                {"now": now.timestamp()},
            )
            if not due:
                return None
            rows = await self.runner.run(
                "MATCH (j:Job {id: $id}) SET j.lock = $token WITH j WHERE j.state = 'queued' "
                "SET j.state = 'running', j.worker = $worker, j.attempts = j.attempts + 1, "
                "j.started_at = $iso, j.started_ts = $ts RETURN j {.*} AS job",
                {
                    "id": due[0]["id"],
                    "token": str(uuid.uuid4()),
                    "worker": worker,
                    "iso": now.isoformat(),
                    "ts": now.timestamp(),
                },
            )
            if rows:
                return _job(rows[0]["job"])
        return None

    async def complete(self, job: Job, result: dict[str, Any] | None = None) -> None:
        await self.runner.run(
            "MATCH (j:Job {id: $id}) SET j.state = 'succeeded', j.finished_at = $now, j.result_json = $result, j.error = null",
            {"id": job.id, "now": datetime.now(UTC).isoformat(), "result": json.dumps(result or {}, default=str)},
        )

    async def fail(self, job: Job, error: str, now: datetime | None = None) -> str:
        """Record a failure; returns the new state: ``queued`` (will retry) or ``dead``."""
        now = now or datetime.now(UTC)
        state = "queued" if job.attempts < job.max_attempts else "dead"
        await self.runner.run(
            "MATCH (j:Job {id: $id}) SET j.state = $state, j.error = $error, j.finished_at = $iso, j.run_after_ts = $after",
            {
                "id": job.id,
                "state": state,
                "error": error[:500],
                "iso": now.isoformat(),
                "after": now.timestamp() + self.backoff_s * 2 ** (job.attempts - 1),
            },
        )
        return state

    async def get(self, job_id: str) -> Job | None:
        rows = await self.runner.run("MATCH (j:Job {id: $id}) RETURN j {.*} AS job", {"id": job_id})
        return _job(rows[0]["job"]) if rows else None

    async def counts(self) -> dict[str, int]:
        rows = await self.runner.run("MATCH (j:Job) RETURN j.state AS state, count(j) AS n")
        return {r["state"]: r["n"] for r in rows}
