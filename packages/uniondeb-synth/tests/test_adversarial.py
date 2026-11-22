"""The generator emits labelled adversarial records without disturbing anything else.

Verifies: UND-043
"""

import csv
import json

from uniondeb.synth import content as C
from uniondeb.synth.generate import main as generate

ARGS = ["--days", "3", "--employees", "10", "--externals", "20", "--rows-per-file", "200", "--seed", "11"]


def _labels(run):
    with (run / "ground_truth" / "labels.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_adversarial_records_are_labelled_by_fixture(tmp_path):
    generate(["--out", str(tmp_path / "a"), *ARGS])
    adv = [x for x in _labels(tmp_path / "a") if x["scenario"] == "adversarial"]
    assert {x["note"] for x in adv} == set(C.ADVERSARIAL) and len(adv) == 3 * len(C.ADVERSARIAL)
    for x in adv:
        t = C.ADVERSARIAL[x["note"]]
        assert x["expected_tag"] == t.tag and x["clauses"] == ";".join(t.clauses) and x["subjects"] == ""
    scenario = next(
        s for s in json.loads((tmp_path / "a/ground_truth/scenarios.json").read_text()) if s["name"] == "adversarial"
    )
    assert scenario["records"] == len(adv)


def test_fixtures_cover_injection_obfuscation_and_welsh_coded_language():
    kinds = {t.typology for t in C.ADVERSARIAL.values()}
    assert {"adversarial_injection", "adversarial_obfuscated", "adversarial_welsh_coded"} <= kinds
    # obfuscated triggers and coded Welsh are invisible to the generator's own plain keyword scan
    for key in ("obfuscated_dirty_leet", "obfuscated_dirty_spaced", "obfuscated_benign", "welsh_coded"):
        assert C.trigger_hits(C.ADVERSARIAL[key].text) == []
    assert any(t.lang == "cy" and t.tag == "dirty" for t in C.ADVERSARIAL.values())


def test_adding_the_fixtures_leaves_every_other_record_unchanged(tmp_path):
    generate(["--out", str(tmp_path / "with"), *ARGS])
    generate(["--out", str(tmp_path / "without"), *ARGS, "--scenarios", "insider_dealing,drug_trafficking"])

    def shape(run):
        return sorted(
            (x["scenario"], x["dataset"], x["expected_tag"], x["note"], x["file"])
            for x in _labels(run)
            if x["scenario"] != "adversarial"
        )

    assert shape(tmp_path / "with") == shape(tmp_path / "without")
