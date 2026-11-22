"""Shared fixtures: an embedded store, the four services, and a small roster and device graph.

Verifies: UND-070
"""

import logfire
import pytest

from uniondeb.cases import Actor, AuditLog, CaseService, FlagService, SarService
from uniondeb.store import RelateRunner, Tarddiad, migrate

OFFICER = Actor(id="olwen", roles={"nominated_officer"}, person_id="CS-0100")
INVESTIGATOR = Actor(id="ifan", roles={"investigator"}, person_id="CS-0005")
REVIEWER = Actor(id="rhian", roles={"reviewer"})

PEOPLE = [
    {"id": "owner:Sam Subject", "name": "Sam Subject", "person_id": "CS-0001", "team": "desk-a", "role": "trader"}
    | {"manager_id": "CS-0009", "kind": "employee"},
    {"id": "owner:Tess Teammate", "name": "Tess Teammate", "person_id": "CS-0002", "team": "desk-a"},
    {"id": "owner:Rob Report", "name": "Rob Report", "person_id": "CS-0003", "team": "desk-b", "manager_id": "CS-0001"},
    {"id": "owner:Hana Housemate", "name": "Hana Housemate", "person_id": "CS-0004", "team": "desk-c"},
    {"id": "owner:Ifan Clean", "name": "Ifan Clean", "person_id": "CS-0005", "team": "desk-d"},
    {"id": "owner:Iona Clean", "name": "Iona Clean", "person_id": "CS-0006", "team": "desk-e"},
    {"id": "owner:Mari Manager", "name": "Mari Manager", "person_id": "CS-0009", "team": "management"},
    {"id": "owner:Olwen Officer", "name": "Olwen Officer", "person_id": "CS-0100", "team": "compliance"},
    {"id": "owner:Ext Contact", "name": "Ext Contact", "kind": "external"},
]
DEVICES = [("M1", "mobile"), ("L1", "landline"), ("M2", "mobile"), ("X1", "mobile")]
# Ifan shares a *mobile* with Sam: only a household landline is a conflict.
USES = [("CS-0001", "M1"), ("CS-0001", "L1"), ("CS-0004", "L1"), ("CS-0001", "M2"), ("CS-0005", "M2")]
RECORDS = [
    ("calls:1", "2026-03-02T09:00:00Z", "FROM", "M1"),
    ("calls:2", "2026-03-05T18:30:00+00:00", "TO", "L1"),
    ("handoff:3", "2026-06-01T00:00:00Z", "ON", "M1"),  # outside the alert window
    ("calls:4", "2026-03-03T10:00:00Z", "FROM", "X1"),  # someone else's device
]


@pytest.fixture
async def store():
    logfire.configure(send_to_logfire=False, console=False)
    runner = RelateRunner.open(":memory:")
    await migrate(runner)
    yield runner
    await runner.close()


@pytest.fixture
async def graph(store):
    await store.run("UNWIND $people AS p CREATE (n:Person) SET n = p", {"people": PEOPLE})
    await store.run("UNWIND $devices AS d CREATE (:Device {id: d[0], kind: d[1]})", {"devices": DEVICES})
    await store.run(
        "UNWIND $uses AS u MATCH (p:Person {person_id: u[0]}) MATCH (d:Device {id: u[1]}) CREATE (p)-[:USES]->(d)",
        {"uses": USES},
    )
    await store.run("MATCH (p:Person {id: 'owner:Ext Contact'}) MATCH (d:Device {id: 'X1'}) CREATE (p)-[:USES]->(d)")
    for key, at, rel, device in RECORDS:
        await store.run(
            f"MATCH (d:Device {{id: $device}}) CREATE (r:Record {{key: $key, occurred_at: $at}}) CREATE (r)-[:{rel}]->(d)",
            {"key": key, "at": at, "device": device},
        )
    await store.run(
        "MATCH (p:Person {person_id: 'CS-0001'}) CREATE (a:Alert {id: 'alert-1', pattern: 'volume_spike', "
        "subject_pseudonym: 'P-7f3a', window_from: '2026-03-01T00:00:00+00:00', "
        "window_to: '2026-03-31T23:59:59+00:00', state: 'escalated'}) CREATE (a)-[:ABOUT]->(p)"
    )
    return store


@pytest.fixture
def audit(store):
    return AuditLog(store)


@pytest.fixture
def cases(store, audit):
    return CaseService(store, audit, Tarddiad(store))


@pytest.fixture
def flags(store, audit, cases):
    return FlagService(store, audit, cases)


@pytest.fixture
def sars(store, audit, cases, flags):
    return SarService(store, audit, cases, flags)


@pytest.fixture
async def case(graph, cases):
    """An open case on Sam, escalated from the alert, with Ifan assigned."""
    opened = await cases.open_from_alert("alert-1", REVIEWER, "four-eyes review: sustained spike before results")
    return await cases.assign(opened.id, INVESTIGATOR, OFFICER)
