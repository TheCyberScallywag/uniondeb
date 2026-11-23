# ADR 0003: Queue in the graph, in-process write-back, consoles in the API

- Status: accepted
- Date: 2026-11-22
- Evidence: `docs/security-review.md`

## Context

The plan left three build choices open or recommended a default: the job queue (D6: arq with Redis,
or Kafka), the review console (D7: Streamlit, or FastAPI pages), and how agents write back
(§3.1 principle 4: only through typed FastAPI endpoints). Building phases 2 to 9 forced each one.

## Decision

1. **The job queue is `(:Job)` nodes in the system of record** (`uniondeb.worker.queue`). An upload's
   `SourceFile` and its ingest job are then in one database, with nothing else to run, back up or
   secure, and a job's history sits beside the provenance it produced. Claiming is safe across
   workers; failures retry with backoff and then park as `dead`. Throughput is bounded by the
   agents, not by the queue. `JobQueue` has five methods, so moving to arq later is contained.
2. **Agents write back through typed functions in the worker process**, not over HTTP. The functions
   are the door: `append_tag`, `create_item` and `decide` enforce the tag rules and four-eyes
   whoever calls them, and `POST /writeback/tags` exposes the same door for an agent that runs
   elsewhere. Reads still go only through Echdynnwr extracts.
3. **The review console and Ymchwiliad are server-rendered pages in the API process**. They call the
   same action functions as the JSON API, so access checks, four-eyes and audit cannot differ
   between the two, and there is one authentication path to secure.

## Consequences

- **Outstanding: one database credential per process family.** The API and the worker each connect
  with a single service account. The roles in `uniondeb.store.roles` describe the intended split
  (`svc_ingest`, `svc_writeback`, `svc_extract_*`, `svc_investigation`, `svc_retention`) and
  Echdynnwr already accepts a runner per role, but `Services` does not yet open one connection per
  role. Until it does, the Aura roles are not the hard boundary the plan describes. This must be
  closed before real data (it is finding 1 in the security review).
- Redis is not a dependency. Kafka is needed only for the mail journal stream.
- Streamlit is not a dependency.
