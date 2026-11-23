# Security review

- Date: 2026-11-22
- Scope: a code review of the gateway API, both consoles, the worker, the agents, Echdynnwr and the
  case domain, by the same engineer that built them.
- **Not done: an independent penetration test.** One is required before a pilot. This document is the
  input to it, not a substitute. Nothing here has been tested by a second party or against a
  deployed environment.

## Fixed during the review

| # | Finding | Fix |
|---|---|---|
| F1 | The OIDC key set was fetched on every request (slow, and a way to make the API hammer the identity provider). | One cached JWKS client per provider (`uniondeb.api.auth`). |
| F2 | A reviewer's rejected "corrected row" echoed the validation error, including the row's values, into a redirect URL, where access logs would keep it. | Errors name the field and rule only, never the value. |
| F3 | Opening a review item shows record content but left no trace; only the portal audited reads. | `review.view_item` audit event on every open, in the API and the console. |
| F4 | Pages had no content-security, framing or sniffing headers. | `Content-Security-Policy: default-src 'none'`, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store` on every response. |
| F5 | A crafted backup file could have injected Cypher through a label name on restore. | Names from a backup are quoted so they can only be names. |
| F6 | The OpenAPI document was served without authentication. | Disabled, with the interactive docs. |

## Open, to fix before any real data

| # | Severity | Finding |
|---|---|---|
| 1 | High | **One database credential per process.** The API and worker each use a single service account, so the fine-grained Aura roles do not yet bound what a bug in one component can read (ADR 0003). Open one connection per role in `Services`. |
| 2 | High | **No independent penetration test** and no review of a deployed environment (TLS termination, network policy, secrets storage, backups). |
| 3 | Medium | **Uploads are read whole into memory** by the ingest job (up to the 512 MB upload limit, more for Parquet once decoded). A hostile or careless uploader can exhaust a worker. Stream by row group and cap rows per file. |
| 4 | Medium | **Holwr queries have no time or memory limit.** They are read-only and row-capped, but a deliberately expensive query on a large case extract can stall a request. Run them with a deadline in a worker process. |
| 5 | Medium | **No rate limiting** on any endpoint, including sign-in and upload. Expected at the gateway in front of the API; not configured anywhere in this repository. |
| 6 | Medium | **Holwr questions are stored in the audit log** as typed. Auditors are meant to see no content, but an investigator may type a name or a quotation into a question. Store a hash and length for auditors, and the text for the nominated officer only. |
| 7 | Low | **Sign-in CSRF** on the development token form (an attacker could sign a victim in as the attacker). Development only; the form does not exist under OIDC. |
| 8 | Low | **The pseudonym key cannot be rotated** without breaking trend continuity in dashboards. Decide a rotation period and accept the break, or version the pseudonyms. |
| 9 | Low | **Extract files are plaintext** by design (ADR 0002). The compose stack mounts them on tmpfs; a production deployment must provide an encrypted ephemeral volume, and nothing verifies that at start-up. |

## Checked and found sound

- Authentication fails closed: no token, a bad signature, an expired token, `alg: none` and an
  unknown role are all refused; with no OIDC or development secret configured the app will not start.
- Cookie-authenticated writes require a same-origin `Origin` header; cookies are `HttpOnly` and
  `SameSite=Strict`.
- Every value on every page is escaped by the template engine (tested with a script tag in a record).
- Cypher is parameterised throughout. The only interpolated names are entity labels validated
  against a fixed table, and contract table names validated by pattern when the registry loads.
- A case that does not exist and a case the caller may not see return the same 404.
- A model cannot make a tag current, cannot lower a tag, cannot clear a trigger hit, cannot approve
  a mapping and cannot submit a SAR: each is enforced in code and covered by a test with a model
  scripted to try.
- Upload filenames are reduced to a base name and never used as a path; files are stored by hash.
