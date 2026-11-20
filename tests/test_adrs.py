"""Every ADR is numbered, names an existing backlog step, and points at evidence that exists.

Verifies: UND-010, UND-030
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
ADRS = sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
STEPS = {s["id"] for s in yaml.safe_load((ROOT / "backlog" / "backlog.yaml").read_text())["steps"]}


def test_adrs_are_numbered_contiguously():
    assert [int(p.name[:4]) for p in ADRS] == list(range(1, len(ADRS) + 1))


def test_each_adr_names_a_step_status_and_existing_evidence():
    for adr in ADRS:
        text = adr.read_text()
        step = re.search(r"^- Step: (UND-\d{3})$", text, re.M)
        assert step and step.group(1) in STEPS, adr.name
        assert re.search(r"^- Status: \w+", text, re.M), adr.name
        for path in re.findall(r"`((?:scripts|docs)/[^`]+\.(?:py|json|md))`", text):
            assert (ROOT / path).exists(), f"{adr.name} cites missing {path}"


def test_relate_spike_results_support_adr_0002():
    results = json.loads((ROOT / "docs" / "spikes" / "und-030-relate.json").read_text())
    adr = (ROOT / "docs" / "adr" / "0002-relate-agent-extracts.md").read_text()
    assert results["read_only"]["write_refused_with"], "read-only mode must refuse writes"
    assert results["plaintext_visible"] is True and "Encryption at rest | **None.**" in adr
    assert f"{results['extract']['bytes_per_record']} bytes per record" in adr
    cap = int(re.search(r"\*\*([\d,]+) records\*\*", adr).group(1).replace(",", ""))
    resident_per_record = results["read_only"]["open_rss_growth_mb"] / results["extract"]["rows"]
    assert cap * resident_per_record < 1024, "the cap must keep a read-only open under 1 GB resident"
