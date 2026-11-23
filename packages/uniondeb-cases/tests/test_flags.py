"""The flags vocabulary, restricted flags staying inside the case, and legal hold."""

from datetime import UTC, datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError

from uniondeb.cases import Actor, FlagError, FlagVocabulary, NotFound, ReasonRequired
from uniondeb.core.prov import EntityRef
from uniondeb.store import Tarddiad

OFFICER = Actor(id="olwen", roles={"nominated_officer"}, person_id="CS-0100")
INVESTIGATOR = Actor(id="ifan", roles={"investigator"}, person_id="CS-0005")
OTHER_INVESTIGATOR = Actor(id="iona", roles={"investigator"}, person_id="CS-0006")
CASE_ADMIN = Actor(id="carys", roles={"case_admin"})

SAM = EntityRef(label="Person", key="owner:Sam Subject")
MOBILE = EntityRef(label="Device", key="M1")

# ───────────────────────────── vocabulary ────────────────────────────────────────────


def test_packaged_vocabulary_matches_the_plan():
    vocabulary = FlagVocabulary.load()
    assert vocabulary.version == "1"
    by_kind = {kind: {f.label for f in vocabulary.flags if f.kind == kind} for kind in ("person", "device", "case")}
    assert by_kind == {
        "person": {"under_investigation", "insider_list_breach_suspected", "safeguarding_concern"}
        | {"false_positive_pattern"},
        "device": {"suspected_burner", "controlled_by", "shared_device"},
        "case": {"legal_hold", "sar_submitted", "referred_to_police", "closed_no_action"},
    }
    assert all(f.restricted for f in vocabulary.flags if f.kind == "person")
    assert all(f.hypothesis and not f.restricted for f in vocabulary.flags if f.kind == "device")
    assert {f.label for f in vocabulary.flags if f.param} == {"controlled_by"}


def test_vocabulary_refuses_what_it_does_not_define():
    vocabulary = FlagVocabulary.load()
    case = EntityRef(label="Case", key="c-1")
    with pytest.raises(FlagError, match="not in flags vocabulary v1"):
        vocabulary.check("looks_shifty", SAM)
    with pytest.raises(FlagError, match="person flag"):
        vocabulary.check("under_investigation", MOBILE)
    with pytest.raises(FlagError, match="device flag"):
        vocabulary.check("suspected_burner", SAM)
    with pytest.raises(FlagError, match="case flag"):
        vocabulary.check("legal_hold", SAM)
    for missing in (None, "", "  "):
        with pytest.raises(FlagError, match="needs a person parameter"):
            vocabulary.check("controlled_by", MOBILE, missing)
    with pytest.raises(FlagError, match="takes no parameter"):
        vocabulary.check("suspected_burner", MOBILE, "CS-0001")
    assert vocabulary.check("controlled_by", EntityRef(label="Identifier", key="447700900001"), "CS-0001").hypothesis
    assert vocabulary.check("legal_hold", case).kind == "case"


def test_vocabulary_file_is_validated(tmp_path):
    def load(*flags: dict, version: object = 2) -> FlagVocabulary:
        path = tmp_path / "flags.yaml"
        path.write_text(yaml.safe_dump({"version": version, "flags": list(flags)}))
        return FlagVocabulary.load(path)

    assert load({"label": "odd_hours", "kind": "device", "hypothesis": True}).version == "2"
    for bad in (
        [{"label": "x", "kind": "person"}],  # person flags must be restricted
        [{"label": "x", "kind": "device"}],  # device flags must be hypotheses
        [{"label": "x", "kind": "team", "restricted": True}],
        [{"label": "x", "kind": "case", "restricted": True}] * 2,
        [{"label": "x", "kind": "case", "restricted": True, "colour": "red"}],
    ):
        with pytest.raises(ValidationError):
            load(*bad)


# ───────────────────────────── setting and clearing ──────────────────────────────────


async def test_set_flag_writes_the_whole_story(case, flags, graph, audit):
    flag = await flags.set_flag(case.id, INVESTIGATOR, "under_investigation", target=SAM, reason="subject of the case")
    rows = await graph.run(
        "MATCH (c:Case)-[:HAS]->(f:Flag)-[:ON]->(t:Person) RETURN c.id AS c, f {.*} AS f, t.person_id AS t"
    )
    assert rows == [{"c": case.id, "f": flag.model_dump(exclude_none=True), "t": "CS-0001"}]
    assert flag.model_dump(include={"label", "kind", "vocabulary_version", "restricted", "hypothesis", "active"}) == {
        "label": "under_investigation",
        "kind": "person",
        "vocabulary_version": "1",
        "restricted": True,
        "hypothesis": False,
        "active": True,
    }
    assert (flag.set_by, flag.reason, flag.case_id) == ("ifan", "subject of the case", case.id)
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.target, event.reason) == ("flag.set", "Person:owner:Sam Subject", "subject of the case")
    assert event.detail["flag_id"] == flag.id
    lineage = await Tarddiad(graph).lineage(EntityRef(label="Flag", key=flag.id))
    assert lineage.steps[0].used == [EntityRef(label="Case", key=case.id)]


async def test_set_flag_refusals_leave_nothing_behind(case, flags, graph):
    with pytest.raises(ReasonRequired):
        await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason=" ")
    with pytest.raises(FlagError, match="not in flags vocabulary"):
        await flags.set_flag(case.id, INVESTIGATOR, "looks_shifty", target=SAM, reason="r")
    with pytest.raises(FlagError, match="person flag"):
        await flags.set_flag(case.id, INVESTIGATOR, "under_investigation", target=MOBILE, reason="r")
    with pytest.raises(FlagError, match="needs a person"):
        await flags.set_flag(case.id, INVESTIGATOR, "controlled_by", target=MOBILE, reason="r")
    with pytest.raises(FlagError, match="does not name one person"):
        await flags.set_flag(case.id, INVESTIGATOR, "controlled_by", target=MOBILE, reason="r", param="CS-9999")
    with pytest.raises(FlagError, match="does not exist"):
        await flags.set_flag(
            case.id, INVESTIGATOR, "suspected_burner", target=EntityRef(label="Device", key="nope"), reason="r"
        )
    with pytest.raises(FlagError, match="own case"):
        await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", target=EntityRef(label="Case", key="x"), reason="r")
    with pytest.raises(FlagError, match="timezone"):
        await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason="r", expires_at=datetime(2030, 1, 1))
    with pytest.raises(NotFound):
        await flags.set_flag(case.id, OTHER_INVESTIGATOR, "legal_hold", reason="r")
    assert await graph.run("MATCH (f:Flag) RETURN f.id AS id") == []


async def test_clearing_keeps_the_flag_as_history(case, flags, graph, audit):
    flag = await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason="litigation notice received")
    with pytest.raises(ReasonRequired):
        await flags.clear_flag(flag.id, INVESTIGATOR, "")
    for stranger_view in (flag.id, "no-such-flag"):
        with pytest.raises(NotFound, match="flag .* not found"):
            await flags.clear_flag(stranger_view, OTHER_INVESTIGATOR, "tidying")
    cleared = await flags.clear_flag(flag.id, CASE_ADMIN, "litigation settled")
    stored = (await graph.run("MATCH (f:Flag {id: $id}) RETURN f {.*} AS f", {"id": flag.id}))[0]["f"]
    assert stored == cleared.model_dump(exclude_none=True)
    assert (stored["active"], stored["cleared_by"], stored["cleared_reason"]) == (False, "carys", "litigation settled")
    assert (stored["reason"], stored["set_by"]) == ("litigation notice received", "ifan")
    with pytest.raises(FlagError, match="already cleared"):
        await flags.clear_flag(flag.id, CASE_ADMIN, "again")
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.actor, event.reason) == ("flag.clear", "carys", "litigation settled")
    again = await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason="new notice")
    assert [(f.id, f.active) for f in await flags.flags_for_case(case.id, OFFICER)] == [
        (flag.id, False),
        (again.id, True),
    ]


# ───────────────────────────── who sees what ─────────────────────────────────────────


async def test_restricted_flags_never_leave_the_case(case, flags, audit):
    await flags.set_flag(case.id, INVESTIGATOR, "under_investigation", target=SAM, reason="subject")
    await flags.set_flag(case.id, INVESTIGATOR, "safeguarding_concern", target=SAM, reason="welfare")
    await flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason="notice")
    await flags.set_flag(case.id, INVESTIGATOR, "suspected_burner", target=MOBILE, reason="prepaid, 3 weeks old")
    await flags.set_flag(case.id, INVESTIGATOR, "controlled_by", target=MOBILE, reason="seen using it", param="CS-0001")

    inside = await flags.flags_for_case(case.id, INVESTIGATOR)
    assert [f.label for f in inside] == [
        "under_investigation",
        "safeguarding_concern",
        "legal_hold",
        "suspected_burner",
        "controlled_by",
    ]
    assert (await audit.events(case_id=case.id))[0].action == "flag.list"
    with pytest.raises(NotFound):
        await flags.flags_for_case(case.id, OTHER_INVESTIGATOR)

    assert await flags.public_flags(SAM) == []
    assert await flags.public_flags(EntityRef(label="Case", key=case.id)) == []
    on_device = await flags.public_flags(MOBILE)
    assert [(f.label, f.param, f.hypothesis) for f in on_device] == [
        ("suspected_burner", None, True),
        ("controlled_by", "owner:Sam Subject", True),
    ]
    hypotheses = await flags.device_hypotheses()
    assert hypotheses == on_device
    for flag in hypotheses:
        dumped = flag.model_dump_json()
        assert flag.kind == "device" and flag.hypothesis and flag.target == MOBILE
        assert case.id not in dumped and "ifan" not in dumped and "prepaid" not in dumped
        assert not {"case_id", "set_by", "reason"} & set(flag.model_dump())


async def test_cleared_and_expired_flags_are_not_public(case, flags, graph):
    soon = datetime.now(UTC) + timedelta(hours=1)
    burner = await flags.set_flag(case.id, INVESTIGATOR, "suspected_burner", target=MOBILE, reason="r", expires_at=soon)
    shared = await flags.set_flag(case.id, INVESTIGATOR, "shared_device", target=MOBILE, reason="r")
    assert [f.label for f in await flags.device_hypotheses()] == ["suspected_burner", "shared_device"]
    await flags.clear_flag(shared.id, INVESTIGATOR, "ruled out")
    assert [f.label for f in await flags.public_flags(MOBILE)] == ["suspected_burner"]
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await graph.run("MATCH (f:Flag {id: $id}) SET f.expires_at = $past", {"id": burner.id, "past": past})
    assert await flags.public_flags(MOBILE) == []
    assert await flags.device_hypotheses() == []


# ───────────────────────────── legal hold ────────────────────────────────────────────


async def test_legal_hold_covers_the_subjects_records_in_the_window(case, flags, graph):
    assert await flags.records_on_hold() == set()
    await flags.set_flag(case.id, INVESTIGATOR, "sar_submitted", reason="not a hold")
    assert await flags.records_on_hold() == set()

    hold = await flags.set_flag(case.id, CASE_ADMIN, "legal_hold", reason="litigation notice")
    assert await flags.records_on_hold() == {"calls:1", "calls:2"}
    assert await flags.is_on_hold("calls:1")
    assert not await flags.is_on_hold("handoff:3")  # the subject's, but outside the case window
    assert not await flags.is_on_hold("calls:4")  # not the subject's device

    # journaled mail has no device: it reaches the subject through the sender's address alone
    await graph.run(
        "MATCH (p:Person {person_id: 'CS-0001'}) CREATE (a:EmailAddress {address: 'subject@cs.example'})-[:BELONGS_TO]->(p) "
        "CREATE (:Record {key: 'email:9', occurred_at: '2026-03-10T09:00:00+00:00'})-[:FROM]->(a) "
        "CREATE (:Record {key: 'email:10', occurred_at: '2026-06-01T09:00:00+00:00'})-[:FROM]->(a)"
    )
    assert await flags.records_on_hold() == {"calls:1", "calls:2", "email:9"}  # email:10 is outside the window

    await flags.clear_flag(hold.id, CASE_ADMIN, "settled")
    assert await flags.records_on_hold() == set()


async def test_legal_hold_without_a_window_holds_everything_and_expires(graph, cases, flags):
    case = await cases.open_manual("CS-0001", OFFICER, "tip-off")
    hold = await flags.set_flag(case.id, OFFICER, "legal_hold", reason="notice")
    assert await flags.records_on_hold() == {"calls:1", "calls:2", "handoff:3"}
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await graph.run("MATCH (f:Flag {id: $id}) SET f.expires_at = $past", {"id": hold.id, "past": past})
    assert await flags.records_on_hold() == set()
