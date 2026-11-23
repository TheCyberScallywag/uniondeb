"""Gwyliwr: activity-normalised metrics, graph signals, both scenario subjects ranked, no content read."""

import json
from datetime import UTC, datetime

import pytest

from uniondeb.agents.gwyliwr import SensitiveDate, concern_metrics, robust_z, run_scan
from uniondeb.agents.gwyliwr.scan import stable_pseudonym
from uniondeb.agents.gwyliwr.signals import email_exfiltration, graph_signals
from uniondeb.extract import ContractRegistry, Echdynnwr
from uniondeb.store import Tarddiad
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.review import list_items

KEY = b"k" * 32
END = datetime(2026, 9, 16, tzinfo=UTC)


def test_robust_z_ignores_one_extreme_value():
    population = [10.0, 12.0, 11.0, 9.0, 10.0, 500.0]
    assert robust_z(500.0, population) > 50 and abs(robust_z(10.0, population)) < 1
    assert robust_z(5.0, [1.0, 2.0]) is None and robust_z(0.0, [0.0, 0.0, 0.0]) == 0.0
    assert robust_z(4.0, [0.0, 0.0, 0.0, 0.0, 4.0]) > 0


def test_rates_are_normalised_by_activity_so_busy_people_are_not_flagged():
    people = [{"pid": p, "team": "T"} for p in "abcdef"]
    daily = [
        {"pid": "a", "day": "2026-09-10", "records": 2000, "question": 20, "dirty": 0},  # busy, ordinary rate
        {"pid": "b", "day": "2026-09-10", "records": 100, "question": 1, "dirty": 0},
        {"pid": "c", "day": "2026-09-10", "records": 100, "question": 1, "dirty": 6},  # quiet, high rate
        {"pid": "d", "day": "2026-09-10", "records": 5, "question": 5, "dirty": 0},  # too little to score
        {"pid": "e", "day": "2026-09-10", "records": 300, "question": 4, "dirty": 0},
        {"pid": "f", "day": "2026-09-10", "records": 200, "question": 1, "dirty": 0},
        {"pid": "c", "day": "2026-09-01", "records": 50, "question": 0, "dirty": 0},
        {"pid": "c", "day": "2026-09-02", "records": 50, "question": 1, "dirty": 0},
        {"pid": "c", "day": "2026-09-03", "records": 50, "question": 0, "dirty": 0},
    ]
    m = {x.subject: x for x in concern_metrics(people, daily, window_from="2026-09-08T00:00:00Z")}
    assert m["a"].question_per_1000 == m["b"].question_per_1000 == 10.0 and m["a"].records == 2000
    assert m["c"].dirty_per_1000 == 60.0 and m["c"].concern_per_1000 == 190.0
    assert m["c"].z_team > 3 and m["c"].z_own > 3 and abs(m["a"].z_team) < 1
    assert not m["d"].scored and m["d"].z_team is None


def test_email_exfiltration_needs_an_internal_first_sighting():
    mails = [
        {
            "record": "e1",
            "at": "2026-09-01T10:00:00Z",
            "sender_pid": "p",
            "direction": "internal",
            "personal_recipients": 0,
            "self_forward": False,
            "attachments": ["h1"],
        },
        {
            "record": "e2",
            "at": "2026-09-02T10:00:00Z",
            "sender_pid": "p",
            "direction": "to_personal",
            "personal_recipients": 1,
            "self_forward": True,
            "attachments": ["h1"],
        },
        {
            "record": "e3",
            "at": "2026-09-02T11:00:00Z",
            "sender_pid": "q",
            "direction": "to_personal",
            "personal_recipients": 1,
            "self_forward": False,
            "attachments": ["h2"],
        },
    ]
    [s] = email_exfiltration(mails)
    assert (s.subject, s.record_keys, s.hypothesis) == ("p", ["e2"], {"type": "EXFILTRATED", "from": "p", "to": "h1"})
    assert graph_signals([], [], [], []) == []


@pytest.fixture
def echdynnwr(tagged_store, tmp_path):
    cipher = FieldCipher(FieldCipher.generate_key())
    return Echdynnwr(tagged_store, Tarddiad(tagged_store), ContractRegistry.load(), root=tmp_path / "x", cipher=cipher)


def _scenarios(run):
    scenarios = {s["name"]: s for s in json.loads((run / "ground_truth" / "scenarios.json").read_text())}
    calendar = [
        SensitiveDate(date=t["at"][:10], label="client announcement") for t in scenarios["insider_dealing"]["timeline"]
    ]
    return scenarios, calendar


async def test_both_scenario_subjects_rank_top_three_and_hidden_links_surface(tagged_run, tagged_store, echdynnwr):
    scenarios, calendar = _scenarios(tagged_run[0])
    await run_scan(
        tagged_store,
        Tarddiad(tagged_store),
        echdynnwr,
        pseudonym_key=KEY,
        window_to=END,
        window_days=16,
        baseline_days=0,
        calendar=calendar,
    )
    alerts = await tagged_store.run(
        "MATCH (a:Alert)-[:ABOUT]->(p:Person) RETURN p.person_id AS pid, a {.*} AS a ORDER BY a.score DESC"
    )
    top3 = [a["pid"] for a in alerts[:3]]
    insider = scenarios["insider_dealing"]["actors"]["insider"]["person_id"]
    dealer = scenarios["drug_trafficking"]["actors"]["employee"]["person_id"]
    assert insider in top3 and dealer in top3

    suggested = await tagged_store.run(
        "MATCH (a:Alert)-[s:SUGGESTS]->(n) RETURN a.id AS alert, s.type AS type, s.side AS side, "
        "coalesce(n.person_id, n.id, n.sha256) AS ref"
    )
    ends: dict[tuple[str, str], dict[str, set[str]]] = {}
    for s in suggested:
        ends.setdefault((s["alert"], s["type"]), {"from": set(), "to": set()})[s["side"]].add(s["ref"])
    found = {(kind, f, t) for (_, kind), e in ends.items() for f in e["from"] for t in e["to"]}
    for name, scenario in scenarios.items():
        links = {(k["type"], k["from"], k["to"]) for k in scenario["hidden_links"] if k["type"] != "EXFILTRATED"}
        if any(k["type"] == "EXFILTRATED" for k in scenario["hidden_links"]) and any(
            f[0] == "EXFILTRATED" and f[1] == insider for f in found
        ):
            links.add(("EXFILTRATED", insider, "*"))
            found.add(("EXFILTRATED", insider, "*"))
        if links:
            assert len({k[0] for k in links & found}) >= 2, (
                name,
                sorted(found),
            )  # two kinds of hidden link, as hypotheses
    patterns = {p for a in alerts for p in a["a"]["patterns"]}
    assert {"unregistered_device_contact", "contact_spike", "channel_switch", "email_exfiltration"} <= patterns


async def test_alerts_are_pseudonymous_content_free_and_go_to_four_eyes_review(tagged_run, tagged_store, echdynnwr):
    _, calendar = _scenarios(tagged_run[0])
    t = Tarddiad(tagged_store)
    result = await run_scan(
        tagged_store, t, echdynnwr, pseudonym_key=KEY, window_to=END, window_days=16, baseline_days=0, calendar=calendar
    )
    alerts = await tagged_store.run(
        "MATCH (a:Alert)-[:ABOUT]->(p:Person) RETURN a {.*} AS a, p.name AS name, p.id AS pid"
    )
    assert alerts and len(alerts) == len(result.alerts)
    texts = [
        b
        for r in await tagged_store.run(
            "MATCH (r:Record) WHERE r.body IS NOT NULL RETURN collect(DISTINCT r.body) AS b"
        )
        for b in r["b"]
    ]
    for row in alerts:
        blob = json.dumps(row["a"])
        assert row["name"] not in blob and row["a"]["subject_pseudonym"] == stable_pseudonym(KEY, row["pid"])
        assert not any(text in blob for text in texts if len(text) > 12)
    items = await list_items(tagged_store, kind="alert", limit=500)
    assert len(items) == len(alerts) and all(i["four_eyes"] for i in items)
    assert not any(row["name"] in json.dumps(i) for i in items for row in alerts)
    # the scan is provenance, and a second identical scan does not raise the same alerts again
    [scan] = await tagged_store.run(
        "MATCH (a:Activity {kind: 'Scan'})-[:USED]->(x:Extract) RETURN x.contract_id AS c, a.agent AS agent"
    )
    assert scan == {"c": "gwyliwr.scan.v1", "agent": "gwyliwr"}
    again = await run_scan(
        tagged_store, t, echdynnwr, pseudonym_key=KEY, window_to=END, window_days=16, baseline_days=0, calendar=calendar
    )
    assert again.alerts == []
    [stored] = await tagged_store.run("MATCH (s:Scan {id: $id}) RETURN s.metrics_json AS m", {"id": result.scan_id})
    assert all(m["subject"].startswith("S-") for m in json.loads(stored["m"]))


async def test_quiet_window_raises_nothing(small_store, tmp_path):
    x = Echdynnwr(
        small_store,
        Tarddiad(small_store),
        ContractRegistry.load(),
        root=tmp_path / "x",
        cipher=FieldCipher(FieldCipher.generate_key()),
    )
    result = await run_scan(
        small_store,
        Tarddiad(small_store),
        x,
        pseudonym_key=KEY,
        kind="daily",
        window_to=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.alerts == [] and result.findings.metrics == []
