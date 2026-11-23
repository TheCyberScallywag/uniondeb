"""Crynhoi packs carry evidence and never a verdict; Holwr answers only from its case extract."""

import json

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from uniondeb.agents.crynhoi import DEFAULT_ALTERNATIVES, build_pack, load_pack, rate_pack
from uniondeb.agents.holwr import Holwr
from uniondeb.core.prov import EntityRef
from uniondeb.evals.scripted import output_model
from uniondeb.extract import ContractRegistry, Echdynnwr
from uniondeb.store import Tarddiad
from uniondeb.store.crypto import FieldCipher

CIPHER = FieldCipher(FieldCipher.generate_key())
WINDOW = {"window_from": "2026-08-25T00:00:00Z", "window_to": "2026-09-20T00:00:00Z"}


def _subject(run, scenario="insider_dealing", actor="insider"):
    scenarios = {s["name"]: s for s in json.loads((run / "ground_truth" / "scenarios.json").read_text())}
    return "owner:" + scenarios[scenario]["actors"][actor]["name"], scenarios[scenario]


@pytest.fixture
def echdynnwr(tagged_store, tmp_path):
    return Echdynnwr(tagged_store, Tarddiad(tagged_store), ContractRegistry.load(), root=tmp_path / "x", cipher=CIPHER)


async def _pack(tagged_run, tagged_store, echdynnwr, **kw):
    subject, _ = _subject(tagged_run[0])
    return await build_pack(
        tagged_store,
        Tarddiad(tagged_store),
        echdynnwr,
        CIPHER,
        subject=subject,
        subject_pseudonym="S-TEST",
        **WINDOW,
        **kw,
    )


async def test_pack_has_timeline_evidence_provenance_and_alternatives(tagged_run, tagged_store, echdynnwr):
    pack = await _pack(tagged_run, tagged_store, echdynnwr)
    assert (
        pack.version == 1 and pack.timeline and pack.timeline == sorted(pack.timeline, key=lambda e: (e.at, e.record))
    )
    dirty = [e for e in pack.timeline if e.proposed == "dirty"]
    assert dirty and all(
        e.evidence_span and e.clauses and e.text and e.evidence_span.casefold() in e.text.casefold() for e in dirty
    )
    assert all((e.proposed or e.tag) in ("question", "dirty") for e in pack.timeline)
    t = Tarddiad(tagged_store)
    for e in pack.timeline[:5]:
        prov = pack.provenance[e.record]
        assert prov["source_sha256"] == e.source_sha256 and prov["tag_activity_ids"]
        assert (await t.lineage(EntityRef(label="Record", key=e.record))).source_hashes == [e.source_sha256]
    assert pack.alternative_explanations == list(DEFAULT_ALTERNATIVES) and pack.narrative_by == "none"
    assert pack.recommendation is None and "recommend" not in pack.summary.lower()
    name = _subject(tagged_run[0])[0].removeprefix("owner:")
    assert name not in pack.model_dump_json()

    [stored] = await tagged_store.run("MATCH (c:CasePack {id: $id}) RETURN c {.*} AS c", {"id": pack.id})
    assert dirty[0].text not in json.dumps(stored["c"]) and stored["c"]["state"] == "draft"
    assert await load_pack(tagged_store, CIPHER, pack.id) == pack
    assert (await _pack(tagged_run, tagged_store, echdynnwr)).version == 2
    await rate_pack(tagged_store, pack.id, "ann", 4, "clear timeline")
    with pytest.raises(ValueError):
        await rate_pack(tagged_store, pack.id, "ann", 6)
    lineage = await t.lineage(EntityRef(label="CasePack", key=pack.id))
    assert [s.activity["kind"] for s in lineage.steps][:2] == ["CasePack", "Extract"]


async def test_a_narrative_that_recommends_is_rejected(tagged_run, tagged_store, echdynnwr):
    verdict = output_model(
        lambda _p: {"summary": "The subject is guilty and I recommend dismissal.", "alternative_explanations": ["none"]}
    )
    pack = await _pack(tagged_run, tagged_store, echdynnwr, model=verdict)
    assert pack.narrative_by == "none" and pack.alternative_explanations == list(DEFAULT_ALTERNATIVES)
    fair = output_model(
        lambda _p: {"summary": "Twelve records carry concern tags.", "alternative_explanations": ["A surprise party."]}
    )
    pack = await _pack(tagged_run, tagged_store, echdynnwr, model=fair)
    assert pack.narrative_by == "scripted" and pack.alternative_explanations == ["A surprise party."]


def _holwr_model(queries, answer):
    """Calls run_cypher for each query in turn, then answers with ``answer(rows of the last query)``."""

    def respond(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "run_cypher"
        ]
        if len(returns) < len(queries):
            return ModelResponse(parts=[ToolCallPart("run_cypher", {"query": queries[len(returns)]})])
        try:
            rows = json.loads(str(returns[-1].content).split(" (first")[0]) if returns else []
        except ValueError:
            rows = []
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer(rows))])

    return FunctionModel(respond, model_name="scripted")


async def _case_reader(tagged_run, echdynnwr):
    subject, scenario = _subject(tagged_run[0])
    handle = await echdynnwr.build(
        "ymchwiliad.case.v1", {"subject": subject, "from": WINDOW["window_from"], "to": WINDOW["window_to"]}
    )
    return echdynnwr.open(handle), scenario


async def test_holwr_answers_from_the_case_extract_and_shows_its_cypher(tagged_run, echdynnwr):
    reader, scenario = await _case_reader(tagged_run, echdynnwr)
    tippee = scenario["actors"]["tippee"]["person_id"]
    query = (
        "MATCH (a:Party {is_subject: true})-[c:CONTACT]-(b:Party {person_id: '" + tippee + "'}) "
        "RETURN c.record AS record, c.at AS at, c.medium AS medium ORDER BY at"
    )
    model = _holwr_model(
        [query],
        lambda rows: {"answer": f"{len(rows)} contacts with {tippee}.", "record_keys": [r["record"] for r in rows]},
    )
    async with reader:
        out = await Holwr(model).ask(reader, "Who did they contact most before the announcement?")
        assert out.cypher == [query] and len(out.record_keys) >= 6 and out.model == "scripted"
        flagged = await reader.graph.run("MATCH (r:Record) RETURN r.tag AS tag, r.text IS NOT NULL AS has_text")
        assert all(
            f["has_text"] is False for f in flagged if f["tag"] in ("clean", "untagged")
        )  # content of flagged records only
        assert any(f["has_text"] for f in flagged)
        props = await reader.graph.run("MATCH (n) UNWIND keys(n) AS k RETURN collect(DISTINCT k) AS ks")
        assert "name" not in props[0]["ks"]


async def test_holwr_cannot_write_cite_outside_the_case_or_speculate(tagged_run, echdynnwr):
    reader, _ = await _case_reader(tagged_run, echdynnwr)
    read = "MATCH (r:Record) RETURN r.key AS record LIMIT 3"
    async with reader:
        [before] = await reader.graph.run("MATCH (n) RETURN count(n) AS n")
        with pytest.raises(UnexpectedModelBehavior):  # every attempt is a write: retries run out
            await Holwr(_holwr_model(["MATCH (r:Record) DETACH DELETE r"], lambda rows: {"answer": "done"})).ask(
                reader, "q"
            )
        assert (await reader.graph.run("MATCH (n) RETURN count(n) AS n")) == [before]
        with pytest.raises(UnexpectedModelBehavior):
            await Holwr(
                _holwr_model([read], lambda rows: {"answer": "x", "record_keys": ["messages:not-in-this-case"]})
            ).ask(reader, "q")
        with pytest.raises(UnexpectedModelBehavior):
            await Holwr(
                _holwr_model([read], lambda rows: {"answer": "They are clearly guilty.", "record_keys": []})
            ).ask(reader, "q")
        with pytest.raises(UnexpectedModelBehavior):
            await Holwr(_holwr_model([], lambda rows: {"answer": "From memory: nothing.", "record_keys": []})).ask(
                reader, "q"
            )


async def test_holwr_drafts_a_sar_narrative_but_can_never_submit(tagged_run, echdynnwr):
    reader, _ = await _case_reader(tagged_run, echdynnwr)
    read = "MATCH (r:Record) WHERE r.tag <> 'clean' AND r.text IS NOT NULL RETURN r.key AS record LIMIT 3"
    async with reader:
        draft = await Holwr(
            _holwr_model(
                [read],
                lambda rows: {
                    "glossary_codes": ["XXS99XX"],
                    "reason_for_suspicion": "On 5 September…",
                    "record_keys": [r["record"] for r in rows],
                },
            )
        ).draft_sar(reader)
        assert draft.reason_for_suspicion and draft.record_keys and not draft.refused_actions
        pushy = FunctionModel(
            lambda messages, info: ModelResponse(parts=[ToolCallPart("submit_sar", {"draft_id": "d1"})])
        )
        refused = await Holwr(pushy).draft_sar(reader)
        assert refused.reason_for_suspicion == "" and refused.refused_actions == [
            "submit_sar: needs a nominated officer"
        ]
