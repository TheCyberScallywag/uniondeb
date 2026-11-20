# Uniondeb

*Uniondeb* is Welsh for "integrity". It is an agentic conduct-assurance platform for CyberScallywags UK.

- **Source of truth:** AuraDB.
- **Agents:** each agent works on its own least-privilege Relate extract.
- **Records:** every record is tagged *clean*, *question* or *dirty*.
- **Decisions:** humans decide.
- **Investigations:** an audited investigation portal.
- **Provenance:** recorded for everything.

> **Status:** under construction. No real data may be processed until the governance gate (EPIC-0) is signed off. See `docs/uniondeb-plan.md` §2.

## Where things are

| | |
|---|---|
| Architecture and build plan | [`docs/uniondeb-plan.md`](docs/uniondeb-plan.md) |
| Backlog (source of truth for issues and the project board) | [`backlog/backlog.yaml`](backlog/backlog.yaml) |
| Traceability matrix | [`TRACEABILITY.md`](TRACEABILITY.md) |
| Decisions | [`docs/adr/`](docs/adr/) |
| Project board | <https://github.com/users/TheCyberScallywag/projects/6> |

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

```bash
uv sync --all-packages
uv run pytest
uv run ruff check
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Licensed under the GNU GPL v3 or later.
