# Uniondeb

*Uniondeb* is Welsh for "integrity". It is a conduct-assurance platform for CyberScallywags UK: it
ingests records of calls, messages, hand-offs and email from the organisation's own accounts and
equipment, tags each one *clean*, *question* or *dirty* against the code of ethics, scans for
patterns, and gives people an audited place to review, investigate and report. People decide every
outcome.

> **Status:** built and tested **on synthetic data only**. It is not ready for real data: the
> governance gate is not signed off, there has been no independent penetration test, and some
> functions are incomplete. See [Governance and status](docs/governance.md).

## Documentation

The documentation is in [`docs/`](docs/) and builds into a site with [Zensical](https://zensical.org):

```bash
uv run --group docs zensical serve -a localhost:8001
```

| | |
|---|---|
| What it does | [`docs/index.md`](docs/index.md) |
| How it works: components, data model, agents | [`docs/how-it-works.md`](docs/how-it-works.md) |
| Getting started | [`docs/getting-started.md`](docs/getting-started.md) |
| Test data | [`docs/test-data.md`](docs/test-data.md) |
| Sending data directly | [`docs/run-direct.md`](docs/run-direct.md) |
| Sending data through the delivery harness | [`docs/run-harness.md`](docs/run-harness.md) |
| Review, scans and cases | [`docs/review-and-cases.md`](docs/review-and-cases.md) |
| Reference: command line, HTTP API, configuration | [`docs/reference/`](docs/reference/) |
| Governance and status | [`docs/governance.md`](docs/governance.md) |
| Runbooks and security review | [`docs/runbooks/`](docs/runbooks/), [`docs/security-review.md`](docs/security-review.md) |
| Original design and decision records | [`docs/design.md`](docs/design.md), [`docs/adr/`](docs/adr/) |

## Quick start

```bash
uv sync --all-packages
cp deploy/.env.example deploy/.env                   # set a password, accept the licence, add the three secrets
docker compose --profile app up -d --build --wait    # Neo4j, migrations, API on :8000, worker

set -a; . deploy/.env; set +a
uv run python -m ext_harness make-data               # synthetic test data in ./data
uv run python -m ext_harness serve &                 # delivery harness on :8090
uv run python -m ext_harness send data/run-001/data --watch
```

[Getting started](docs/getting-started.md) explains each step.

## What is in the box

| Package | What it is |
|---|---|
| `uniondeb-core` | Record contracts, identifiers, the alias layer, provenance types, logging set-up |
| `uniondeb-synth` | Synthetic traffic with ground truth: normal, insider dealing, drug supply, adversarial fixtures |
| `uniondeb-store` | Neo4j/Aura and embedded Relate runners, migrations, roles, provenance, append-only tags, review queues, backup and restore |
| `uniondeb-extract` | Echdynnwr: extract contracts and the builder that cuts read-only, pseudonymised extracts for agents |
| `uniondeb-mail` | Cennad: SMTP journal receiver, M365 and Google Workspace parsers, raw mail store, publisher |
| `uniondeb-agents` | Porthor (ingest), Didolwr (tagging), Gwyliwr (security scans), Crynhoi (case packs), Holwr (case Q&A) |
| `uniondeb-cases` | Cases, conflict-checked assignment, flags, hash-chained audit log, mock SAR submission |
| `uniondeb-worker` | Job queue, job handlers, scheduler, retention with legal hold |
| `uniondeb-api` | The HTTP API, the review console (`/console`) and the investigation portal (`/portal`) |
| `uniondeb-evals` | Arholwr: the offline evaluation gate and the online agreement report |
| `ext_harness/` | A delivery harness: accepts files and streams and feeds them to the API, with status over SSE |

## Development

```bash
uv run pytest
uv run ruff check
uv run uniondeb evals gate
pre-commit install
```

See [Development](docs/development.md).

Licensed under the GNU GPL v3 or later.
