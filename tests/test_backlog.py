"""The backlog is well-formed, so issues, the project board and the matrix stay consistent.

Verifies: UND-010
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKLOG = yaml.safe_load((ROOT / "backlog" / "backlog.yaml").read_text())


def test_ids_are_unique_and_well_formed() -> None:
    ids = [s["id"] for s in BACKLOG["steps"]]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"UND-\d{3}", i) for i in ids)


def test_every_step_belongs_to_an_epic_and_has_acceptance_criteria() -> None:
    epics = {e["id"] for e in BACKLOG["epics"]}
    for s in BACKLOG["steps"]:
        assert s["epic"] in epics, s["id"]
        assert s.get("acceptance"), f"{s['id']} has no acceptance criteria"
        assert s.get("plan"), f"{s['id']} does not reference the plan"


def test_dependencies_exist_and_are_acyclic() -> None:
    steps = {s["id"]: s for s in BACKLOG["steps"]}
    for s in steps.values():
        for d in s.get("depends_on", []):
            assert d in steps, f"{s['id']} depends on unknown {d}"

    seen, stack = set(), set()

    def visit(n: str) -> None:
        assert n not in stack, f"dependency cycle through {n}"
        if n in seen:
            return
        stack.add(n)
        for d in steps[n].get("depends_on", []):
            visit(d)
        stack.discard(n)
        seen.add(n)

    for n in steps:
        visit(n)


def test_plan_document_is_present() -> None:
    assert (ROOT / "docs" / "uniondeb-plan.md").exists()
