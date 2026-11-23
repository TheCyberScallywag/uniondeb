# Command line

One command, `uniondeb`, with a subcommand per area. Run them with `uv run uniondeb …` on the
host (after `set -a; . deploy/.env; set +a`) or `docker compose run --rm uniondeb …` in a
container, where paths are relative to `./data`. Every subcommand has `--help`.

## Data

| Command | What it does |
|---|---|
| `uniondeb synth generate --out DIR` | Write a labelled synthetic run. Options: `--seed`, `--start`, `--days`, `--employees`, `--externals`, `--format {mixed,jsonl,csv,parquet}`, `--rows-per-file`, `--error-rate`, `--messy-share`, `--scenarios`, `--blend` |
| `uniondeb synth stream replay --run DIR` | Replay a run's email in time order. `--speed 0` is as fast as possible; `--sink {stdout,kafka,http}`; `--raw` skips normalisation |
| `uniondeb synth stream live` | Generate email with current timestamps: `--rate`, `--scenario-rate`, `--duration` |
| `uniondeb load DIR` | Bulk-load a synthetic run into the synthetic database. `--migrate` applies migrations first |
| `uniondeb schemas export` | Write the dataset schemas as JSON Schema, Table Schema and Arrow |

## Services

| Command | What it does |
|---|---|
| `uniondeb api serve` | The API, review console and portal. `--host`, `--port` (default `127.0.0.1:8000`) |
| `uniondeb api token --sub ID --roles a,b` | Mint a one-hour development token. `--person-id` sets the user's roster id |
| `uniondeb worker run` | Run queued and scheduled jobs until stopped |
| `uniondeb worker once` | Enqueue what is due, run everything queued, and exit |
| `uniondeb worker enqueue KIND --args JSON` | Queue one job: `ingest`, `tag`, `scan`, `reap`, `retention`, `audit_verify` |
| `uniondeb cennad serve` | The SMTP journal receiver |
| `uniondeb cennad parse FILE.eml` | Print what one message would be published as |

`worker` takes `--synthetic`; the API and the compose stack select the synthetic database with
`UNIONDEB_SYNTHETIC=1`.

## Store

| Command | What it does |
|---|---|
| `uniondeb store status` | Which migrations are applied |
| `uniondeb store migrate` | Apply pending migrations |
| `uniondeb store roles` | Print the database roles script; `--apply` reconciles the server |
| `uniondeb store keygen` | Generate a field-encryption key |
| `uniondeb store backup --file F` | Write a logical backup |
| `uniondeb store restore --file F` | Restore one into an empty database |
| `uniondeb store rotate-key` | Re-encrypt content from `UNIONDEB_FIELD_KEY` to `UNIONDEB_FIELD_KEY_NEW` |

All take `--synthetic`, `--database` and `--uri`.

## Assurance

| Command | What it does |
|---|---|
| `uniondeb audit verify` | Check the audit hash chain. `--anchor F` also checks a signed head |
| `uniondeb audit anchor --out F` | Write the signed head of the chain |
| `uniondeb evals gate` | Run the whole pipeline on a pinned synthetic run and fail on any regression. `--model provider:model` judges with a real model; `--json F` writes the report |
| `uniondeb evals online` | Reviewer agreement and concern rates by team and language |
| `uniondeb extract contracts` | List the extract contracts |
| `uniondeb extract reap` | Delete expired extracts |

## Delivery harness

A separate program: `uv run python -m ext_harness …`. See
[Sending data through the delivery harness](../run-harness.md).

| Command | What it does |
|---|---|
| `serve` | Run the harness (`--host`, `--port`, default `127.0.0.1:8090`) |
| `make-data` | Build the [test data](../test-data.md) |
| `send PATH…` | Deliver files or folders. `--dataset`, `--source-system`, `--limit`, `--watch` |
| `stream FILE --dataset D` | Send JSON lines over the WebSocket. `--rate`, `--batch`, `--limit`, `--watch` |
| `watch` | Print status events. `--replay` starts with those the harness remembers |
