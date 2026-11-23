# HTTP API

Served by `uniondeb api serve` on port 8000. Every request needs a bearer token (or the sign-in
cookie); the role needed is listed for each endpoint. Errors are JSON with a `detail` field. A
resource you may not see answers `404`, the same as one that does not exist. The OpenAPI page is
switched off.

## Uploads and jobs

| | Endpoint | Role | |
|---|---|---|---|
| `POST` | `/uploads` | uploader | Multipart `file`, optional `dataset`, `source_system`. `202` with the ingest job, or `200` if a duplicate |
| `GET` | `/uploads/{sha256}` | uploader, reviewer | State and row counts of an uploaded file |
| `GET` | `/jobs/{id}` | uploader, reviewer, compliance, compliance_lead | A job's state, attempts, error and result |

## Review

| | Endpoint | Role | |
|---|---|---|---|
| `GET` | `/review/queues` | reviewer, compliance_lead | Open items per queue |
| `GET` | `/review/items?kind=&limit=` | reviewer, compliance_lead | List items |
| `GET` | `/review/items/{id}` | reviewer, compliance_lead | One item with its record and options |
| `POST` | `/review/items/{id}/decisions` | reviewer, compliance_lead | `{"decision", "rationale", "fixed_row"?, "case_pack_version"?}` |
| `POST` | `/review/alerts/{id}/identity` | compliance_lead | `{"reason"}`. Reveals an alert's subject; audited |
| `POST` | `/review/alerts/{id}/pack` | reviewer, compliance_lead | Build a case pack |
| `GET` | `/review/packs/{id}` | reviewer, compliance_lead | Read a case pack |
| `POST` | `/review/packs/{id}/rating` | reviewer, compliance_lead | `{"rating": 1–5, "comment"}` |

## Scans and dashboards

| | Endpoint | Role | |
|---|---|---|---|
| `POST` | `/scans` | compliance, compliance_lead | `{"kind": "daily"｜"weekly"｜"on_demand", "window_days"?, "window_to"?}`. Returns a job |
| `GET` | `/dashboards/concern` | compliance, compliance_lead | Concern rates by pseudonym and team from recent scans |
| `GET` | `/dashboards/evals` | compliance, compliance_lead | Reviewer agreement and fairness report |

## Cases

Roles: investigator, nominated_officer, case_admin, unless noted. An investigator must be assigned.

| | Endpoint | |
|---|---|---|
| `GET` | `/cases` | Cases you may see |
| `POST` | `/cases` | Nominated officer only. Open a case by hand: `{"person", "reason", "window_from"?, "window_to"?}` |
| `GET` | `/cases/{id}` | The case |
| `POST` | `/cases/{id}/assignments` | Nominated officer only. `{"investigator_id", "person_id"}` |
| `POST` | `/cases/{id}/identity` | `{"reason"}`. Reveal the subject |
| `GET` | `/cases/{id}/timeline`, `/contacts` | The subject's records and contact graph |
| `GET` | `/cases/{id}/records/{key}/provenance` | A record's lineage back to its source file |
| `POST` | `/cases/{id}/records/{key}/tags` | Tag a record in the case: `{"label", "rationale", "clauses"?, "context"?}` |
| `GET` `POST` | `/cases/{id}/notes` | Case notes: `{"text"}` |
| `GET` `POST` | `/cases/{id}/flags` | Flags: `{"label", "reason", "target_label"?, "target_key"?, "param"?}` |
| `POST` | `/cases/{id}/holwr` | `{"question"}`. Needs a model |
| `POST` | `/cases/{id}/sar/narrative` | Draft a narrative. Needs a model |
| `GET` `POST` | `/cases/{id}/sar/drafts` | SAR drafts |
| `PUT` | `/sar/drafts/{id}` | Replace a draft's fields |
| `POST` | `/sar/drafts/{id}/approval` | Nominated officer only. Validate and submit to the mock portal |
| `GET` | `/cases/{id}/sar/submissions` | Nominated officer and auditor only |
| `POST` | `/cases/{id}/closure` | Nominated officer only. `{"outcome", "retention_review"}` |

## Audit and identity

| | Endpoint | Role | |
|---|---|---|---|
| `GET` | `/audit/events?case_id=&actor=&limit=` | auditor | Audit events. The read is itself audited |
| `GET` | `/audit/verification` | auditor | Check the hash chain |
| `GET` | `/whoami` | any | Your subject and roles |
| `POST` | `/writeback/tags` | service | One typed tag from an agent running outside the process |

## Browser pages

`/login`, `/console` (review queues, items and case packs) and `/portal` (cases). They use the same
token in a `SameSite=Strict` cookie, and refuse cookie-authenticated writes from another origin.

## Delivery harness

A separate application on port 8090 with its own endpoints: see
[Sending data through the delivery harness](../run-harness.md).
