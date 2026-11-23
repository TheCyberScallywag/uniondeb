# ext_harness — delivery platform and test data for Uniondeb

A FastAPI application that stands where a source system would. It accepts files and record
streams, sends them to the Uniondeb API (`POST /uploads`, as an `uploader`), follows each one
through ingest, and reports progress as server-sent events. It also builds the synthetic test
data set in `data/`.

```
 files  ── POST /files ──────────┐
                                 ├─ spool ─ forwarder ── POST /uploads ──▶ Uniondeb API ─▶ worker (ingest, tag)
 records ─ WS /ws/stream/{ds} ───┘             │  ▲
                                               │  └── GET /jobs/{id}, /uploads/{sha}  (polled)
 status ◀─ GET /events (SSE) ◀─────────────────┘
```

Uniondeb has no streaming intake over HTTP, so a stream is cut into JSONL files (every 500
records, or after 5 seconds of silence) and each file is delivered like an upload.

The full guide is in `docs/run-harness.md`.

## Quick start

```sh
uv run python -m ext_harness make-data            # data/run-001, data/streams, data/edge-cases

# Uniondeb itself: the API and a worker, on the synthetic database (see the repository README)
export UNIONDEB_AUTH_SECRET=…                     # the same secret the Uniondeb API runs with
UNIONDEB_SYNTHETIC=1 uv run uniondeb api serve    # 127.0.0.1:8000
uv run uniondeb worker run --synthetic

uv run python -m ext_harness serve                # 127.0.0.1:8090 — open it for the status page
uv run python -m ext_harness send data/run-001/data --watch
uv run python -m ext_harness stream data/streams/mail-journal.ndjson --dataset email --watch
uv run python -m ext_harness watch                # tail the status events
```

`send` takes the dataset from the folder name (`…/calls/…`); `--watch` waits for every delivery
and exits non-zero unless all were ingested or were duplicates.

## Endpoints

| | | |
| --- | --- | --- |
| `POST` | `/files` | Multipart field `files` (one or more `.jsonl`, `.csv`, `.parquet`), optional `dataset` and `source_system`. `202` with a delivery id per accepted file and a reason per refused one; `422` if none was accepted. |
| `WS` | `/ws/stream/{dataset}` | `dataset` is `calls`, `messages`, `handoff` or `email`. Query: `batch` (records per file), `source_system`. |
| `GET` | `/events` | SSE, every event. Resumes after `Last-Event-ID` (or `?after=N`); `?replay=false` for new events only. |
| `GET` | `/deliveries/{id}/events` | SSE for one delivery; ends when it reaches a final state. |
| `GET` | `/deliveries`, `/deliveries/{id}` | Current state as JSON; `?state=` filters. |
| `POST` | `/deliveries/{id}/retry` | Send again after `rejected`, `failed` or `undeliverable`. |
| `GET` | `/health` | Whether Uniondeb is reachable and accepts the harness's token. |
| `GET` | `/` | A status page: upload form and live table. `/docs` has the OpenAPI page. |

### WebSocket protocol

Client → server, text frames:

- a JSON object: one record; or a JSON array of records
- `{"$control": "flush"}` cuts a batch now; `{"$control": "close"}` flushes and ends the stream

Server → client:

- `{"type": "batch", "delivery_id": "…", "records": 500, "received": 500}` for each file cut
- `{"type": "rejected", "reason": "…", "frame": "…"}` for a frame that is not a record
- `{"type": "closed", "received": N, "rejected": N, "batches": N}` at the end

Close codes: `4401` missing harness key, `4404` unknown dataset.

### Events

`event: delivery` carries the whole delivery each time it changes; `event: stream` marks a stream
opening and closing.

```
id: 42
event: delivery
data: {"at":"…","id":"8d5ddd3c4904","source":"stream","filename":"stream-email-….jsonl","dataset":"email",
       "sha256":"…","bytes":528113,"records":500,"state":"ingested","job":"…",
       "uniondeb":{"state":"ingested","rows":500,"loaded":500,"in_review":0,"linked":0,…}}
```

| State | Meaning |
| --- | --- |
| `received` → `forwarding` → `queued` → `ingesting` | on its way |
| `ingested` | Uniondeb loaded it; `uniondeb` has rows loaded, held for review (`in_review`) and already known (`linked`) |
| `duplicate` | Uniondeb already holds the same bytes |
| `unreadable` | Uniondeb took the file but could not parse it; it is one item in the ingest review queue |
| `rejected` | Uniondeb refused the upload (4xx); `detail` has its reason |
| `failed` | the ingest job died; `detail` has the error |
| `undeliverable` | Uniondeb could not be reached after the retries; the file stays in the spool for `/retry` |

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HARNESS_UNIONDEB_URL` | `http://127.0.0.1:8000` | the Uniondeb API |
| `HARNESS_UNIONDEB_TOKEN` | | a bearer token with the `uploader` role (from the OIDC provider) |
| `UNIONDEB_AUTH_SECRET` | | development only: used to mint 5-minute `uploader` tokens when no token is set |
| `HARNESS_API_KEY` | | when set, callers must send it as `X-API-Key` (or `?key=` for browsers' SSE and WebSocket) |
| `HARNESS_SPOOL` | `data/harness-spool` | where files wait until Uniondeb has them |
| `HARNESS_BATCH_RECORDS`, `HARNESS_BATCH_SECONDS` | `500`, `5` | how a stream is cut into files |
| `HARNESS_WORKERS`, `HARNESS_RETRIES`, `HARNESS_RETRY_SECONDS` | `2`, `5`, `2` | forwarding concurrency and backoff |
| `HARNESS_POLL_SECONDS`, `HARNESS_POLL_TIMEOUT` | `2`, `900` | how ingest is followed |
| `HARNESS_MAX_UPLOAD_BYTES` | 512 MiB | |
| `HARNESS_KEEP_DELIVERED` | `0` | `1` keeps spooled files after ingest |

## Limits

- It is a test harness. Without `HARNESS_API_KEY` anyone who can reach the port can deliver as the
  harness's uploader identity, so it binds to `127.0.0.1` by default. Keep it there, or set the key.
- Deliveries and event history live in memory. After a restart, files still in the spool are not
  picked up again; send them again.
- A stream batch is acknowledged when it is spooled, not when Uniondeb has ingested it. Watch the
  events for the outcome.

## Tests

```sh
uv run pytest ext_harness
```

They run the harness against the real Uniondeb application in-process (embedded store), and against
a scripted one for the failure paths. They are not part of the default `pytest` run or of CI.
