# Test data

All test data is synthetic. Identifiers come from reserved ranges (Ofcom drama numbers, the ITU
test IMSI range, documentation IP addresses), and every run folder contains `README.SYNTHETIC.txt`.

## Build the standard set

```sh
uv run python -m ext_harness make-data
```

This writes to `data/` (git-ignored). Add `--force` to rebuild, and `--seed`, `--start`, `--days`,
`--employees`, `--externals` to change it.

| Folder | Contents |
|---|---|
| `data/run-001/data/` | The files to upload: `<partition>/<dataset>/date=…/part-NNNN.{jsonl,csv,parquet}` |
| `data/run-001/roster/` | `people.csv` and `devices.jsonl`: who owns which identifiers |
| `data/run-001/ground_truth/` | `labels.csv` (expected tag per record), `expected_errors.csv`, `scenarios.json` |
| `data/run-001/manifest.json` | Every file with its SHA-256, row count, format and dialect |
| `data/streams/` | Record streams for the harness's WebSocket endpoint, one JSON record per line |
| `data/edge-cases/` | Files that should be refused, reported as duplicates or held as unreadable |
| `data/README.md`, `data/catalogue.json` | What each file is and what should happen to it |

The default run covers 14 days from 5 October 2026, with 30 employees and 60 external contacts:
6,579 records in 111 files.

## What is in a run

| Partition | What it is |
|---|---|
| `normal` | Background traffic, some of it using trigger words innocently, with a small share of injected faults |
| `insider_dealing` | An employee on an insider list tips a friend before a takeover, then moves to a landline |
| `drug_trafficking` | An employee runs a supply line from unregistered phones and rotates the number |
| `adversarial` | Messages built to defeat a tagger: prompt injection, obfuscated trigger words |

Datasets are `calls`, `messages`, `handoff` and `email`. About a third of files use a
source-system dialect (different column names) to exercise the alias layer.

## Generate a run yourself

```sh
uv run uniondeb synth generate --out data/run-002 --seed 7 --start 2026-09-01 \
    --days 45 --employees 150 --externals 450
```

`--blend` mixes scenario records into the normal partitions. `uniondeb synth generate --help`
lists the rest.

## Journaled email as a stream

```sh
# replay a run's email in time order, as the journal receiver would publish it
uv run uniondeb synth stream replay --run data/run-001 --speed 0 --sink stdout > mail.ndjson

# generate live mail with current timestamps
uv run uniondeb synth stream live --rate 30 --duration 60 --sink stdout
```

## The roster

Scans attribute devices to people through the roster. The bulk loader (`uniondeb load`) loads it
with the run. If you send files through the API or the harness instead, load the roster once
yourself; there is no command for it yet:

```sh
set -a; . deploy/.env; set +a
uv run python - <<'EOF'
import asyncio
from pathlib import Path
from uniondeb.store import StoreSettings, open_runner
from uniondeb.store.load import load_roster

async def main():
    async with open_runner(StoreSettings.from_env(synthetic=True)) as runner:
        print(await load_roster(runner, Path("data/run-001/roster/people.csv")), "people")

asyncio.run(main())
EOF
```
