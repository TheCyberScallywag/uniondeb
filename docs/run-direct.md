# Sending data directly

There are three ways to put data into Uniondeb without the delivery harness. Start the stack
first ([Getting started](getting-started.md)) and build the [test data](test-data.md).

| Way | Use it when | Goes through review and tagging? |
|---|---|---|
| [Upload to the API](#upload-to-the-api) | You want the real path a source system would use | Yes |
| [Bulk-load a run](#bulk-load-a-synthetic-run) | You want a whole synthetic run in the database quickly | No: it loads records and the roster only |
| [Journal email over SMTP](#journal-email) | You are testing the mail receiver | The receiver publishes; see the note there |

## Upload to the API

`POST /uploads` takes one file per request, as multipart form data, from a caller with the
`uploader` role.

```sh
set -a; . deploy/.env; set +a
TOKEN=$(uv run uniondeb api token --sub colin --roles uploader)

curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@data/run-001/data/normal/calls/date=2026-10-05/part-0000.jsonl" \
     -F dataset=calls \
     http://localhost:8000/uploads
```

```json
{"sha256": "877a0c56…", "bytes": 15624, "duplicate": false, "job": "01a207f9-…"}
```

| Field | |
|---|---|
| `file` | A `.jsonl`, `.csv` or `.parquet` file, up to 512 MiB |
| `dataset` | Optional: `calls`, `messages`, `handoff` or `email`. Left out, Porthor works it out from the columns |
| `source_system` | Optional: a label for where the file came from |

| Status | Meaning |
|---|---|
| `202` | Stored and queued. `job` is the ingest job |
| `200` with `"duplicate": true` | These exact bytes were uploaded before; nothing is queued |
| `413`, `415`, `422` | Too large; not an accepted file type; empty or an unknown dataset |

Follow it:

```sh
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/<job>
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/uploads/<sha256>
```

```json
{"state": "ingested", "dataset": "calls", "format": "csv", "rows": 43, "loaded": 43, "in_review": 0, "linked": 0}
```

`loaded` rows are new records, `in_review` rows failed validation and are waiting in the
`ingest_error` queue, and `linked` rows were already known. A `state` of `unreadable` means the
file could not be parsed and is one review item.

To upload a whole run:

```sh
find data/run-001/data -type f -name 'part-*' | while read -r f; do
  curl -s -H "Authorization: Bearer $TOKEN" -F "file=@$f" http://localhost:8000/uploads; echo
done
```

Development tokens last an hour. The worker ingests each file and then tags it; nothing else is
needed. If you uploaded this way, [load the roster](test-data.md#the-roster) before running a scan.

## Bulk-load a synthetic run

```sh
set -a; . deploy/.env; set +a
uv run uniondeb load data/run-001
```

The loader reads the run's `manifest.json`, checks each file's SHA-256, and writes records,
devices, people and the roster with provenance. It refuses any target but the synthetic database.

It does **not** create review items for faulty rows and does **not** tag anything: those belong
to the upload path. Queue tagging for a loaded file with:

```sh
uv run uniondeb worker enqueue tag --args '{"sha256": "<file sha256>"}' --synthetic
```

Inside the containers the same commands are `docker compose run --rm uniondeb load run-001`
(paths are relative to `./data`).

## Journal email

Cennad is the SMTP endpoint a mail platform journals to. It stores each raw message under its
SHA-256, parses Microsoft 365 envelope reports and Google Workspace BCC copies into the email
contract, and publishes canonical JSON to the `mail-journal` topic, or to `mail-journal-dlq` if it
cannot.

```sh
uv run uniondeb cennad parse report.eml     # what one message would be published as
uv run uniondeb cennad serve                # the receiver; settings from CENNAD_* variables
```

!!! note "The stream is not wired into the stack"
    Nothing in `deploy/compose.yml` runs Cennad or a consumer for its topics, and the API has no
    HTTP intake for the stream. To put journaled email through the running system today, send it
    as `email` files: upload them, or stream them through the
    [delivery harness](run-harness.md#stream-records), which cuts a stream into files.

## Next

[Review, scans and cases](review-and-cases.md).
