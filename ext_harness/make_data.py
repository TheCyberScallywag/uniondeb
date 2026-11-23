"""Build the test data set in ``data/``. Everything in it is synthetic.

data/run-001/        a labelled run from ``uniondeb synth generate``: files to upload, roster, ground truth
data/streams/        record streams for the WebSocket endpoint, one JSON record per line
data/edge-cases/     files a delivery platform and Uniondeb should each refuse or flag
data/catalogue.json  every deliverable file and stream, with its dataset and what to expect
data/README.md       the same, for people
"""

import contextlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

from uniondeb.synth import stream as synth_stream
from uniondeb.synth.generate import main as generate

DEFAULTS = {
    "seed": 2026, "start": "2026-10-05", "days": 14, "employees": 30, "externals": 60,
    "rows_per_file": 400, "error_rate": 0.01,
}  # fmt: skip


def build(out: Path, *, run: str = "run-001", force: bool = False, **options: Any) -> dict[str, Any]:
    options = {**DEFAULTS, **{k: v for k, v in options.items() if v is not None}}
    run_dir, streams, edge = out / run, out / "streams", out / "edge-cases"
    for path in (run_dir, streams, edge):
        if path.exists():
            if not force:
                raise SystemExit(f"{path} exists; pass --force to rebuild it")
            shutil.rmtree(path)
    out.mkdir(parents=True, exist_ok=True)

    argv = ["--out", str(run_dir)]
    for key, value in options.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    with contextlib.redirect_stdout(io.StringIO()):
        generate(argv)
    manifest = json.loads((run_dir / "manifest.json").read_text())

    streams.mkdir()
    stream_entries = [_mail_journal(run_dir, streams)]
    for dataset in ("messages", "calls", "handoff"):
        if entry := _record_stream(run_dir, manifest, dataset, streams):
            stream_entries.append(entry)

    edge.mkdir()
    edge_entries = _edge_cases(run_dir, manifest, edge)

    catalogue = {
        "synthetic": True,
        "run": {"path": run, "options": options, "records": manifest["records"], "expected_tags": manifest["expected_tags"],
                "injected_errors": manifest["injected_errors"]},
        "files": [
            {"path": f"{run}/{f['path']}", **{k: f[k] for k in ("dataset", "format", "dialect", "scenario", "date", "rows", "sha256")}}
            for f in manifest["files"]
        ],
        "streams": stream_entries,
        "edge_cases": edge_entries,
    }  # fmt: skip
    (out / "catalogue.json").write_text(json.dumps(catalogue, indent=1) + "\n")
    (out / "README.md").write_text(_readme(catalogue))
    return catalogue


def _mail_journal(run_dir: Path, streams: Path) -> dict[str, Any]:
    """The journaled email of the run in time order, normalised as the journal receiver would publish it."""
    good, bad = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(good), contextlib.redirect_stderr(bad):
        synth_stream.main(["replay", "--run", str(run_dir), "--speed", "0", "--sink", "stdout"])
    dead = [line for line in bad.getvalue().splitlines() if line.startswith('{"error"')]
    (streams / "mail-journal.ndjson").write_text(good.getvalue())
    (streams / "mail-journal.dead-letter.ndjson").write_text("".join(line + "\n" for line in dead))
    return {
        "path": "streams/mail-journal.ndjson", "dataset": "email", "records": good.getvalue().count("\n"),
        "note": f"{len(dead)} messages failed normalisation and are in streams/mail-journal.dead-letter.ndjson (not deliverable)",
    }  # fmt: skip


def _record_stream(run_dir: Path, manifest: dict[str, Any], dataset: str, streams: Path) -> dict[str, Any] | None:
    """The canonical JSONL partitions of one dataset, in date order, as a single stream."""
    parts = sorted(
        (f for f in manifest["files"] if f["dataset"] == dataset and f["format"] == "jsonl" and f["dialect"] == "canonical"),
        key=lambda f: (f["date"], f["scenario"], f["path"]),
    )  # fmt: skip
    if not parts:
        return None
    lines = [line for f in parts for line in (run_dir / f["path"]).read_text().splitlines() if line.strip()]
    (streams / f"{dataset}.ndjson").write_text("".join(line + "\n" for line in lines))
    return {
        "path": f"streams/{dataset}.ndjson", "dataset": dataset, "records": len(lines),
        "note": "the same records as the run's canonical JSONL partitions: Uniondeb links them rather than loading them twice",
    }  # fmt: skip


def _edge_cases(run_dir: Path, manifest: dict[str, Any], edge: Path) -> list[dict[str, Any]]:
    sample = next(f for f in manifest["files"] if f["dataset"] == "messages" and f["format"] == "jsonl" and f["dialect"] == "canonical" and f["rows"] >= 3)  # fmt: skip
    rows = (run_dir / sample["path"]).read_text().splitlines()
    (edge / "empty.jsonl").write_bytes(b"")
    (edge / "notes.txt").write_text("Not a data file.\n")
    (edge / "duplicate-of-run-file.jsonl").write_text((run_dir / sample["path"]).read_text())
    broken = json.loads(rows[0])
    broken.pop("sender", None)
    (edge / "malformed-rows.jsonl").write_text("\n".join([rows[1], rows[2][: len(rows[2]) // 2], json.dumps(broken), "not json at all"]) + "\n")  # fmt: skip
    (edge / "wrong-shape.csv").write_text("colour,size,price\nred,large,4.50\nblue,small,2.25\n")
    return [
        {"path": "edge-cases/empty.jsonl", "expect": "refused by the harness: the file is empty"},
        {"path": "edge-cases/notes.txt", "expect": "refused by the harness: not .jsonl, .csv or .parquet"},
        {"path": "edge-cases/duplicate-of-run-file.jsonl", "dataset": "messages",
         "expect": f"state 'duplicate' once {sample['path']} has been delivered; ingested normally if sent first"},
        {"path": "edge-cases/malformed-rows.jsonl", "dataset": "messages",
         "expect": "state 'unreadable': a line that is not JSON makes Uniondeb hold the whole file as one review item"},
        {"path": "edge-cases/wrong-shape.csv", "expect": "state 'unreadable': the columns match no dataset, so the file becomes one review item"},
    ]  # fmt: skip


def _readme(c: dict[str, Any]) -> str:
    run = c["run"]
    by_dataset: dict[str, list[int]] = {}
    for f in c["files"]:
        entry = by_dataset.setdefault(f["dataset"], [0, 0])
        entry[0] += 1
        entry[1] += f["rows"]
    lines = [
        "# Uniondeb test data",
        "",
        "**Everything here is synthetic.** Built by `uv run python -m ext_harness make-data`; rebuild with `--force`.",
        "The folder is git-ignored.",
        "",
        f"## `{run['path']}/` — files to upload",
        "",
        f"{run['options']['days']} days from {run['options']['start']}, {run['options']['employees']} employees, "
        f"{run['options']['externals']} external contacts, seed {run['options']['seed']}.",
        "",
        "| Dataset | Files | Rows |",
        "| --- | ---: | ---: |",
        *(f"| {d} | {n} | {rows} |" for d, (n, rows) in sorted(by_dataset.items())),
        "",
        "Records by partition: " + ", ".join(f"{k} {v}" for k, v in run["records"].items()) + ".",
        "Expected tags: " + ", ".join(f"{k} {v}" for k, v in run["expected_tags"].items())
        + f". Injected faults that ingest should send to review: {run['injected_errors']}.",
        "",
        "- `data/<partition>/<dataset>/date=…/part-NNNN.{jsonl,csv,parquet}` — the deliverable files, in canonical and source-system dialects",
        "- `roster/`, `config/` — who owns which identifiers; trigger list and draft code of ethics",
        "- `ground_truth/` — expected tag per record, expected ingest errors, scenario actors and timelines",
        "- `manifest.json` — every file with its SHA-256, row count, format and dialect",
        "",
        "## `streams/` — records for the WebSocket endpoint",
        "",
        "| Stream | Dataset | Records | Note |",
        "| --- | --- | ---: | --- |",
        *(f"| `{s['path']}` | {s['dataset']} | {s['records']} | {s['note']} |" for s in c["streams"]),
        "",
        "## `edge-cases/`",
        "",
        "| File | What should happen |",
        "| --- | --- |",
        *(f"| `{e['path']}` | {e['expect']} |" for e in c["edge_cases"]),
        "",
        "`catalogue.json` lists all of the above for scripts.",
        "",
    ]  # fmt: skip
    return "\n".join(lines)
