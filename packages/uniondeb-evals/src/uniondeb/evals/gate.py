"""The offline eval gate (design §8): run the pipeline on a pinned synthetic run and hold it to targets.

Each suite measures one row of the plan's target table against the generator's ground truth
(``labels.csv``, ``expected_errors.csv``, ``scenarios.json``). Per-case suites are pydantic-evals
datasets, so a failing case is named in the report. The gate exits non-zero when any suite is below
its target, which is what blocks a merge.

By default the ethics judge is scripted from ground truth, so the gate measures the pipeline around
the model and is deterministic. ``--model`` runs the same suites against a real model; only then do
the dirty-recall numbers say anything about that model's judgement.
"""

import csv
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from uniondeb.agents.didolwr import DidolwrConfig, EthicsJudge, EthicsJudgement, TriggerList, route, tag_file
from uniondeb.agents.gwyliwr import SensitiveDate, run_scan
from uniondeb.agents.porthor import ingest_file, register_upload
from uniondeb.cases import Actor, AuditLog, CaseService, FlagService, SarGlossary, SarService, SarValidationError
from uniondeb.core.mail import EmailEvent
from uniondeb.evals.scripted import ground_truth_judge, labels_by_text
from uniondeb.extract import ContractRegistry, Echdynnwr
from uniondeb.mail import Cennad, InMemoryPublisher, LocalObjectStore, render
from uniondeb.store import RelateRunner, Tarddiad, migrate
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.load import load_roster
from uniondeb.store.roles import CONTENT_PROPERTIES
from uniondeb.synth import content as C
from uniondeb.synth.generate import main as generate
from uniondeb.synth.stream import replay_docs

PINNED = [
    "--seed",
    "7",
    "--days",
    "14",
    "--employees",
    "30",
    "--externals",
    "60",
    "--rows-per-file",
    "400",
    "--error-rate",
    "0.01",
]


class Suite(BaseModel):
    name: str
    score: float
    target: float
    passed: bool
    cases: int
    detail: dict[str, Any] = {}


class GateReport(BaseModel):
    run: str
    judge: str
    suites: list[Suite]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.suites)


def suite(name: str, score: float, target: float, cases: int, **detail: Any) -> Suite:
    return Suite(name=name, score=round(score, 4), target=target, passed=score >= target, cases=cases, detail=detail)


@dataclass
class Correct(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return ctx.output == ctx.expected_output


async def _dataset(name: str, cases: list[Case], task) -> tuple[float, list[str]]:
    report = await Dataset(name=name, cases=cases, evaluators=[Correct()]).evaluate(task, progress=False)
    wrong = [c.name for c in report.cases if not all(a.value for a in c.assertions.values())]
    wrong += [f.name for f in getattr(report, "failures", [])]
    return (1 - len(wrong) / len(cases)) if cases else 1.0, wrong


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


async def porthor_suites(runner: RelateRunner, run: Path, cipher: FieldCipher) -> list[Suite]:
    t, manifest = Tarddiad(runner), json.loads((run / "manifest.json").read_text())
    expected = _csv(run / "ground_truth" / "expected_errors.csv")
    faults = {(e["file"], int(e["row"])) for e in expected if e["error_type"] != "duplicate_record"}
    rows = accounted = 0
    for entry in manifest["files"]:
        path = run / entry["path"]
        await register_upload(runner, t, path, uploader="arholwr", filename=entry["path"], synthetic=True)
        r = await ingest_file(runner, t, cipher, path)
        rows, accounted = rows + r.rows, accounted + r.loaded + r.linked + r.errors
        await runner.run(
            "MATCH (f:SourceFile {sha256: $s}) SET f.scenario = $scenario",
            {"s": r.sha256, "scenario": entry["scenario"]},
        )
    in_review = {
        (e["file"], e["row"])
        for e in await runner.run(
            "MATCH (f:SourceFile)-[:HAD_ERROR]->(e:IngestError)<-[:ABOUT]-(:ReviewItem) RETURN f.path AS file, e.row AS row"
        )
    }
    labelled = {f"{x['dataset']}:{x['record_key']}" for x in _csv(run / "ground_truth" / "labels.csv")}
    bad = {f"{e['dataset']}:{e['record_key']}" for e in expected if e["error_type"] != "duplicate_record"}
    [stored] = await runner.run("MATCH (r:Record) RETURN collect(r.key) AS k")
    valid = labelled - bad
    return [
        suite("porthor: valid rows accepted", len(valid & set(stored["k"])) / len(valid), 1.0, len(valid)),
        suite(
            "porthor: injected faults in review",
            len(faults & in_review) / max(1, len(faults)),
            1.0,
            len(faults),
            unexpected=len(in_review - faults),
        ),
        suite("porthor: no silent drops", accounted / rows, 1.0, rows),
    ]


def cennad_suite(run: Path, tmp: Path) -> Suite:
    publisher = InMemoryPublisher()
    cennad, sent = Cennad(LocalObjectStore(tmp / "mail"), publisher, synthetic=True), 0
    for doc in replay_docs(run):
        try:
            event = EmailEvent.model_validate(doc)
        except ValueError:
            continue  # an injected fault: never a real journal copy
        cennad.handle(render(event), peer_ip="192.0.2.10")
        sent += 1
    return suite(
        "cennad: journal copies normalised or dead-lettered",
        (len(publisher.sent) + len(publisher.dead)) / max(1, sent),
        1.0,
        sent,
        dead_lettered=len(publisher.dead),
    )


async def trigger_suite(run: Path, triggers: TriggerList) -> Suite:
    cases = []
    for n, (text, (tag, _)) in enumerate(sorted(labels_by_text(run).items())):
        fixture = next((k for k, t in C.ADVERSARIAL.items() if t.text.split("{")[0] in text), None)
        expected = bool(C.trigger_hits(text)) or (fixture or "").startswith("obfuscated")
        if tag == "question":
            expected = True  # recall: every question label must be found
        cases.append(Case(name=f"{n}: {text[:60]}", inputs=text, expected_output=expected))
    score, wrong = await _dataset("didolwr-triggers", cases, lambda text: bool(triggers.scan(text)))
    return suite("didolwr: trigger precision and recall", score, 1.0, len(cases), wrong=wrong[:10])


async def never_downgrade_suite(run: Path, triggers: TriggerList) -> Suite:
    """Whatever a model answers, a record with trigger hits is routed to question, never clean."""
    none = EthicsJudgement(verdict="none", confidence=1.0)
    cases = [
        Case(name=f"{n}: {text[:60]}", inputs=text, expected_output="question")
        for n, text in enumerate(sorted(labels_by_text(run)))
        if triggers.scan(text)
    ]
    score, wrong = await _dataset("didolwr-never-downgrade", cases, lambda text: route(triggers.scan(text), none).label)
    return suite("didolwr: a model cannot clear a trigger hit", score, 1.0, len(cases), wrong=wrong[:10])


async def didolwr_suites(runner: RelateRunner, run: Path, echdynnwr: Echdynnwr, judge: EthicsJudge) -> list[Suite]:
    t = Tarddiad(runner)
    config = DidolwrConfig(clean_sample_rate=1.0, question_review_rate=0.0, concurrency=8)
    failures = 0
    for row in await runner.run("MATCH (f:SourceFile) RETURN f.sha256 AS sha ORDER BY sha"):
        failures += (await tag_file(runner, t, echdynnwr, row["sha"], judge=judge, config=config)).judge_failures
    labels = {f"{x['dataset']}:{x['record_key']}": x for x in _csv(run / "ground_truth" / "labels.csv")}
    stored = {r["k"] for r in await runner.run("MATCH (r:Record) RETURN r.key AS k")}
    dirty = {k for k, v in labels.items() if v["expected_tag"] == "dirty"} & stored
    proposed = {
        r["k"]
        for r in await runner.run("MATCH (t:Tag {proposed: true, label: 'dirty'})-[:ON]->(r:Record) RETURN r.key AS k")
    }
    adversarial = {k for k in dirty if labels[k]["scenario"] == "adversarial"}
    [auto] = await runner.run(
        "MATCH (t:Tag {current: true}) WHERE t.label = 'dirty' OR t.method = 'llm' RETURN count(t) AS n"
    )
    [cleared] = await runner.run(
        "MATCH (t:Tag {current: true, label: 'clean'}) WHERE size(coalesce(t.trigger_hits, [])) > 0 RETURN count(t) AS n"
    )
    items = await runner.run(
        "MATCH (i:ReviewItem {kind: 'dirty_tag'}) RETURN count(i) AS n, sum(CASE WHEN i.four_eyes THEN 1 ELSE 0 END) AS four"
    )
    return [
        suite(
            "didolwr: dirty recall on fixtures",
            len(dirty & proposed) / max(1, len(dirty)),
            0.95,
            len(dirty),
            false_proposals=len(proposed - dirty),
            judge_failures=failures,
        ),
        suite(
            "didolwr: dirty recall on adversarial fixtures",
            len(adversarial & proposed) / max(1, len(adversarial)),
            0.95,
            len(adversarial),
        ),
        suite(
            "didolwr: nothing dirty or model-written is current without review",
            1.0 if auto["n"] == 0 and cleared["n"] == 0 else 0.0,
            1.0,
            len(stored),
        ),
        suite(
            "didolwr: every proposal is in four-eyes review",
            (items[0]["four"] or 0) / max(1, len(proposed)),
            1.0,
            len(proposed),
        ),
    ]


async def gwyliwr_suites(runner: RelateRunner, run: Path, echdynnwr: Echdynnwr) -> list[Suite]:
    scenarios = {s["name"]: s for s in json.loads((run / "ground_truth" / "scenarios.json").read_text())}
    calendar = [
        SensitiveDate(date=x["at"][:10], label="client announcement") for x in scenarios["insider_dealing"]["timeline"]
    ]
    start = datetime.fromisoformat(json.loads((run / "manifest.json").read_text())["start"]).replace(tzinfo=UTC)
    days = json.loads((run / "manifest.json").read_text())["days"]
    result = await run_scan(
        runner, Tarddiad(runner), echdynnwr, pseudonym_key=b"arholwr-gate-key-arholwr-gate-key", window_to=start + timedelta(days=days + 2),
        window_days=days + 3, baseline_days=0, calendar=calendar,
    )  # fmt: skip
    ranked = [
        a["pid"]
        for a in await runner.run(
            "MATCH (a:Alert)-[:ABOUT]->(p:Person) RETURN p.person_id AS pid ORDER BY a.score DESC LIMIT 3"
        )
    ]
    subjects = [
        scenarios["insider_dealing"]["actors"]["insider"]["person_id"],
        scenarios["drug_trafficking"]["actors"]["employee"]["person_id"],
    ]
    suggested = await runner.run(
        "MATCH (a:Alert)-[s:SUGGESTS]->(n) RETURN a.id AS alert, s.type AS type, s.side AS side, coalesce(n.person_id, n.id, n.sha256) AS ref"
    )
    ends: dict[tuple[str, str], dict[str, set[str]]] = {}
    for s in suggested:
        ends.setdefault((s["alert"], s["type"]), {"from": set(), "to": set()})[s["side"]].add(s["ref"])
    found = {(kind, f, to) for (_, kind), e in ends.items() for f in e["from"] for to in e["to"]}
    recovered = {}
    for name in ("insider_dealing", "drug_trafficking"):
        kinds = set()
        for link in scenarios[name]["hidden_links"]:
            exact = (link["type"], link["from"], link["to"]) in found
            exfil = link["type"] == "EXFILTRATED" and any(f[0] == "EXFILTRATED" and f[1] == link["from"] for f in found)
            if exact or exfil:
                kinds.add(link["type"])
        recovered[name] = sorted(kinds)
    handle = await echdynnwr.build("gwyliwr.scan.v1", {"from": "2000-01-01T00:00:00Z", "to": "2100-01-01T00:00:00Z"})
    async with echdynnwr.open(handle) as reader:
        props = await reader.graph.run("MATCH (n) UNWIND keys(n) AS k RETURN collect(DISTINCT k) AS ks")
        rels = await reader.graph.run("MATCH ()-[r]->() UNWIND keys(r) AS k RETURN collect(DISTINCT k) AS ks")
    leaked = (set(props[0]["ks"]) | set(rels[0]["ks"])) & {*CONTENT_PROPERTIES, "name"}
    return [
        suite(
            "gwyliwr: scenario subjects in the top three",
            sum(s in ranked for s in subjects) / 2,
            1.0,
            2,
            top3=ranked,
            alerts=len(result.alerts),
        ),
        suite(
            "gwyliwr: hidden links recovered as hypotheses (two kinds per scenario)",
            min(len(v) for v in recovered.values()) / 2,
            1.0,
            2,
            recovered=recovered,
        ),
        suite(
            "gwyliwr: board-pack forward detected by attachment hash",
            float("EXFILTRATED" in recovered["insider_dealing"]),
            1.0,
            1,
        ),
        suite("gwyliwr: no content properties in its extract", float(not leaked), 1.0, 1, leaked=sorted(leaked)),
    ]


async def cases_suites(runner: RelateRunner, run: Path) -> list[Suite]:
    scenarios = {s["name"]: s for s in json.loads((run / "ground_truth" / "scenarios.json").read_text())}
    audit = AuditLog(runner)
    cases = CaseService(runner, audit, Tarddiad(runner))
    flags = FlagService(runner, audit, cases)
    sars = SarService(runner, audit, cases, flags)
    officer = Actor(id="gate-officer", roles=frozenset({"nominated_officer"}))
    investigator = Actor(id="gate-investigator", roles=frozenset({"investigator"}), person_id="CS-GATE")
    code = next(iter(SarGlossary.load().codes))
    ok = refused = 0
    subjects = [scenarios["insider_dealing"]["actors"]["insider"], scenarios["drug_trafficking"]["actors"]["employee"]]
    for subject in subjects:
        case = await cases.open_manual(subject["person_id"], officer, "eval gate: scenario investigation")
        await runner.run(
            "MERGE (:Person {id: 'owner:Gate Investigator', person_id: 'CS-GATE', kind: 'employee', team: 'Compliance'})"
        )
        await cases.assign(case.id, investigator, officer)
        fields = {
            "subject": {
                "name": subject["name"],
                "date_of_birth": "UNKNOWN",
                "address": "UNKNOWN",
                "identifiers": [subject["person_id"]],
                "occupation": "UNKNOWN",
            },
            "glossary_codes": [code],
            "reason_for_suspicion": f"{code} Synthetic scenario narrative: who, what, where, when, why and how.",
        }
        broken = {**fields, "subject": {**fields["subject"], "address": ""}}
        draft = await sars.draft(case.id, investigator, broken)
        try:
            await sars.approve_and_submit(draft.id, officer)
        except SarValidationError:
            refused += 1
        await sars.update_draft(draft.id, investigator, fields)
        ok += (await sars.approve_and_submit(draft.id, officer)).reference.startswith("MOCK-SAR-")
    verify = await audit.verify()
    return [
        suite(
            "sar mock: scenario payloads accepted, a missing field refused",
            (ok + refused) / (2 * len(subjects)),
            1.0,
            2 * len(subjects),
        ),
        suite("audit: hash chain verifies", float(verify.ok), 1.0, verify.checked),
    ]


async def run_gate(
    run: Path | None = None, *, model: Any = None, args: list[str] | None = None, runner: Any = None
) -> GateReport:
    """Run every suite. ``runner`` is an empty, migrated store to use instead of a temporary embedded one
    (the integration tests pass a Neo4j Enterprise database, so the same suites prove the Cypher there)."""
    with tempfile.TemporaryDirectory(prefix="uniondeb-gate-") as tmp_name:
        tmp = Path(tmp_name)
        if run is None:
            run = tmp / "run"
            generate(["--out", str(run), *(args or PINNED)])
        own = runner is None
        runner = RelateRunner.open(str(tmp / "store.rl")) if own else runner
        try:
            await migrate(runner)
            await load_roster(runner, run / "roster" / "people.csv")
            cipher = FieldCipher(FieldCipher.generate_key())
            echdynnwr = Echdynnwr(
                runner, Tarddiad(runner), ContractRegistry.load(), root=tmp / "extracts", cipher=cipher
            )
            triggers = TriggerList.load()
            judge = EthicsJudge(model if model is not None else ground_truth_judge(labels_by_text(run)))
            suites = await porthor_suites(runner, run, cipher)
            suites.append(cennad_suite(run, tmp))
            suites.append(await trigger_suite(run, triggers))
            suites.append(await never_downgrade_suite(run, triggers))
            suites += await didolwr_suites(runner, run, echdynnwr, judge)
            suites += await gwyliwr_suites(runner, run, echdynnwr)
            suites += await cases_suites(runner, run)
        finally:
            if own:
                await runner.close()
        return GateReport(run=run.name, judge=judge.model_name, suites=suites)
