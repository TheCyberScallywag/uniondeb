"""Synthetic traffic: templates honour the trigger list, scenarios are coherent, and every
generated file replays through the ingest path except exactly the injected faults.

Verifies: UND-012
"""

import csv
import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from uniondeb.core.mail import EmailEvent
from uniondeb.core.schemas import DATASETS, unflatten
from uniondeb.synth import content as C

pq = pytest.importorskip("pyarrow.parquet")


def _all_templates():
    groups = (
        C.NORMAL_MESSAGES,
        C.NORMAL_TRANSCRIPTS,
        C.NORMAL_NOTES,
        C.BENIGN_TRIGGER_MESSAGES,
        C.BENIGN_TRIGGER_TRANSCRIPTS,
        tuple(C.INSIDER.values()),
        tuple(C.DRUGS.values()),
    )
    return [t for g in groups for t in g]


@pytest.mark.parametrize("t", _all_templates(), ids=lambda t: t.text[:40])
def test_template_tags_agree_with_trigger_list(t) -> None:
    hits = C.trigger_hits(
        C.render(
            t,
            place="Y Caban",
            time="7pm",
            day="Tuesday",
            first="Nia",
            company="Tonnau Ynni plc",
            ticker="TNNU",
            project="Prosiect Morlo",
        )
    )
    if t.tag == "clean":
        assert not hits, f"clean template contains triggers {hits}"
    if t.tag == "question":
        assert hits, "question template must contain a trigger"
    if t.tag == "dirty":
        assert t.clauses and set(t.clauses) <= set(C.CODE_OF_ETHICS_DRAFT)


def test_some_dirty_content_has_no_trigger_words() -> None:
    """Keyword scanning alone must not be enough: coded language exists in the fixtures."""
    dirty = [t for t in _all_templates() if t.tag == "dirty"]
    assert any(not C.trigger_hits(C.render(t, company="X", ticker="Y", project="Z")) for t in dirty)


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Path:
    from uniondeb.synth.generate import main

    out = tmp_path_factory.mktemp("traffic") / "run"
    main(
        [
            "--out",
            str(out),
            "--employees",
            "30",
            "--externals",
            "60",
            "--days",
            "14",
            "--rows-per-file",
            "800",
            "--error-rate",
            "0.02",
            "--messy-share",
            "0.5",
        ]
    )
    return out


def _rows(run: Path, f: dict):
    p = run / f["path"]
    if f["format"] == "jsonl":
        return [json.loads(line) for line in p.open()]
    if f["format"] == "csv":
        return list(csv.DictReader(p.open()))
    return pq.read_table(p).to_pylist()


def test_replay_matches_injected_errors_exactly(run: Path) -> None:
    manifest = json.loads((run / "manifest.json").read_text())
    expected = {
        (r["file"], int(r["row"])): r["error_type"]
        for r in csv.DictReader((run / "ground_truth/expected_errors.csv").open())
    }
    assert {f["dialect"] for f in manifest["files"]} > {"canonical"}
    assert {f["format"] for f in manifest["files"]} == {"jsonl", "csv", "parquet"}
    seen: Counter[str] = Counter()
    for f in manifest["files"]:
        ds = DATASETS.get(f["dataset"])
        for i, r in enumerate(_rows(run, f)):
            exp = expected.get((f["path"], i))
            try:
                if f["dataset"] == "email":
                    ev = EmailEvent.model_validate(r)
                else:
                    ev = ds.model.model_validate(r) if f["format"] == "jsonl" else unflatten(ds, r)
            except ValidationError, ValueError:
                assert exp not in (None, "duplicate_record"), (
                    f"{f['path']} row {i} ({f['dialect']}) failed unexpectedly"
                )
                continue
            assert exp in (None, "duplicate_record"), f"{f['path']} row {i}: injected {exp} was accepted"
            seen[
                getattr(ev, "call_id", None)
                or getattr(ev, "msg_id", None)
                or getattr(ev, "message_id", None)
                or f"{ev.conversation_id}#{ev.seq}"
            ] += 1
    dupes = sum(1 for v in expected.values() if v == "duplicate_record")
    assert sum(1 for n in seen.values() if n > 1) == dupes


def manifest_files(run: Path) -> list[dict]:
    return json.loads((run / "manifest.json").read_text())["files"]


def test_stream_replay_matches_email_faults(run: Path) -> None:
    from uniondeb.synth.stream import Stats, publish, replay_docs

    class Null:
        def send(self, *a) -> None: ...
        def dead_letter(self, *a) -> None: ...
        def close(self) -> None: ...

    stats = Stats()
    for doc in replay_docs(run):
        publish(Null(), doc, stats)
    faults = [
        r
        for r in csv.DictReader((run / "ground_truth/expected_errors.csv").open())
        if r["dataset"] == "email" and r["error_type"] != "duplicate_record"
    ]
    assert stats.dead_lettered == len(faults) and stats.sent > 0
    assert stats.by_direction.get("to_personal", 0) >= 1  # the board-pack forward


def test_ground_truth_is_complete(run: Path) -> None:
    labels = list(csv.DictReader((run / "ground_truth/labels.csv").open()))
    by_scn = Counter((r["scenario"], r["expected_tag"]) for r in labels)
    assert by_scn[("insider_dealing", "dirty")] >= 4 and by_scn[("drug_trafficking", "dirty")] >= 10
    assert by_scn[("normal", "question")] > 0 and by_scn[("normal", "dirty")] == 0
    scenarios = json.loads((run / "ground_truth/scenarios.json").read_text())
    links = {lk["type"] for s in scenarios for lk in s["hidden_links"]}
    assert {"TIPPED", "TRADED_VIA", "CONTROLS", "SAME_LINE", "EXFILTRATED"} <= links
    emails = [r for r in labels if r["dataset"] == "email"]
    assert any(r["typology"] == "insider_exfiltration" and r["expected_tag"] == "dirty" for r in emails)
    assert len({f["path"] for f in manifest_files(run)}) == len(manifest_files(run))  # no partition overwritten
    assert (run / "README.SYNTHETIC.txt").exists()
    manifest = json.loads((run / "manifest.json").read_text())
    assert all(f["synthetic"] and len(f["sha256"]) == 64 for f in manifest["files"])
