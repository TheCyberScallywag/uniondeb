# Development

## Layout

```
packages/uniondeb-*/     the workspace packages (src/uniondeb/<name>/ and tests/)
ext_harness/             the delivery harness and its tests
tests/                   tests that span packages
deploy/                  compose stack, Dockerfile, database init and roles
docs/                    this documentation
scripts/spikes/          the Relate measurement behind ADR 0002
conftest.py              shared fixtures: synthetic runs and loaded embedded stores
```

Python 3.14, a uv workspace. All packages share the `uniondeb` namespace.

## Tests

```sh
uv sync --all-packages
uv run pytest                 # embedded store; no server needed
uv run ruff check
uv run ruff format --check
```

Tests that need Neo4j Enterprise are skipped unless `UNIONDEB_INTEGRATION=1` and the
`UNIONDEB_NEO4J_*` variables point at a server. In containers:

```sh
docker compose up -d --wait
docker compose run --rm dev               # pytest, including the Neo4j tests
docker compose run --rm dev ruff check
```

## The evaluation gate

```sh
uv run uniondeb evals gate
```

It generates a pinned synthetic run, puts it through ingest, tagging, scans, review, cases, SAR
and the audit chain, and fails on any regression. CI runs it on every pull request. The ethics
judge is scripted from the ground truth unless you pass `--model provider:model`.

## Continuous integration

`.github/workflows/ci.yml` runs lint, format check, the tests with a Neo4j service, and the
evaluation gate.

## Conventions

- Line length 120; ruff rules `E`, `F`, `I`, `UP`, `B`, `SIM`.
- Tests are `async` where the code is; `pytest-asyncio` runs in auto mode.
- Every graph statement is auto-committed. Store only scalars and lists as properties; serialise
  anything structured to JSON.
- A write to the graph happens inside a provenance activity.
- Record a significant decision as an ADR in `docs/adr/`, citing the evidence it rests on.
- Draft policy files (trigger list, code of ethics, retention, SAR glossary) are placeholders.
  Replacing them is a [governance](governance.md) decision.

## Documentation

The site is built with [Zensical](https://zensical.org) from `docs/` and `zensical.toml`.

```sh
uv run --group docs zensical serve -a localhost:8001     # preview (the API already uses port 8000)
uv run --group docs zensical build     # writes ./site (git-ignored)
```
