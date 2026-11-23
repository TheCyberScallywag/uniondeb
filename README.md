# Uniondeb

*Uniondeb* is Welsh for "integrity". It is an agentic conduct-assurance platform for CyberScallywags UK.

- **Source of truth:** AuraDB.
- **Agents:** each agent works on its own least-privilege Relate extract.
- **Records:** every record is tagged *clean*, *question* or *dirty*.
- **Decisions:** humans decide.
- **Investigations:** an audited investigation portal.
- **Provenance:** recorded for everything.

> **Status:** the software for phases 1 to 8 is built and passes its tests and eval gate **on synthetic data**. It is not ready for real data: the governance gate (EPIC-0) is not signed off, there has been no independent penetration test, and the items under "Not done yet" below are open. See `docs/uniondeb-plan.md` §2.

## Where things are

| | |
|---|---|
| Architecture and build plan | [`docs/uniondeb-plan.md`](docs/uniondeb-plan.md) |
| Backlog (source of truth for issues and the project board) | [`backlog/backlog.yaml`](backlog/backlog.yaml) |
| Traceability matrix | [`TRACEABILITY.md`](TRACEABILITY.md) |
| Decisions | [`docs/adr/`](docs/adr/) |
| Security review and open findings | [`docs/security-review.md`](docs/security-review.md) |
| Runbooks (incidents, restore, key rotation, audit chain, jobs) | [`docs/runbooks/`](docs/runbooks/) |
| Project board | <https://github.com/users/TheCyberScallywag/projects/6> |

## What is in the box

| Package | What it is |
|---|---|
| `uniondeb-core` | Record contracts, identifiers, the alias layer, provenance types, Logfire set-up |
| `uniondeb-synth` | Synthetic traffic with ground truth: normal, insider dealing, drug supply, adversarial fixtures |
| `uniondeb-store` | Neo4j/Aura and embedded Relate runners, migrations, roles, provenance, append-only tags, review queues, backup and restore |
| `uniondeb-extract` | Echdynnwr: extract contracts and the builder that cuts read-only, pseudonymised extracts for agents |
| `uniondeb-mail` | Cennad: SMTP journal receiver, M365 and Google Workspace parsers, raw mail store, Kafka publisher |
| `uniondeb-agents` | Porthor (ingest), Didolwr (tagging), Gwyliwr (security scans), Crynhoi (case packs), Holwr (case Q&A) |
| `uniondeb-cases` | Cases, conflict-checked assignment, flags vocabulary, hash-chained audit log, mock SAR submission |
| `uniondeb-worker` | Job queue, job handlers, scheduler, retention with legal hold |
| `uniondeb-api` | The gateway API, the review console (`/console`) and the Ymchwiliad portal (`/portal`) |
| `uniondeb-evals` | Arholwr: the offline eval gate and the online agreement and fairness report |

Agents that need a language model run only when one is named (`UNIONDEB_DIDOLWR_MODEL`, `_PORTHOR_`, `_CRYNHOI_`, `_HOLWR_`, in Pydantic AI's `provider:model` form). Without one, ingest, trigger tagging, scans, review and investigation all still work; only the ethics judgement, mapping proposals, pack narratives and Holwr are off.

### Not done yet

- **Governance (EPIC-0)** and the **pilot (UND-093)**: decisions for people, not code. The trigger list, code of ethics, retention schedule and SAR glossary codes in this repository are labelled drafts.
- **Penetration test (UND-091)**: a self-review is in `docs/security-review.md`, with open findings. No independent test has been done.
- **One database credential per role**: the API and worker each use one service account, so the Aura roles are not yet the hard boundary the plan describes (ADR 0003).
- **Judgement quality of a real model**: the eval gate scripts the ethics judge from ground truth, so it measures the pipeline around the model. `uniondeb evals gate --model <provider:model>` measures a real one; that has not been run.
- **Never run against AuraDB, a Kafka broker, or a real Microsoft 365 or Google Workspace tenant.** Tests use the embedded engine and Neo4j Enterprise; the mail parsers are tested against messages built to the documented formats.

## Traceability conventions

Every change belongs to a backlog step (`UND-NNN`), and the step ID appears in each of these places:

| Where | Form |
|---|---|
| Issue | `[UND-NNN] …`, created by `scripts/bootstrap_backlog.py`, as a sub-issue of its phase epic, on the project board |
| Branch | `und-NNN-<slug>` |
| Commits | the trailer `Refs: UND-NNN` (enforced by pre-commit and CI) |
| PR | `Closes #<issue>` |
| Code | module docstrings say `Implements: UND-NNN` |
| Tests | test modules say `Verifies: UND-NNN` |

`uv run scripts/traceability.py` rebuilds the matrix from these markers. CI fails if the matrix is stale or a commit has no step.

## Development

Everything runs in containers managed by Docker Compose. The stack is defined in [`deploy/compose.yml`](deploy/compose.yml) and the images in [`deploy/Dockerfile`](deploy/Dockerfile).

```bash
cp deploy/.env.example deploy/.env        # set a password; read the Neo4j evaluation licence
docker compose up -d --wait               # Neo4j Enterprise, the synthetic database, migrations
docker compose run --rm dev               # pytest, including the Neo4j integration tests
docker compose run --rm dev ruff check
docker compose run --rm uniondeb store status
docker compose down                       # add -v to delete the Neo4j volumes too
```

| Service | What it does |
|---|---|
| `neo4j` | Neo4j Enterprise, standing in for AuraDB. Browser on <http://localhost:7474>, Bolt on 7687. |
| `neo4j-init` | One-shot. Creates the `uniondeb-synthetic` database. |
| `migrate` | One-shot. Applies the migrations to both databases on every `up`. |
| `uniondeb` | The CLI, on demand. Paths are relative to `./data` (git-ignored), for example `docker compose run --rm uniondeb synth generate --out run-1`. |
| `dev` | The workspace with pytest and ruff, on demand. The checkout is mounted, so edits need no rebuild. |

Rebuild after changing dependencies: `docker compose build dev uniondeb`.

To run the application itself on the synthetic database, add the three secrets described in `deploy/.env.example` and start the `app` profile:

```bash
docker compose --profile app up -d                                     # api on http://localhost:8000, and the worker
docker compose run --rm uniondeb synth generate --out run-1            # a labelled synthetic run in ./data
docker compose run --rm uniondeb load run-1                            # or upload its files to POST /uploads
docker compose exec api uniondeb api token --sub ann --roles reviewer  # a development sign-in token for /login
```

The eval gate runs the whole pipeline on a pinned synthetic run and fails on any regression: `uv run uniondeb evals gate` (CI runs it on every pull request).

Git hooks still run on the host, and you can work there without Docker (integration tests are skipped unless `UNIONDEB_INTEGRATION=1`):

```bash
uv sync --all-packages
uv run pytest
uv run ruff check
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Licensed under the GNU GPL v3 or later.
