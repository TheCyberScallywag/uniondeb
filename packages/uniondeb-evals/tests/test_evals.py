"""The offline gate meets every target on a synthetic run and fails on a regression; online evals
turn reviewer decisions into agreement and fairness numbers.
"""

import pytest

from uniondeb.agents.didolwr import TriggerList
from uniondeb.core.cli import main
from uniondeb.evals.gate import GateReport, Suite, run_gate, suite, trigger_suite
from uniondeb.evals.online import Rate, _disparity, online_report
from uniondeb.store import Tarddiad
from uniondeb.store.review import Reviewer, decide, list_items


async def test_gate_meets_every_target_on_the_scenario_run(scenario_run):
    report = await run_gate(scenario_run[0])
    assert report.passed, [s.model_dump() for s in report.suites if not s.passed]
    names = " | ".join(s.name for s in report.suites)
    for area in ("porthor", "cennad", "didolwr", "gwyliwr", "sar mock", "audit"):
        assert area in names
    assert report.judge == "scripted" and all(s.cases > 0 for s in report.suites)
    assert next(s for s in report.suites if "dirty recall on fixtures" in s.name).target == 0.95


async def test_a_regression_fails_the_gate(scenario_run):
    weaker = TriggerList.load().model_copy(
        update={"phrases": tuple(p for p in TriggerList.load().phrases if p != "cash")}
    )
    result = await trigger_suite(scenario_run[0], weaker)
    assert not result.passed and result.score < 1.0 and result.detail["wrong"]
    report = GateReport(run="x", judge="scripted", suites=[suite("ok", 1.0, 1.0, 1), result])
    assert not report.passed
    assert suite("recall", 0.94, 0.95, 100).passed is False and suite("recall", 0.95, 0.95, 100).passed is True
    assert isinstance(result, Suite)


async def test_online_report_measures_agreement_and_concern_by_team_and_language(tagged_store):
    t = Tarddiad(tagged_store)
    items = await list_items(tagged_store, kind="dirty_tag", limit=6)
    ann, bob = Reviewer(id="ann"), Reviewer(id="bob")
    for item in items[:4]:
        await decide(tagged_store, t, item["id"], ann, "confirm_dirty", "yes")
        await decide(tagged_store, t, item["id"], bob, "confirm_dirty", "agree")
    for item in items[4:]:
        await decide(tagged_store, t, item["id"], ann, "downgrade_question", "benign")
        await decide(tagged_store, t, item["id"], bob, "downgrade_question", "benign")
    report = await online_report(tagged_store)
    assert report.dirty_reviews == {"confirm_dirty": 4, "downgrade_question": 2} and report.dirty_agreement == 0.667
    assert sum(r.records for r in report.concern_by_language.values()) == sum(
        r.records for r in report.concern_by_team.values()
    )
    assert (
        report.concern_by_language["cy"].records > 0
        and report.concern_by_language["en"].records > report.concern_by_language["cy"].records
    )
    assert {"Partnerships", "Events", "no employee sender"} <= set(report.concern_by_team)
    assert report.agreement_by_language and report.agreement_by_medium and report.pack_ratings["count"] == 0
    assert all(isinstance(w, str) for w in report.warnings)


def test_disparity_needs_enough_records_and_a_real_gap():
    even = {
        "cy": Rate(records=200, question=6, dirty=0, concern_per_1000=30.0),
        "en": Rate(records=5000, question=140, dirty=0, concern_per_1000=28.0),
    }
    assert _disparity(even, "language") == []
    skewed = even | {"cy": Rate(records=200, question=20, dirty=0, concern_per_1000=100.0)}
    assert len(_disparity(skewed, "language")) == 1 and "'cy'" in _disparity(skewed, "language")[0]
    tiny = even | {"cy": Rate(records=10, question=5, dirty=0, concern_per_1000=500.0)}
    assert _disparity(tiny, "language") == []


def test_cli_lists_the_commands(capsys):
    with pytest.raises(SystemExit):
        main(["evals", "--help"])
    assert "gate" in capsys.readouterr().out
