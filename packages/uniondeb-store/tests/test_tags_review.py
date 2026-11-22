"""Tags are append-only, a model cannot weaken a finding, and four-eyes holds for dirty.

Verifies: UND-042, UND-050, UND-022
"""

import logfire
import pytest

from uniondeb.core.prov import EntityRef
from uniondeb.store import Tarddiad, migrate
from uniondeb.store.crypto import CipherError, FieldCipher
from uniondeb.store.review import (
    ItemNotFound,
    Reviewer,
    ReviewError,
    create_item,
    decide,
    get_item,
    list_items,
    queue_stats,
)
from uniondeb.store.tags import TagRuleError, TagWrite, append_tag, current_tag, history

A, B, C = Reviewer(id="ann"), Reviewer(id="bob"), Reviewer(id="cai")


@pytest.fixture
async def store(relate_runner):
    logfire.configure(send_to_logfire=False, console=False)
    await migrate(relate_runner)
    await relate_runner.run("CREATE (:Record {key: 'messages:1'}), (:Record {key: 'messages:2'})")
    return relate_runner


def tag(label, method, **kw):
    return TagWrite(record_key=kw.pop("key", "messages:1"), label=label, method=method, created_by="didolwr", **kw)


async def test_new_tag_supersedes_and_nothing_is_overwritten(store):
    first = await append_tag(store, tag("clean", "trigger", trigger_list_version="v1"))
    second = await append_tag(store, tag("question", "trigger", trigger_hits=["cash"], trigger_list_version="v2"))
    hist = await history(store, "messages:1")
    assert [(t["id"], t["label"], t["current"]) for t in hist] == [(first, "clean", False), (second, "question", True)]
    assert hist[0]["superseded_by"] == second and hist[0]["trigger_list_version"] == "v1"
    [row] = await store.run("MATCH (:Tag {id: $a})-[:SUPERSEDES]->(o:Tag) RETURN o.id AS id", {"a": second})
    assert row["id"] == first


async def test_model_cannot_downgrade_or_clean_a_trigger_hit(store):
    await append_tag(store, tag("question", "trigger", trigger_hits=["cash"]))
    with pytest.raises(TagRuleError, match="clean"):
        await append_tag(store, tag("clean", "llm"))
    with pytest.raises(TagRuleError, match="trigger hits"):
        await append_tag(store, tag("clean", "llm", trigger_hits=["cash"], key="messages:2"))
    assert (await current_tag(store, "messages:1"))["label"] == "question"


async def test_agents_can_only_propose_dirty(store):
    with pytest.raises(TagRuleError, match="propose"):
        await append_tag(store, tag("dirty", "llm"))
    await append_tag(store, tag("question", "trigger", trigger_hits=["cash"]))
    proposal = await append_tag(store, tag("dirty", "llm", clauses=["CE1"]), proposed=True)
    assert (await current_tag(store, "messages:1"))["label"] == "question"
    [p] = [t for t in await history(store, "messages:1") if t["id"] == proposal]
    assert p["proposed"] is True and p["current"] is False
    with pytest.raises(TagRuleError, match="no record"):
        await append_tag(store, tag("clean", "trigger", key="messages:404"))


async def _dirty_item(store):
    await append_tag(store, tag("question", "trigger", trigger_hits=["cash"], trigger_list_version="v1"))
    proposal = await append_tag(store, tag("dirty", "llm", clauses=["CE1"], evidence_span="cash only"), proposed=True)
    return await create_item(
        store,
        "dirty_tag",
        created_by="didolwr",
        about=EntityRef(label="Record", key="messages:1"),
        summary="dirty proposal",
        payload={"record_key": "messages:1", "tag_id": proposal, "clauses": ["CE1"], "evidence_span": "cash only"},
    )


async def test_dirty_needs_two_distinct_reviewers(store):
    t, item = Tarddiad(store), await _dirty_item(store)
    with pytest.raises(ReviewError, match="rationale"):
        await decide(store, t, item, A, "confirm_dirty", "  ")
    with pytest.raises(ReviewError, match="not a decision"):
        await decide(store, t, item, A, "approve", "x")
    one = await decide(store, t, item, A, "confirm_dirty", "describes supply")
    assert one.state == "awaiting_second" and one.decision is None
    assert (await current_tag(store, "messages:1"))["label"] == "question"
    with pytest.raises(ReviewError, match="second reviewer"):
        await decide(store, t, item, A, "confirm_dirty", "again")
    two = await decide(store, t, item, B, "confirm_dirty", "agree")
    assert two.state == "decided" and two.decided_by == ["ann", "bob"]
    cur = await current_tag(store, "messages:1")
    assert (cur["label"], cur["method"], cur["created_by"], cur["clauses"]) == ("dirty", "human", "ann+bob", ["CE1"])
    assert cur["trigger_hits"] == ["cash"] and len(await history(store, "messages:1")) == 3
    with pytest.raises(ReviewError, match="already decided"):
        await decide(store, t, item, C, "confirm_dirty", "late")
    lineage = await t.lineage(EntityRef(label="Tag", key=cur["id"]))
    assert lineage.steps[0].activity["kind"] == "Review" and lineage.steps[0].activity["trigger"] == "reviewer:bob"


async def test_disagreement_is_broken_by_a_third_reviewer(store):
    t, item = Tarddiad(store), await _dirty_item(store)
    await decide(store, t, item, A, "confirm_dirty", "yes")
    assert (await decide(store, t, item, B, "needs_info", "who is B?")).state == "awaiting_second"
    assert (await decide(store, t, item, B, "downgrade_question", "benign")).state == "disputed"
    out = await decide(store, t, item, C, "downgrade_question", "benign, agreed")
    assert out.decision == "downgrade_question" and out.decided_by == ["bob", "cai"]
    assert (await current_tag(store, "messages:1"))["label"] == "question"
    assert len((await get_item(store, item))["decisions"]) == 4


async def test_question_clear_and_escalate(store):
    t = Tarddiad(store)
    await append_tag(store, tag("question", "trigger", trigger_hits=["cash"]))
    await append_tag(store, tag("question", "trigger", trigger_hits=["tip"], key="messages:2"))
    one = await create_item(store, "question_tag", created_by="didolwr", payload={"record_key": "messages:1"})
    two = await create_item(store, "question_tag", created_by="didolwr", payload={"record_key": "messages:2"})
    assert (await decide(store, t, one, A, "clear", "food van")).state == "decided"
    assert (await current_tag(store, "messages:1"))["label"] == "clean"
    await decide(store, t, two, A, "escalate", "looks like a tip-off")
    [dirty] = await list_items(store, kind="dirty_tag")
    assert dirty["four_eyes"] and dirty["payload"]["escalated_from"] == two
    assert (await current_tag(store, "messages:2"))["label"] == "question"
    assert await queue_stats(store) == [{"kind": "dirty_tag", "open": 1, "oldest": dirty["created_at"]}]


async def test_restricted_decisions_and_missing_items(store):
    t = Tarddiad(store)
    await store.run("CREATE (:Alert {id: 'al-1'})")
    item = await create_item(store, "alert", created_by="gwyliwr", about=EntityRef(label="Alert", key="al-1"))
    with pytest.raises(ReviewError, match="compliance_lead"):
        await decide(store, t, item, A, "escalate_legal", "serious")
    lead = Reviewer(id="lead", roles=frozenset({"compliance_lead"}))
    assert (await decide(store, t, item, lead, "escalate_legal", "serious")).state == "awaiting_second"
    with pytest.raises(ItemNotFound):
        await get_item(store, "nope")
    with pytest.raises(ReviewError, match="missing entity"):
        await create_item(store, "alert", created_by="x", about=EntityRef(label="Alert", key="nope"))


async def test_a_failed_effect_records_no_decision(store):
    t = Tarddiad(store)
    item = await create_item(store, "mapping_proposal", created_by="porthor", payload={})

    async def boom(*_):
        raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError):
        await decide(store, t, item, A, "approve", "ok", effects={"mapping_proposal": boom})
    assert (await get_item(store, item))["state"] == "open"


def test_field_cipher_round_trip_and_fails_closed():
    key = FieldCipher.generate_key()
    c = FieldCipher(key)
    token = c.encrypt_json({"body": "cash only"})
    assert "cash" not in token and c.decrypt_json(token) == {"body": "cash only"}
    with pytest.raises(CipherError):
        FieldCipher(FieldCipher.generate_key()).decrypt(token)
    with pytest.raises(CipherError, match="UNIONDEB_FIELD_KEY"):
        FieldCipher.from_env({})
    assert FieldCipher.from_env({"UNIONDEB_FIELD_KEY": key}).decrypt_json(token) == {"body": "cash only"}
