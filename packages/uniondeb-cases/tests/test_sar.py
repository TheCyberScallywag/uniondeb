"""SAR payload rules, the mock portal, and second-person approval.

Verifies: UND-074
"""

import copy
import hashlib
import json

import pytest

from uniondeb.cases import (
    Actor,
    MockSarPortal,
    NotFound,
    PermissionDenied,
    SarError,
    SarGlossary,
    SarPayload,
    SarService,
    SarValidationError,
    validate_payload,
)
from uniondeb.core.prov import EntityRef
from uniondeb.store import Tarddiad

OFFICER = Actor(id="olwen", roles={"nominated_officer"}, person_id="CS-0100")
INVESTIGATOR = Actor(id="ifan", roles={"investigator"}, person_id="CS-0005")
OTHER_INVESTIGATOR = Actor(id="iona", roles={"investigator"}, person_id="CS-0006")
BOTH_ROLES = Actor(id="bethan", roles={"investigator", "nominated_officer"}, person_id="CS-0006")
AUDITOR = Actor(id="aled", roles={"auditor"})
CASE_ADMIN = Actor(id="carys", roles={"case_admin"})

REASON = (
    "XXD9XX XXV2XX Who: the subject, a trader. What: repeated calls to an unregistered mobile. "
    "Where: the firm's London desk. When: 2-5 March 2026. Why: timing matches cash deposits. How: recorded lines."
)


def fields(**changes) -> dict:
    base = {
        "route": "nca_sar",
        "subject": {
            "name": "Sam Subject",
            "date_of_birth": "UNKNOWN",
            "address": "UNKNOWN",
            "identifiers": ["447700900001"],
            "occupation": "Trader",
        },
        "glossary_codes": ["XXD9XX", "XXV2XX"],
        "reason_for_suspicion": REASON,
        "record_keys": ["calls:1", "calls:2"],
        "provenance_refs": ["Activity:0193"],
    }
    subject = {k.removeprefix("subject_"): v for k, v in changes.items() if k.startswith("subject_")}
    base["subject"] |= subject
    return base | {k: v for k, v in changes.items() if not k.startswith("subject_")}


# ───────────────────────────── payload ───────────────────────────────────────────────


def test_valid_payload_and_what_is_sent():
    payload = validate_payload(fields(subject_date_of_birth="1988-02-29"))
    document = payload.submission_document()
    assert set(document) == {"route", "subject", "glossary_codes", "reason_for_suspicion"}
    assert "calls:1" not in json.dumps(document) and "Activity:0193" not in json.dumps(document)
    assert payload.record_keys == ("calls:1", "calls:2")
    assert json.loads(payload.frozen_json())["record_keys"] == ["calls:1", "calls:2"]
    with pytest.raises(Exception, match="frozen"):
        payload.reason_for_suspicion = "edited after validation"
    for route in ("police_referral", "fca_report"):
        assert validate_payload(fields(route=route)).route == route


@pytest.mark.parametrize("field", ["name", "date_of_birth", "address", "occupation"])
@pytest.mark.parametrize("value", ["", "   ", "N/A", "n/a", "-", "?", "none", "None", "unknown", "Unknown", "---"])
def test_unknowns_must_be_the_literal_unknown(field, value):
    with pytest.raises(SarValidationError) as exc:
        validate_payload(fields(**{f"subject_{field}": value}))
    assert len(exc.value.errors) == 1
    assert exc.value.errors[0].startswith(f"subject.{field}: ") and "literal UNKNOWN" in exc.value.errors[0]
    assert validate_payload(fields(**{f"subject_{field}": "UNKNOWN"}))


def test_identifiers_and_dates():
    assert validate_payload(fields(subject_identifiers=["UNKNOWN"])).subject.identifiers == ("UNKNOWN",)
    for bad in ([], ["447700900001", ""], ["n/a"], ["?"]):
        with pytest.raises(SarValidationError, match="subject.identifiers"):
            validate_payload(fields(subject_identifiers=bad))
    with pytest.raises(SarValidationError, match="YYYY-MM-DD"):
        validate_payload(fields(subject_date_of_birth="29/02/1988"))
    with pytest.raises(SarValidationError, match="subject.name"):
        validate_payload({k: v for k, v in fields().items() if k != "subject"} | {"subject": {"address": "UNKNOWN"}})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"glossary_codes": ["XXD9"]}, "must match XX…XX"),
        ({"glossary_codes": ["xxd9xx"]}, "must match XX…XX"),
        ({"glossary_codes": ["D9"]}, "must match XX…XX"),
        ({"glossary_codes": ["XXZZ99XX"]}, "not in the configured list"),
        ({"glossary_codes": []}, "glossary_codes"),
        ({"reason_for_suspicion": "The subject made calls. XXD9XX XXV2XX"}, "must start with its glossary codes"),
        ({"reason_for_suspicion": "XXD9XX The subject made calls."}, "must start with its glossary codes"),
        ({"reason_for_suspicion": "XXV2XX XXD9XX wrong order"}, "must start with its glossary codes"),
        ({"reason_for_suspicion": "XXD9XX XXV2XX " + "x" * 7987}, "8,001 characters; the limit is 8,000"),
        ({"reason_for_suspicion": "   "}, "reason for suspicion is required"),
        ({"route": "carrier_pigeon"}, "route"),
        ({"urgency": "high"}, "urgency"),
    ],
)
def test_payload_validation_matrix(changes, message):
    with pytest.raises(SarValidationError, match=message):
        validate_payload(fields(**changes))


def test_reason_limit_and_code_punctuation():
    exact = "XXD9XX XXV2XX " + "x" * 7986
    assert len(validate_payload(fields(reason_for_suspicion=exact)).reason_for_suspicion) == 8000
    assert validate_payload(fields(reason_for_suspicion="XXD9XX, XXV2XX: the subject made calls."))
    assert validate_payload(fields(glossary_codes=["XXD9XX"], reason_for_suspicion="XXD9XX the subject made calls."))


def test_glossary_is_configuration(tmp_path):
    packaged = SarGlossary.load()
    assert "PLACEHOLDER" in packaged.source and packaged.version.startswith("placeholder")
    assert {"XXD9XX", "XXV2XX"} <= set(packaged.codes)

    path = tmp_path / "glossary.yaml"
    path.write_text("source: test booklet\nversion: '2026-11'\ncodes:\n  XXNEW1XX: A new code\n")
    current = SarGlossary.load(path)
    new = fields(glossary_codes=["XXNEW1XX"], reason_for_suspicion="XXNEW1XX the subject made calls.")
    assert validate_payload(new, current).glossary_codes == ("XXNEW1XX",)
    with pytest.raises(SarValidationError, match=r"not in the configured list \(2026-11\)"):
        validate_payload(fields(), current)
    with pytest.raises(SarValidationError, match="not in the configured list"):
        validate_payload(new)
    path.write_text("source: s\nversion: v\ncodes:\n  D9: bad\n")
    with pytest.raises(ValueError, match="XX…XX"):
        SarGlossary.load(path)


async def test_mock_portal_validates_and_never_touches_the_network():
    receipt = await MockSarPortal().submit(validate_payload(fields()))
    assert receipt.reference.startswith("MOCK-SAR-") and receipt.mock and receipt.acknowledged_at.tzinfo
    unchecked = SarPayload.model_construct(**fields(glossary_codes=["XXZZ99XX"]))
    with pytest.raises(SarValidationError):
        await MockSarPortal().submit(unchecked)


# ───────────────────────────── drafting ──────────────────────────────────────────────


async def test_draft_update_and_who_can_read_it(case, sars, graph, audit):
    draft = await sars.draft(case.id, INVESTIGATOR, {"reason_for_suspicion": "to be written"})
    stored = (await graph.run("MATCH (:Case)-[:HAS]->(d:SarDraft) RETURN d {.*} AS d"))[0]["d"]
    assert stored == draft.model_dump(exclude_none=True)
    assert (draft.state, draft.drafted_by, draft.payload) == (
        "draft",
        "ifan",
        {"reason_for_suspicion": "to be written"},
    )

    updated = await sars.update_draft(draft.id, INVESTIGATOR, fields())
    assert updated.payload == fields() and updated.updated_at
    assert (await sars.get_draft(draft.id, INVESTIGATOR)) == updated
    assert [d.id for d in await sars.drafts(case.id, OFFICER)] == [draft.id]

    with pytest.raises(PermissionDenied, match="only the drafter"):
        await sars.update_draft(draft.id, OFFICER, fields())
    for draft_id in (draft.id, "no-such-draft"):
        with pytest.raises(NotFound, match="SAR draft .* not found"):
            await sars.get_draft(draft_id, OTHER_INVESTIGATOR)
    with pytest.raises(NotFound):
        await sars.draft(case.id, OTHER_INVESTIGATOR, fields())
    with pytest.raises(NotFound):
        await sars.drafts(case.id, AUDITOR)
    assert [e.action for e in await audit.events(case_id=case.id, actor="ifan")] == [
        "sar.view_draft",
        "sar.update_draft",
        "sar.draft",
    ]


# ───────────────────────────── approval ──────────────────────────────────────────────


async def nothing_submitted(graph, case_id: str) -> bool:
    rows = await graph.run(
        "MATCH (c:Case {id: $id}) OPTIONAL MATCH (s:SarSubmission) OPTIONAL MATCH (c)-[:HAS]->(d:SarDraft) "
        "OPTIONAL MATCH (c)-[:HAS]->(f:Flag) RETURN count(s) AS s, collect(DISTINCT d.state) AS states, count(f) AS f",
        {"id": case_id},
    )
    return rows == [{"s": 0, "states": ["draft"], "f": 0}]


async def test_self_approval_is_refused(graph, cases, sars, audit):
    case = await cases.open_manual("CS-0001", OFFICER, "tip-off")
    draft = await sars.draft(case.id, OFFICER, fields())
    with pytest.raises(PermissionDenied, match="second person"):
        await sars.approve_and_submit(draft.id, OFFICER)
    assert await nothing_submitted(graph, case.id)
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.actor, event.detail) == ("sar.approval_denied", "olwen", {"why": "self_approval"})


async def test_holding_both_roles_does_not_make_one_person_two(case, cases, sars, graph):
    await cases.assign(case.id, BOTH_ROLES, OFFICER)
    draft = await sars.draft(case.id, BOTH_ROLES, fields())
    with pytest.raises(PermissionDenied, match="second person"):
        await sars.approve_and_submit(draft.id, BOTH_ROLES)
    assert await nothing_submitted(graph, case.id)


@pytest.mark.parametrize("approver", [INVESTIGATOR, OTHER_INVESTIGATOR, CASE_ADMIN, AUDITOR])
async def test_only_a_nominated_officer_approves(case, sars, graph, audit, approver):
    draft = await sars.draft(case.id, INVESTIGATOR, fields())
    with pytest.raises(PermissionDenied, match="nominated officer"):
        await sars.approve_and_submit(draft.id, approver)
    assert await nothing_submitted(graph, case.id)
    event = (await audit.events(actor=approver.id))[0]
    assert (event.action, event.detail) == ("sar.approval_denied", {"why": "role"})


async def test_invalid_draft_is_not_submitted(case, sars, graph, audit):
    draft = await sars.draft(case.id, INVESTIGATOR, fields(subject_address="n/a", glossary_codes=["XXZZ99XX"]))
    with pytest.raises(SarValidationError) as exc:
        await sars.approve_and_submit(draft.id, OFFICER)
    assert len(exc.value.errors) == 2
    assert await nothing_submitted(graph, case.id)
    event = (await audit.events(case_id=case.id))[0]
    assert (event.action, event.detail["errors"]) == ("sar.validation_failed", exc.value.errors)

    await sars.update_draft(draft.id, INVESTIGATOR, fields())
    assert (await sars.approve_and_submit(draft.id, OFFICER)).reference.startswith("MOCK-SAR-")


async def test_happy_path_stores_the_frozen_copy(case, sars, flags, graph, audit):
    draft = await sars.draft(case.id, INVESTIGATOR, fields())
    before = (await audit.head()).seq
    submission = await sars.approve_and_submit(draft.id, OFFICER)

    rows = await graph.run(
        "MATCH (c:Case)-[:REPORTED]->(s:SarSubmission)-[:FROM_DRAFT]->(d:SarDraft) "
        "RETURN c.id AS c, s {.*} AS s, d.state AS draft, d.approved_by AS approved_by"
    )
    assert rows == [{"c": case.id, "s": submission.model_dump(), "draft": "submitted", "approved_by": "olwen"}]
    assert submission.reference.startswith("MOCK-SAR-") and submission.mock is True
    assert (submission.drafted_by, submission.approved_by, submission.route) == ("ifan", "olwen", "nca_sar")
    assert submission.payload_sha256 == hashlib.sha256(submission.payload_json.encode()).hexdigest()
    assert submission.payload == validate_payload(fields()).model_dump(mode="json")
    assert submission.payload["record_keys"] == ["calls:1", "calls:2"]  # the internal links are kept, not sent

    case_flags = await flags.flags_for_case(case.id, OFFICER)
    assert [(f.label, f.kind, f.set_by, f.active) for f in case_flags] == [("sar_submitted", "case", "olwen", True)]
    assert submission.reference in case_flags[0].reason
    assert await flags.public_flags(EntityRef(label="Case", key=case.id)) == []

    events = [e for e in reversed(await audit.events(case_id=case.id)) if e.seq > before]
    assert [(e.action, e.actor) for e in events[:3]] == [
        ("sar.approve", "olwen"),
        ("sar.submit", "olwen"),
        ("flag.set", "olwen"),
    ]
    assert events[0].detail == {"payload_sha256": submission.payload_sha256, "drafted_by": "ifan", "route": "nca_sar"}
    assert events[1].detail["reference"] == submission.reference
    assert (await audit.verify()).ok

    lineage = await Tarddiad(graph).lineage(EntityRef(label="SarSubmission", key=submission.id))
    step = lineage.steps[0]
    assert (step.activity["kind"], step.activity["agent"], step.activity["trigger"]) == (
        "Sar",
        "ymchwiliad",
        "sar.submit by olwen",
    )
    assert set(step.used) == {EntityRef(label="Case", key=case.id), EntityRef(label="SarDraft", key=draft.id)}

    with pytest.raises(SarError, match="already submitted"):
        await sars.approve_and_submit(draft.id, OFFICER)
    with pytest.raises(SarError, match="no longer be edited"):
        await sars.update_draft(draft.id, INVESTIGATOR, fields())
    assert (await graph.run("MATCH (s:SarSubmission) RETURN count(s) AS n"))[0]["n"] == 1


async def test_stored_copy_does_not_follow_later_edits_to_the_input(case, sars):
    original = fields()
    draft = await sars.draft(case.id, INVESTIGATOR, original)
    sent = copy.deepcopy(original)
    original["subject"]["name"] = "Somebody Else"
    submission = await sars.approve_and_submit(draft.id, OFFICER)
    assert submission.payload["subject"] == sent["subject"]


async def test_portal_failure_leaves_the_draft_approvable(case, store, audit, cases, flags, graph):
    class Down:
        async def submit(self, payload):
            raise ConnectionError("portal unavailable")

    sars = SarService(store, audit, cases, flags, client=Down())
    draft = await sars.draft(case.id, INVESTIGATOR, fields())
    with pytest.raises(ConnectionError):
        await sars.approve_and_submit(draft.id, OFFICER)
    assert await nothing_submitted(graph, case.id)
    assert (await audit.events(case_id=case.id))[0].action == "sar.submit_failed"
    retry = SarService(store, audit, cases, flags)
    assert (await retry.approve_and_submit(draft.id, OFFICER)).mock


async def test_only_officers_and_auditors_read_submissions(case, sars, audit):
    draft = await sars.draft(case.id, INVESTIGATOR, fields())
    submission = await sars.approve_and_submit(draft.id, OFFICER)
    assert await sars.submissions(case.id, OFFICER) == [submission]
    assert await sars.submissions(case.id, AUDITOR) == [submission]
    assert await sars.submissions("no-such-case", AUDITOR) == []
    for actor in (INVESTIGATOR, OTHER_INVESTIGATOR, CASE_ADMIN):
        with pytest.raises(PermissionDenied):
            await sars.submissions(case.id, actor)
    assert [e.action for e in await audit.events(case_id=case.id, limit=5)] == [
        "case.access_denied",
        "case.access_denied",
        "case.access_denied",
        "sar.submissions",
        "sar.submissions",
    ]
    assert (await sars.get_draft(draft.id, INVESTIGATOR)).state == "submitted"  # the drafter still sees the draft


async def test_every_flag_and_sar_method_is_audited(case, flags, sars, audit):
    async def audited(call):
        before = (await audit.head()).seq
        result = await call
        assert (await audit.head()).seq > before, call
        return result

    flag = await audited(flags.set_flag(case.id, INVESTIGATOR, "legal_hold", reason="notice"))
    await audited(flags.flags_for_case(case.id, INVESTIGATOR))
    await audited(flags.clear_flag(flag.id, INVESTIGATOR, "settled"))
    draft = await audited(sars.draft(case.id, INVESTIGATOR, {}))
    await audited(sars.update_draft(draft.id, INVESTIGATOR, fields()))
    await audited(sars.get_draft(draft.id, INVESTIGATOR))
    await audited(sars.drafts(case.id, INVESTIGATOR))
    await audited(sars.approve_and_submit(draft.id, OFFICER))
    await audited(sars.submissions(case.id, OFFICER))
    assert (await audit.verify()).ok
