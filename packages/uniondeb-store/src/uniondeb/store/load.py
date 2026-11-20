"""``uniondeb load <run>``: load a generated synthetic run into the store, with provenance.

For every file in the run's ``manifest.json`` the loader checks the SHA-256, records a
``SourceFile``, parses the rows in whatever format and dialect they arrived in (JSON lines, CSV,
Parquet; canonical or source-system column names), and writes ``Record``, ``Device``,
``Identifier``, ``Person`` and ``EmailAddress`` nodes. One ``Ingest`` activity per file says which
file it used and which records it generated.

Synthetic runs only ever go to the synthetic database: the loader refuses any other target.
Rows that fail validation are counted on the ``SourceFile`` and returned; routing them to human
review is Porthor's job (UND-022).

Implements: UND-017
"""

import argparse
import asyncio
import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from uniondeb.core.mail import EmailEvent
from uniondeb.core.models import CallEvent, HandoffStep, MessageEvent
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.core.schemas import DATASETS, SCHEMA_VERSION, unflatten
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner, open_runner
from uniondeb.store.settings import StoreSettings

LOADER = AgentRef(name="uniondeb-load", version="0.1.0")
BATCH = 1000


class SyntheticIsolationError(RuntimeError):
    pass


@dataclass
class FileResult:
    path: str
    sha256: str
    rows: int = 0
    loaded: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    activity_id: str | None = None


@dataclass
class LoadResult:
    run: str
    database: str
    files: list[FileResult]

    def summary(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "database": self.database,
            "files": len(self.files),
            "rows": sum(f.rows for f in self.files),
            "records": sum(f.loaded for f in self.files),
            "duplicates": sum(f.duplicates for f in self.files),
            "rejected": sum(len(f.rejected) for f in self.files),
        }


# ───────────────────────────── reading ──────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_rows(path: Path, fmt: str) -> list[dict[str, Any]]:
    match fmt:
        case "jsonl":
            with path.open(encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        case "csv":
            with path.open(newline="", encoding="utf-8") as fh:
                return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(fh)]
        case "parquet":
            import pyarrow.parquet as pq

            return pq.read_table(path).to_pylist()
    raise ValueError(f"unsupported format {fmt!r}")


def parse_row(dataset: str, row: dict[str, Any]) -> BaseModel:
    if dataset == "email":
        return EmailEvent.model_validate(row)
    ds = DATASETS[dataset]
    nested = any(isinstance(v, dict) for v in row.values())
    return ds.model.model_validate(row) if nested else unflatten(ds, row)


# ───────────────────────────── mapping to the graph ─────────────────────────────────

SCALARS = (str, int, float, bool)
TIME_FIELD = {"calls": "started_at", "messages": "sent_at", "handoff": "at", "email": "sent_at"}


def record_key(dataset: str, event: BaseModel) -> str:
    match event:
        case CallEvent():
            k = event.call_id
        case MessageEvent():
            k = event.msg_id
        case HandoffStep():
            k = f"{event.conversation_id}#{event.seq}"
        case EmailEvent():
            k = event.message_id
        case _:
            raise TypeError(type(event))
    return f"{dataset}:{k}"


def _device(d: BaseModel) -> dict[str, Any]:
    dump = d.model_dump(mode="json")
    idents = [
        {"kind": k, "value": str(dump[k])}
        for k in ("imei", "imsi", "iccid", "msisdn", "number", "ip", "mac")
        if dump.get(k)
    ]
    return {"id": dump["id"], "type": dump["type"], "owner": dump["owner"], "identifiers": idents}


def to_graph_rows(dataset: str, event: BaseModel) -> dict[str, Any]:
    dump = event.model_dump(mode="json")
    props = {k: v for k, v in dump.items() if isinstance(v, SCALARS)}
    props |= {"dataset": dataset, "schema_version": SCHEMA_VERSION, "occurred_at": dump[TIME_FIELD[dataset]]}
    if dataset == "email":
        props["journal_source"] = dump["journal"]["source"]
    links: list[tuple[str, dict[str, Any]]] = []
    addresses: list[tuple[str, str]] = []
    attachments: list[dict[str, Any]] = []
    match event:
        case CallEvent():
            links = [("FROM", _device(event.caller)), ("TO", _device(event.callee))]
        case MessageEvent():
            links = [("FROM", _device(event.sender)), ("TO", _device(event.recipient))]
        case HandoffStep():
            links = [("ON", _device(event.device))]
        case EmailEvent():
            if event.device is not None:
                links = [("SENT_FROM", _device(event.device))]
            addresses = [("FROM", event.sender.address)]
            addresses += [("TO", a.address) for a in (*event.to, *event.cc, *event.bcc)]
            attachments = [a.model_dump(mode="json") for a in event.attachments]
    return {
        "key": record_key(dataset, event),
        "email": dataset == "email",
        "props": props,
        "links": links,
        "addresses": addresses,
        "attachments": attachments,
    }


async def write_batch(runner: CypherRunner, rows: list[dict[str, Any]], activity_id: str) -> None:
    await runner.run(
        "UNWIND $rows AS row MERGE (r:Record {key: row.key}) SET r += row.props, r.ingest_activity_id = $act",
        {"rows": [{"key": r["key"], "props": r["props"]} for r in rows], "act": activity_id},
    )
    emails = [r["key"] for r in rows if r["email"]]
    if emails:
        await runner.run("UNWIND $keys AS k MATCH (r:Record {key: k}) SET r:Email", {"keys": emails})
    devices = {d["id"]: d for r in rows for _, d in r["links"]}
    if devices:
        await runner.run(
            "UNWIND $devices AS d MERGE (dev:Device {id: d.id}) SET dev.kind = d.type "
            "MERGE (p:Person {id: 'owner:' + d.owner}) ON CREATE SET p.name = d.owner "
            "MERGE (p)-[:USES]->(dev)",
            {"devices": list(devices.values())},
        )
        idents = [{"device": d["id"], **i} for d in devices.values() for i in d["identifiers"]]
        await runner.run(
            "UNWIND $ids AS i MATCH (dev:Device {id: i.device}) MERGE (n:Identifier {kind: i.kind, value: i.value}) "
            "MERGE (dev)-[:HAS_IDENTIFIER]->(n)",
            {"ids": idents},
        )
    by_rel: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        for rel, d in r["links"]:
            by_rel[rel].append({"key": r["key"], "device": d["id"]})
    for rel, pairs in by_rel.items():
        await runner.run(
            f"UNWIND $pairs AS p MATCH (r:Record {{key: p.key}}) MATCH (d:Device {{id: p.device}}) MERGE (r)-[:{rel}]->(d)",
            {"pairs": pairs},
        )
    addr_by_rel: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        for rel, address in r["addresses"]:
            addr_by_rel[rel].append({"key": r["key"], "address": address})
    for rel, pairs in addr_by_rel.items():
        await runner.run(
            f"UNWIND $pairs AS p MATCH (r:Record {{key: p.key}}) MERGE (a:EmailAddress {{address: p.address}}) "
            f"MERGE (r)-[:{rel}]->(a)",
            {"pairs": pairs},
        )
    atts = [{"key": r["key"], **a} for r in rows for a in r["attachments"]]
    if atts:
        await runner.run(
            "UNWIND $atts AS a MATCH (r:Record {key: a.key}) MERGE (x:Attachment {sha256: a.sha256}) "
            "ON CREATE SET x.size = a.size, x.content_type = a.content_type "
            "MERGE (r)-[:ATTACHED {filename: a.filename}]->(x)",
            {"atts": atts},
        )


# ───────────────────────────── the run ──────────────────────────────────────────────


async def load_run(runner: CypherRunner, run_dir: Path, settings: StoreSettings) -> LoadResult:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    synthetic = bool(manifest.get("synthetic")) or any(f.get("synthetic") for f in manifest["files"])
    if synthetic and not settings.synthetic:
        raise SyntheticIsolationError(
            f"{run_dir} is synthetic: load it with --synthetic into {settings.synthetic_database!r}, "
            f"never into {settings.database!r}"
        )
    if not synthetic:
        raise SyntheticIsolationError("only synthetic runs are loaded this way; real data goes through Porthor")
    tarddiad = Tarddiad(runner)
    run_name = manifest.get("run") or run_dir.name
    results = []
    for entry in manifest["files"]:
        path = run_dir / entry["path"]
        actual = sha256_file(path)
        if actual != entry["sha256"]:
            raise ValueError(f"{entry['path']}: sha256 {actual} does not match the manifest")
        result = FileResult(entry["path"], actual)
        await runner.run(
            "MERGE (f:SourceFile {sha256: $sha}) SET f += $props",
            {
                "sha": actual,
                "props": {
                    "path": entry["path"],
                    "bytes": entry["bytes"],
                    "format": entry["format"],
                    "dialect": entry["dialect"],
                    "dataset": entry["dataset"],
                    "scenario": entry["scenario"],
                    "run": run_name,
                    "synthetic": True,
                },
            },
        )
        async with tarddiad.activity(
            ActivityKind.INGEST,
            agent=LOADER,
            trigger=f"uniondeb load {run_name}",
            config_versions={"schema": SCHEMA_VERSION},
        ) as act:
            result.activity_id = act.id
            act.used(EntityRef(label="SourceFile", key=actual))
            rows = read_rows(path, entry["format"])
            result.rows = len(rows)
            mapped: dict[str, dict[str, Any]] = {}
            for i, row in enumerate(rows):
                try:
                    g = to_graph_rows(entry["dataset"], parse_row(entry["dataset"], row))
                except (ValidationError, ValueError, TypeError, KeyError) as exc:
                    result.rejected.append({"row": i, "error": _first_error(exc)})  # row index in file
                    continue
                if g["key"] in mapped:
                    result.duplicates += 1
                mapped[g["key"]] = g
            batch = list(mapped.values())
            for start in range(0, len(batch), BATCH):
                part = batch[start : start + BATCH]
                await write_batch(runner, part, act.id)
                await runner.run(
                    "MATCH (f:SourceFile {sha256: $sha}) UNWIND $keys AS k MATCH (r:Record {key: k}) "
                    "MERGE (f)-[:CONTAINED]->(r)",
                    {"sha": actual, "keys": [g["key"] for g in part]},
                )
            result.loaded = len(batch)
            act.generated(*(EntityRef(label="Record", key=k) for k in mapped))
        await runner.run(
            "MATCH (f:SourceFile {sha256: $sha}) SET f.rows = $rows, f.rows_loaded = $loaded, "
            "f.rows_rejected = $rejected, f.rows_duplicate = $dup",
            {
                "sha": actual,
                "rows": result.rows,
                "loaded": result.loaded,
                "rejected": len(result.rejected),
                "dup": result.duplicates,
            },
        )
        results.append(result)
    return LoadResult(run_name, settings.target_database, results)


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        e = exc.errors()[0]
        where = ".".join(map(str, e["loc"])) or "record"
        return f"{where}: {e['msg']}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


# ───────────────────────────── CLI ──────────────────────────────────────────────────


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("load", help="load a synthetic run (manifest.json) into the synthetic database")
    p.add_argument("run", type=Path, help="run directory written by `uniondeb synth generate`")
    p.add_argument("--uri", help="overrides UNIONDEB_NEO4J_URI (relate:<path> for a local file)")
    p.add_argument("--migrate", action="store_true", help="apply migrations first")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    settings = StoreSettings.from_env(uri=args.uri, synthetic=True)
    return asyncio.run(_main(settings, args.run, args.migrate))


async def _main(settings: StoreSettings, run_dir: Path, do_migrate: bool) -> int:
    from uniondeb.core.observe import configure
    from uniondeb.store.migrations import migrate

    configure("uniondeb-load")
    async with open_runner(settings) as runner:
        if do_migrate:
            await migrate(runner)
        result = await load_run(runner, run_dir, settings)
    summary = result.summary()
    summary["by_reason"] = dict(Counter(r["error"].split(":")[0] for f in result.files for r in f.rejected))
    print(json.dumps(summary))
    return 0
