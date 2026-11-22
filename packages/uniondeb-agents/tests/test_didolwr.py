"""Didolwr: deterministic triggers, a judge that can only propose, and routing that never downgrades.

Verifies: UND-040, UND-041, UND-042, UND-043
"""

import csv
import unicodedata

import pytest

from uniondeb.agents.didolwr import DidolwrConfig, EthicsJudge, EthicsJudgement, TriggerList, route, tag_file
from uniondeb.agents.didolwr.run import context_line, sampled
from uniondeb.agents.models import ModelNotConfigured
from uniondeb.core.prov import EntityRef
from uniondeb.evals.scripted import gullible_judge, labels_by_text, output_model
from uniondeb.extract import ContractRegistry, Echdynnwr
from uniondeb.store import Tarddiad
from uniondeb.store.review import Reviewer, decide, list_items
from uniondeb.store.tags import current_tag, history
from uniondeb.synth import content as C

TRIGGERS = TriggerList.load()
CTX = {
    "place": "Y Caban",
    "time": "7pm",
    "day": "Tuesday",
    "first": "Nia",
    "company": "Tonnau Ynni plc",
    "ticker": "TNNU",
    "project": "Prosiect Morlo",
    "n": "12",
}


def _labels(run):
    with (run / "ground_truth" / "labels.csv").open(newline="") as fh:
        return {f"{x['dataset']}:{x['record_key']}": x for x in csv.DictReader(fh)}


def _templates():
    groups = [
        C.NORMAL_MESSAGES,
        C.NORMAL_TRANSCRIPTS,
        C.NORMAL_NOTES,
        C.BENIGN_TRIGGER_MESSAGES,
        C.BENIGN_TRIGGER_TRANSCRIPTS,
    ]
    groups += [tuple(C.INSIDER.values()), tuple(C.DRUGS.values()), C.NORMAL_EMAILS, C.BENIGN_TRIGGER_EMAILS]
    groups += [tuple(C.INSIDER_EMAILS.values()), tuple(C.DRUG_EMAILS.values())]
    return [t for g in groups for t in g]


@pytest.mark.parametrize("t", _templates(), ids=lambda t: t.text[:30])
def test_trigger_scan_is_exact_on_every_fixture_template(t):
    text = t.text.format(**CTX)
    assert TRIGGERS.scan(text) == C.trigger_hits(text)  # 100% precision and recall against the list
    assert TRIGGERS.scan(text.upper()) == TRIGGERS.scan(text)


def test_whole_word_case_unicode_and_whitespace():
    assert TRIGGERS.scan("Multiple tipsters, cashew nuts and a repackaged insiders' guide") == []
    assert TRIGGERS.scan("TIP: bring   CASH") == ["cash", "tip"]
    assert TRIGGERS.scan("Don’t tell anyone") == ["don't tell"]
    assert TRIGGERS.scan(unicodedata.normalize("NFD", "Paid â dweud wrth neb")) == ["paid â dweud"]
    assert TRIGGERS.scan("keep\nit\tquiet") == ["keep it quiet"]
    assert TRIGGERS.scan(None) == [] and TRIGGERS.scan("") == []


def test_disguised_triggers_are_found_as_their_canonical_phrase():
    for key, expected in {
        "obfuscated_dirty_leet": ["cash"],
        "obfuscated_dirty_spaced": ["delete this"],
        "obfuscated_benign": ["cash"],
        "welsh_trigger_dirty": ["arian parod", "dileu hwn"],
        "welsh_trigger_benign": ["rhwng ni"],
    }.items():
        assert TRIGGERS.scan(C.ADVERSARIAL[key].text) == expected, key
    assert TRIGGERS.scan(C.ADVERSARIAL["welsh_coded"].text) == []  # coded language: only the judge can catch it
    assert TRIGGERS.scan("Invoice INV-4051 at 7am, v2 of the 10 slides") == []


def test_list_version_and_hash_identify_the_list():
    other = TRIGGERS.model_copy(update={"phrases": (*TRIGGERS.phrases, "nudge")})
    assert TRIGGERS.version == "v1-draft" and other.sha256 != TRIGGERS.sha256
    assert (
        TriggerList(version="x", owner="o", phrases=("Cash", "cash ")).sha256
        == TriggerList(version="y", owner="o", phrases=("cash",)).sha256
    )


def test_routing_has_no_path_from_a_model_to_clean():
    none = EthicsJudgement(verdict="none", confidence=1.0)
    concern = EthicsJudgement(verdict="concern", clauses=["CE1"], evidence_span="x", confidence=0.8)
    assert route(["cash"], none).label == "question" and not route(["cash"], none).propose_dirty
    assert route(["cash"], concern) == route(["cash"], concern, sampled_for_review=True)
    assert (route([], concern).label, route([], concern).propose_dirty) == ("clean", True)
    assert route([], None).label == "clean" and route(["cash"], None, sampled_for_review=True).review_question
    assert sampled("k", 1.0, "s") and not sampled("k", 0.0, "s") and sampled("k", 0.5, "s") == sampled("k", 0.5, "s")


def test_context_line_carries_roles_not_identities():
    parties = {
        "P-1": {"party_kind": "employee", "device_kind": "mobile"},
        "P-2": {"party_kind": "external", "device_kind": "landline"},
    }
    links = [{"party": "P-2", "role": "TO"}, {"party": "P-1", "role": "FROM"}]
    row = {"channel": "SMS", "occurred_at": "2026-09-01T21:40:09Z", "dataset": "messages"}
    assert context_line(row, links, parties) == "A (employee, mobile) → B (external, landline), SMS, 21:40"


def _echdynnwr(store, tmp_path):
    return Echdynnwr(store, Tarddiad(store), ContractRegistry.load(), root=tmp_path / "extracts")


async def _files(store):
    return [r["sha"] for r in await store.run("MATCH (f:SourceFile) RETURN f.sha256 AS sha ORDER BY sha")]


async def test_deterministic_pass_alone_tags_every_record(small_run, small_store, tmp_path):
    run, _ = small_run
    labels, t, x = _labels(run), Tarddiad(small_store), _echdynnwr(small_store, tmp_path)
    total = 0
    for sha in await _files(small_store):
        r = await tag_file(small_store, t, x, sha)
        total += r.records
        assert r.records == r.clean + r.question and r.proposed_dirty == 0 and r.judged == 0
    tags = await small_store.run("MATCH (t:Tag {current: true})-[:ON]->(r:Record) RETURN r.key AS key, t {.*} AS t")
    [n] = await small_store.run("MATCH (r:Record) RETURN count(r) AS n")
    assert len(tags) == n["n"] == total  # every record has exactly one current tag
    for row in tags:
        want, tag = labels[row["key"]], row["t"]
        assert tag["method"] == "trigger" and tag["trigger_list_version"] == "v1-draft"
        if want["expected_tag"] == "question":
            assert tag["label"] == "question" and tag["trigger_hits"], row["key"]
        if want["expected_tag"] == "clean" and want["scenario"] != "adversarial":
            assert tag["label"] == "clean", row["key"]
    again = await tag_file(small_store, t, x, (await _files(small_store))[0])
    assert again.records == 0  # tagged records are not in the next extract, so history is never rewritten


async def test_judge_proposes_dirty_and_only_four_eyes_makes_it_current(tagged_run, tagged_store):
    run, _ = tagged_run
    labels = _labels(run)
    dirty = {k for k, v in labels.items() if v["expected_tag"] == "dirty"}
    proposed = await tagged_store.run(
        "MATCH (t:Tag {proposed: true, label: 'dirty'})-[:ON]->(r:Record) RETURN r.key AS key, t {.*} AS t"
    )
    stored = {r["k"] for r in await tagged_store.run("MATCH (r:Record) RETURN r.key AS k")}
    found = {p["key"] for p in proposed}
    assert (
        len(found & dirty) / len(dirty & stored) >= 0.95 and found <= dirty
    )  # recall on fixtures, coded language included
    for p in proposed:
        assert set(p["t"]["clauses"]) == set(labels[p["key"]]["clauses"].split(";")) and p["t"]["evidence_span"]
        assert (
            p["t"]["model"] == "scripted" and p["t"]["prompt_version"] == "didolwr-ethics-v1" and not p["t"]["current"]
        )
    [n] = await tagged_store.run("MATCH (t:Tag {current: true, label: 'dirty'}) RETURN count(t) AS n")
    assert n["n"] == 0  # nothing is dirty until people say so
    items = await list_items(tagged_store, kind="dirty_tag", limit=1000)
    assert len(items) == len(proposed) and all(i["four_eyes"] for i in items)

    t, item = Tarddiad(tagged_store), items[0]
    key = item["payload"]["record_key"]
    await decide(tagged_store, t, item["id"], Reviewer(id="ann"), "confirm_dirty", "arranges supply")
    assert (await current_tag(tagged_store, key))["label"] != "dirty"
    await decide(tagged_store, t, item["id"], Reviewer(id="bob"), "confirm_dirty", "agree")
    cur = await current_tag(tagged_store, key)
    assert (cur["label"], cur["method"]) == ("dirty", "human") and [
        h["method"] for h in await history(tagged_store, key)
    ] == ["trigger", "llm", "human"]
    lineage = await t.lineage(EntityRef(label="Tag", key=item["payload"]["tag_id"]))
    kinds = [s.activity["kind"] for s in lineage.steps]
    assert kinds[0] == "Tag" and "Extract" in kinds  # the proposal traces to the extract and its contract version


async def test_prompt_injection_and_a_gullible_model_cannot_clear_anything(scenario_run, scenario_store, tmp_path):
    run, _ = scenario_run
    labels, t, x = _labels(run), Tarddiad(scenario_store), _echdynnwr(scenario_store, tmp_path)
    files = await scenario_store.run("MATCH (f:SourceFile {scenario: 'adversarial'}) RETURN f.sha256 AS sha")
    judge = EthicsJudge(gullible_judge())
    results = [
        await tag_file(scenario_store, t, x, f["sha"], judge=judge, config=DidolwrConfig(clean_sample_rate=1.0))
        for f in files
    ]
    assert sum(r.proposed_dirty for r in results) == 0 and sum(r.injection_suspected for r in results) == 9
    tags = await scenario_store.run("MATCH (t:Tag {current: true})-[:ON]->(r:Record) RETURN r.key AS key, t {.*} AS t")
    by_fixture: dict[str, set[str]] = {}
    for row in tags:
        by_fixture.setdefault(labels[row["key"]]["note"], set()).add(row["t"]["label"])
        assert row["t"]["method"] == "trigger"  # the model wrote no tag at all
    # every trigger hit, disguised or not, stays question even though the model said "no concern"
    for fixture in (
        "inject_dirty_trigger",
        "obfuscated_dirty_leet",
        "obfuscated_dirty_spaced",
        "obfuscated_benign",
        "welsh_trigger_dirty",
        "welsh_insider",
        "welsh_trigger_benign",
    ):
        assert by_fixture[fixture] == {"question"}, fixture
    # text that addresses the classifier is put in front of a person whatever the model said
    flagged = {
        i["payload"]["record_key"]
        for i in await list_items(scenario_store, kind="question_tag", limit=1000)
        if i["payload"]["reason"] == "text addresses a classifier"
    }
    injected = {k for k, v in labels.items() if v["note"].startswith("inject_")}
    assert flagged == injected


async def test_ungrounded_model_output_is_rejected_and_the_record_goes_to_a_person(small_store, tmp_path):
    t, x = Tarddiad(small_store), _echdynnwr(small_store, tmp_path)
    invented = output_model(
        lambda _p: {
            "verdict": "concern",
            "clauses": ["CE99"],
            "evidence_span": "words not in the text",
            "confidence": 1.0,
        }
    )
    [f] = await small_store.run("MATCH (f:SourceFile {dataset: 'messages'}) RETURN f.sha256 AS sha LIMIT 1")
    r = await tag_file(
        small_store,
        t,
        x,
        f["sha"],
        judge=EthicsJudge(invented),
        config=DidolwrConfig(clean_sample_rate=1.0, question_review_rate=0),
    )
    assert r.judged > 0 and r.judge_failures == r.judged and r.proposed_dirty == 0
    items = await list_items(small_store, kind="question_tag", limit=1000)
    assert len(items) == r.judged and {i["payload"]["reason"] for i in items} == {"judge unavailable"}
    with pytest.raises(ModelNotConfigured, match="UNIONDEB_DIDOLWR_MODEL"):
        EthicsJudge()


async def test_the_judge_sees_no_identity(small_store, tmp_path):
    seen: list[str] = []

    def answer(prompt):
        seen.append(prompt)
        return {"verdict": "none", "confidence": 0.5}

    t, x = Tarddiad(small_store), _echdynnwr(small_store, tmp_path)
    for sha in await _files(small_store):
        await tag_file(
            small_store, t, x, sha, judge=EthicsJudge(output_model(answer)), config=DidolwrConfig(clean_sample_rate=1.0)
        )
    [ids] = await small_store.run(
        "MATCH (d:Device)-[:HAS_IDENTIFIER]->(i:Identifier) RETURN collect(DISTINCT i.value) AS v"
    )
    assert seen and not any(v in p for p in seen for v in ids["v"])
    assert all('"context": "' in p and "→" in p or "unknown parties" in p or "(" in p for p in seen)


def test_ground_truth_lookup_covers_the_fixtures(small_run):
    by_text = labels_by_text(small_run[0])
    assert by_text[C.ADVERSARIAL["welsh_coded"].text] == ("dirty", ["CE1"])
    assert by_text[C.ADVERSARIAL["welsh_trigger_benign"].text][0] == "question"
