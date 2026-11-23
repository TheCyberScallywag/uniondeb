# Configuration

Everything is set through environment variables. The compose stack reads `deploy/.env`; tools run
on the host read the shell environment (`set -a; . deploy/.env; set +a`).

## Database

| Variable | Default | |
|---|---|---|
| `UNIONDEB_NEO4J_URI` | `neo4j://localhost:7687` | `neo4j+s://…` for AuraDB, or `relate:<path>` for an embedded file |
| `UNIONDEB_NEO4J_USER`, `UNIONDEB_NEO4J_PASSWORD` | `neo4j`, none | |
| `UNIONDEB_NEO4J_DATABASE` | `neo4j` | The database for real data |
| `UNIONDEB_SYNTHETIC` | | `1` makes the API and worker use `uniondeb-synthetic`. The compose stack sets it |

## Secrets the API and worker need

| Variable | |
|---|---|
| `UNIONDEB_FIELD_KEY` | Encrypts record content and case packs. Generate with `uniondeb store keygen`. Losing it loses the content |
| `UNIONDEB_PSEUDONYM_KEY` | At least 32 characters. Keys the stable pseudonyms in alerts |
| `UNIONDEB_AUTH_SECRET` | Development sign-in only: at least 32 characters, signs tokens from `uniondeb api token` |

## Sign-in (production)

| Variable | |
|---|---|
| `UNIONDEB_OIDC_JWKS_URL` | The identity provider's signing keys. Set this in place of `UNIONDEB_AUTH_SECRET` |
| `UNIONDEB_OIDC_ISSUER`, `UNIONDEB_OIDC_AUDIENCE` | Checked on every token |
| `UNIONDEB_OIDC_ROLES_CLAIM` | The claim holding roles; default `roles` |

With neither a JWKS URL nor a secret the API refuses to start.

## Models

An agent uses a model only when one is named, in Pydantic AI's `provider:model` form, for example
`ollama:llama3.3`.

| Variable | Turns on |
|---|---|
| `UNIONDEB_DIDOLWR_MODEL` | The ethics judgement during tagging |
| `UNIONDEB_PORTHOR_MODEL` | Column-mapping proposals for unknown dialects |
| `UNIONDEB_CRYNHOI_MODEL` | The narrative in case packs |
| `UNIONDEB_HOLWR_MODEL` | Case question-answering and SAR narrative drafts |

## Files and policy

| Variable | Default | |
|---|---|---|
| `UNIONDEB_DATA_DIR` | `/var/lib/uniondeb` | Uploaded files are kept under `uploads/` here |
| `UNIONDEB_UPLOAD_DIR` | `$UNIONDEB_DATA_DIR/uploads` | |
| `UNIONDEB_EXTRACT_ROOT` | `/run/uniondeb/extracts` | Agent extracts. Unencrypted, so keep it on memory-backed storage |
| `UNIONDEB_MAX_UPLOAD_BYTES` | 512 MiB | |
| `UNIONDEB_TRIGGER_LIST` | the packaged draft | Path to the trigger list |
| `UNIONDEB_RETENTION` | the packaged draft | Path to the retention schedule |
| `UNIONDEB_CALENDAR` | | Path to a YAML list of sensitive dates for scans |
| `UNIONDEB_AUDIT_ANCHOR`, `UNIONDEB_AUDIT_ANCHOR_KEY` | | Where the nightly audit check writes the signed head of the chain, and the key that signs it |

The compose stack passes the secrets, the model variables and the OIDC variables through from
`deploy/.env`. The others in this table are not passed to the containers; add them to
`deploy/compose.yml` if you need them there.

## Observability

| Variable | |
|---|---|
| `LOGFIRE_TOKEN` | Spans leave the process only when this is set |
| `UNIONDEB_ENV` | Environment name on spans; default `dev` |

## Mail receiver

`uniondeb cennad serve` reads `CENNAD_*`:

| Variable | Default | |
|---|---|---|
| `CENNAD_HOST`, `CENNAD_PORT` | `127.0.0.1`, `8025` | |
| `CENNAD_ALLOW` | empty | CIDRs of the journaling mail servers. Empty rejects everyone |
| `CENNAD_TLS_CERT`, `CENNAD_TLS_KEY` | | Without a certificate, mail is refused unless `CENNAD_ALLOW_PLAINTEXT` is set |
| `CENNAD_STORE` | `var/cennad` | Raw messages, by SHA-256 |
| `CENNAD_KAFKA` | | Bootstrap servers. Unset, documents are written to stdout as JSON lines |
| `CENNAD_MAX_BYTES` | 50 MiB | |
| `CENNAD_SYNTHETIC` | | Mark published documents as synthetic |

## Delivery harness

`HARNESS_*`: see [the harness settings](../run-harness.md#settings).

## Tests

| Variable | |
|---|---|
| `UNIONDEB_INTEGRATION` | `1` runs the tests that need a Neo4j Enterprise server |
