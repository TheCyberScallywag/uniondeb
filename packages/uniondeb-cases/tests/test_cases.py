"""Opening, assigning (each conflict rule), access without existence leaks, and identity reveal.

Verifies: UND-070
"""

import pytest

from uniondeb.cases import Actor, CaseError, ConflictError, NotFound, PermissionDenied, ReasonRequired
from uniondeb.core.prov import EntityRef
from uniondeb.store import Tarddiad

OFFICER = Actor(id="olwen", roles={"nominated_officer"}, person_id="CS-0100")
INVESTIGATOR = Actor(id="ifan", roles={"investigator"}, person_id="CS-0005")
OTHER_INVESTIGATOR = Actor(id="iona", roles={"investigator"}, person_id="CS-0006")
REVIEWER = Actor(id="rhian", roles={"reviewer"})
AUDITOR = Actor(id="aled", roles={"auditor"})
CASE_ADMIN = Actor(id="carys", roles={"case_admin"})


async def actions(audit, **filters) -> list[str]:
    return [e.action for e in reversed(await audit.events(**filters))]


async def node(store, case_id: str) -> dict:
    return (await store.run("MATCH (n:Case {id: $id}) RETURN n {.*} AS n", {"id": case_id}))[0]["n"]


# ───────────────────────────── opening ───────────────────────────────────────────────


async def test_open_from_alert_copies_the_pseudonym_and_window(graph, cases, audit):
    case = await cases.open_from_alert("alert-1", REVIEWER, "  four-eyes review agreed  ")
    stored = await node(graph, case.id)
    assert stored == {
        "id": case.id,
        "state": "open",
        "origin": "alert",
        "alert_id": "alert-1",
        "subject_pseudonym": "P-7f3a",
        "opened_by": "rhian",
        "opened_reason": "four-eyes review agreed",
        "opened_at": case.opened_at,
        "window_from": "2026-03-01T00:00:00+00:00",
        "window_to": "2026-03-31T23:59:59+00:00",
    }
    rows = await graph.run("MATCH (:Case {id: $id})-[:SUBJECT]->(p:Person) RETURN p.person_id AS p", {"id": case.id})
    assert rows == [{"p": "CS-0001"}]
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.actor, event.reason) == ("case.open", "rhian", "four-eyes review agreed")
    lineage = await Tarddiad(graph).lineage(EntityRef(label="Case", key=case.id))
    assert [s.activity["kind"] for s in lineage.steps] == ["Investigate"]
    assert lineage.steps[0].used == [EntityRef(label="Alert", key="alert-1")]


@pytest.mark.parametrize("reason", ["", "   ", None])
async def test_opening_needs_a_written_reason(graph, cases, reason):
    with pytest.raises(ReasonRequired):
        await cases.open_from_alert("alert-1", REVIEWER, reason)
    with pytest.raises(ReasonRequired):
        await cases.open_manual("CS-0001", OFFICER, reason)
    assert await graph.run("MATCH (c:Case) RETURN c.id AS id") == []


async def test_alert_escalation_is_for_reviewers_once_per_alert(graph, cases, audit):
    with pytest.raises(PermissionDenied):
        await cases.open_from_alert("alert-1", INVESTIGATOR, "curious")
    with pytest.raises(NotFound):
        await cases.open_from_alert("no-such-alert", REVIEWER, "reason")
    await cases.open_from_alert("alert-1", REVIEWER, "reason")
    with pytest.raises(CaseError, match="already"):
        await cases.open_from_alert("alert-1", REVIEWER, "again")
    assert await actions(audit) == ["case.access_denied", "case.open"]


async def test_manual_open_is_for_the_nominated_officer(graph, cases, audit):
    for actor in (INVESTIGATOR, REVIEWER, AUDITOR, CASE_ADMIN):
        with pytest.raises(PermissionDenied):
            await cases.open_manual("CS-0001", actor, "a hunch")
    assert await graph.run("MATCH (c:Case) RETURN c.id AS id") == []
    assert await actions(audit) == ["case.access_denied"] * 4

    case = await cases.open_manual("CS-0001", OFFICER, "tip-off from the trading floor", window_from="2026-03-01")
    assert (case.origin, case.alert_id, case.opened_by, case.window_from) == ("manual", None, "olwen", "2026-03-01")
    assert case.subject_pseudonym.startswith("SUBJ-") and "Sam" not in case.subject_pseudonym
    by_node_id = await cases.open_manual("owner:Sam Subject", OFFICER, "second matter")
    assert by_node_id.subject_pseudonym != case.subject_pseudonym
    with pytest.raises(NotFound):
        await cases.open_manual("CS-9999", OFFICER, "reason")


# ───────────────────────────── assignment ────────────────────────────────────────────


def investigator(person_id: str | None) -> Actor:
    return Actor(id=f"inv-{person_id}", roles={"investigator"}, person_id=person_id)


@pytest.mark.parametrize(
    ("person_id", "rules"),
    [
        ("CS-0001", ["is_subject", "same_team"]),
        ("CS-0002", ["same_team"]),
        ("CS-0009", ["line_manager_of_subject"]),
        ("CS-0003", ["managed_by_subject"]),
        ("CS-0004", ["shared_household_landline"]),
        (None, ["no_person_id"]),
        ("CS-7777", ["not_in_roster"]),
    ],
)
async def test_each_conflict_rule_blocks_assignment(graph, cases, audit, person_id, rules):
    case = await cases.open_from_alert("alert-1", REVIEWER, "reason")
    with pytest.raises(ConflictError) as exc:
        await cases.assign(case.id, investigator(person_id), OFFICER)
    assert exc.value.rules == rules
    assert await graph.run("MATCH (:Case)-[r:ASSIGNED]->() RETURN r.at AS at") == []
    blocked = (await audit.events(case_id=case.id))[0]
    assert (blocked.action, blocked.target, blocked.detail) == (
        "case.assign_blocked",
        f"inv-{person_id}",
        {"rules": rules},
    )


async def test_assign_and_unassign_keep_history(graph, cases, audit):
    case = await cases.open_from_alert("alert-1", REVIEWER, "reason")
    with pytest.raises(PermissionDenied):
        await cases.assign(case.id, INVESTIGATOR, INVESTIGATOR)
    with pytest.raises(PermissionDenied, match="investigator role"):
        await cases.assign(case.id, REVIEWER, OFFICER)

    # Ifan shares a mobile with the subject, which is not a household landline: no conflict.
    assigned = await cases.assign(case.id, INVESTIGATOR, OFFICER)
    assert assigned.investigators == ("ifan",)
    assert await cases.can_access(case.id, INVESTIGATOR)
    with pytest.raises(CaseError, match="already assigned"):
        await cases.assign(case.id, INVESTIGATOR, OFFICER)

    with pytest.raises(ReasonRequired):
        await cases.unassign(case.id, "ifan", OFFICER, " ")
    await cases.unassign(case.id, "ifan", OFFICER, "moved to another desk")
    assert not await cases.can_access(case.id, INVESTIGATOR)
    with pytest.raises(CaseError, match="not assigned"):
        await cases.unassign(case.id, "ifan", OFFICER, "again")
    await cases.assign(case.id, INVESTIGATOR, OFFICER)

    rows = await graph.run(
        "MATCH (:Case {id: $id})-[r:ASSIGNED]->(i:Investigator) RETURN i.id AS i, r.by AS by, r.ended_by AS ended_by, "
        "r.ended_at IS NULL AS current ORDER BY r.at",
        {"id": case.id},
    )
    assert rows == [
        {"i": "ifan", "by": "olwen", "ended_by": "olwen", "current": False},
        {"i": "ifan", "by": "olwen", "ended_by": None, "current": True},
    ]
    assert (await graph.run("MATCH (i:Investigator) RETURN count(i) AS n"))[0]["n"] == 1
    assert await actions(audit, case_id=case.id) == [
        "case.open",
        "case.access_denied",
        "case.assign",
        "case.unassign",
        "case.assign",
    ]


# ───────────────────────────── access ────────────────────────────────────────────────


async def test_unassigned_case_looks_exactly_like_a_missing_one(case, cases, audit):
    errors = []
    for case_id in (case.id, "0193-no-such-case"):
        with pytest.raises(NotFound) as exc:
            await cases.view(case_id, OTHER_INVESTIGATOR)
        errors.append(str(exc.value).replace(case_id, "<id>"))
        with pytest.raises(NotFound):
            await cases.reveal_identity(case_id, OTHER_INVESTIGATOR, "curious")
        with pytest.raises(NotFound):
            await cases.add_note(case_id, OTHER_INVESTIGATOR, "note")
        with pytest.raises(NotFound):
            await cases.notes(case_id, OTHER_INVESTIGATOR)
    assert errors[0] == errors[1]
    assert await cases.list_cases(OTHER_INVESTIGATOR) == []
    assert not await cases.can_access(case.id, OTHER_INVESTIGATOR)
    denied = [e for e in await audit.events(actor="iona") if e.action == "case.access_denied"]
    assert len(denied) == 8
    assert {e.detail["attempted"] for e in denied} == {"case.view", "case.reveal_identity", "case.note", "case.notes"}


async def test_who_can_see_a_case(case, cases):
    assert [c.id for c in await cases.list_cases(INVESTIGATOR)] == [case.id]
    assert [c.id for c in await cases.list_cases(OFFICER)] == [case.id]
    assert [c.id for c in await cases.list_cases(CASE_ADMIN)] == [case.id]
    for outsider in (AUDITOR, REVIEWER, OTHER_INVESTIGATOR):
        assert await cases.list_cases(outsider) == []
        assert not await cases.can_access(case.id, outsider)
        with pytest.raises(NotFound):
            await cases.require_access(case.id, outsider)
    assert (await cases.require_access(case.id, CASE_ADMIN)).id == case.id


async def test_nobody_sees_a_case_about_themselves(graph, cases):
    case = await cases.open_manual("CS-0100", Actor(id="owain", roles={"nominated_officer"}), "concern about Olwen")
    assert not await cases.can_access(case.id, OFFICER)
    assert await cases.list_cases(OFFICER) == []
    with pytest.raises(NotFound):
        await cases.view(case.id, OFFICER)


async def test_view_is_pseudonymous_and_reveal_is_logged_with_its_reason(case, cases, audit):
    seen = await cases.view(case.id, INVESTIGATOR)
    assert seen.subject_pseudonym == "P-7f3a"
    assert "Sam" not in seen.model_dump_json() and "CS-0001" not in seen.model_dump_json()

    for blank in ("", "  "):
        with pytest.raises(ReasonRequired):
            await cases.reveal_identity(case.id, INVESTIGATOR, blank)
    assert "case.reveal_identity" not in await actions(audit)

    identity = await cases.reveal_identity(case.id, INVESTIGATOR, "need to interview the subject")
    assert identity.model_dump() == {
        "case_id": case.id,
        "person_ref": "owner:Sam Subject",
        "name": "Sam Subject",
        "person_id": "CS-0001",
        "team": "desk-a",
        "role": "trader",
    }
    view, reveal = (await audit.events(case_id=case.id, actor="ifan"))[1::-1]
    assert view.action == "case.view"
    assert (reveal.action, reveal.reason) == ("case.reveal_identity", "need to interview the subject")

    with pytest.raises(PermissionDenied):  # a case admin holds cases, but does not unmask subjects
        await cases.reveal_identity(case.id, CASE_ADMIN, "legal hold paperwork")


# ───────────────────────────── notes and closing ─────────────────────────────────────


async def test_notes(case, cases, graph):
    with pytest.raises(ReasonRequired):
        await cases.add_note(case.id, INVESTIGATOR, "  ")
    note = await cases.add_note(case.id, INVESTIGATOR, "Spoke to the desk head.")
    rows = await graph.run("MATCH (:Case {id: $id})-[:HAS]->(n:CaseNote) RETURN n {.*} AS n", {"id": case.id})
    assert rows == [{"n": note.model_dump()}]
    assert await cases.notes(case.id, OFFICER) == [note]
    lineage = await Tarddiad(graph).lineage(EntityRef(label="CaseNote", key=note.id))
    assert lineage.steps[0].activity["trigger"] == "case.note by ifan"


async def test_close_needs_officer_outcome_and_retention_review(case, cases, graph, audit):
    with pytest.raises(PermissionDenied):
        await cases.close(case.id, INVESTIGATOR, "closed_no_action", "reviewed")
    with pytest.raises(CaseError, match="outcome"):
        await cases.close(case.id, OFFICER, "abandoned", "reviewed")
    with pytest.raises(ReasonRequired, match="retention review"):
        await cases.close(case.id, OFFICER, "closed_no_action", "")
    assert (await node(graph, case.id))["state"] == "open"

    closed = await cases.close(case.id, OFFICER, "closed_conduct_process", "keep 6 years: conduct file opened")
    stored = await node(graph, case.id)
    assert (stored["state"], stored["outcome"], stored["closed_by"]) == ("closed", "closed_conduct_process", "olwen")
    assert stored["retention_review"] == closed.retention_review == "keep 6 years: conduct file opened"
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.detail, event.reason) == (
        "case.close",
        {"outcome": "closed_conduct_process"},
        "keep 6 years: conduct file opened",
    )
    for attempt in (
        cases.close(case.id, OFFICER, "referred", "again"),
        cases.add_note(case.id, INVESTIGATOR, "late note"),
        cases.assign(case.id, OTHER_INVESTIGATOR, OFFICER),
    ):
        with pytest.raises(CaseError, match="closed"):
            await attempt
    assert (await cases.view(case.id, INVESTIGATOR)).state == "closed"


async def test_every_case_method_is_audited_and_state_changes_have_provenance(graph, cases, audit):
    async def audited(call):
        before = getattr(await audit.head(), "seq", 0)
        result = await call
        assert (await audit.head()).seq > before, call
        return result

    case = await audited(cases.open_from_alert("alert-1", REVIEWER, "reason"))
    manual = await audited(cases.open_manual("CS-0002", OFFICER, "reason"))
    await audited(cases.assign(case.id, INVESTIGATOR, OFFICER))
    await audited(cases.list_cases(INVESTIGATOR))
    await audited(cases.view(case.id, INVESTIGATOR))
    await audited(cases.reveal_identity(case.id, INVESTIGATOR, "reason"))
    await audited(cases.add_note(case.id, INVESTIGATOR, "note"))
    await audited(cases.notes(case.id, INVESTIGATOR))
    await audited(cases.unassign(case.id, "ifan", OFFICER, "reason"))
    await audited(cases.close(case.id, OFFICER, "referred", "reviewed"))
    with pytest.raises(NotFound):
        await audited(cases.require_access(manual.id, INVESTIGATOR))
    assert (await audit.verify()).ok

    rows = await graph.run(
        "MATCH (a:Activity {kind: 'Investigate'})-[:WAS_ASSOCIATED_WITH]->(sa:SoftwareAgent) "
        "RETURN a.trigger AS trigger, a.status AS status, sa.name AS agent ORDER BY a.started_at"
    )
    assert {(r["agent"], r["status"]) for r in rows} == {("ymchwiliad", "succeeded")}
    assert [r["trigger"].split(" by ")[0] for r in rows] == [
        "case.open",
        "case.open",
        "case.assign",
        "case.note",
        "case.unassign",
        "case.close",
    ]
