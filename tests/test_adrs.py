"""Every ADR is numbered, has a status, and points at evidence that exists."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
ADRS = sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))


def test_adrs_are_numbered_contiguously():
    assert [int(p.name[:4]) for p in ADRS] == list(range(1, len(ADRS) + 1))


def test_each_adr_has_a_status_and_existing_evidence():
    for adr in ADRS:
        text = adr.read_text()
        assert re.search(r"^- Status: \w+", text, re.M), adr.name
        for path in re.findall(r"`((?:scripts|docs)/[^`]+\.(?:py|json|md))`", text):
            assert (ROOT / path).exists(), f"{adr.name} cites missing {path}"


def test_relate_spike_results_support_adr_0002():
    results = json.loads((ROOT / "docs" / "spikes" / "relate-extract.json").read_text())
    adr = (ROOT / "docs" / "adr" / "0002-relate-agent-extracts.md").read_text()
    assert results["read_only"]["write_refused_with"], "read-only mode must refuse writes"
    assert results["plaintext_visible"] is True and "Encryption at rest | **None.**" in adr
    assert f"{results['extract']['bytes_per_record']} bytes per record" in adr
    cap = int(re.search(r"\*\*([\d,]+) records\*\*", adr).group(1).replace(",", ""))
    resident_per_record = results["read_only"]["open_rss_growth_mb"] / results["extract"]["rows"]
    assert cap * resident_per_record < 1024, "the cap must keep a read-only open under 1 GB resident"
