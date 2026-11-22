"""The deterministic ingest pipeline: detect, resolve, validate, dedupe, write (plan §5.1).

No model is involved. Every row of an uploaded file ends in exactly one place:

- a ``Record`` (new, or already present with identical content, in which case the file is simply
  linked to it), or
- an ``IngestError`` with a ``ReviewItem``, holding the encrypted raw row, the reason and the
  resolution report, so a person can fix and resubmit or reject it.

The counts on the ``SourceFile`` always add up to its row count, and ``ingest_file`` raises if they
do not: a silent drop is a bug, not a data-quality outcome. Replaying a file (after a mapping is
approved, say) is idempotent: rows that now pass close their earlier error.

Implements: UND-021, UND-022
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from uniondeb.agents.porthor.detect import DetectionError, detect_dataset, detect_format
from uniondeb.agents.porthor.registry import AliasRegistry
from uniondeb.core.mail import EmailEvent
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.core.resolve import ResolutionReport
from uniondeb.core.schemas import DATASETS, SCHEMA_VERSION, unflatten
from uniondeb.store import review
from uniondeb.store.crypto import FieldCipher
from uniondeb.store.load import BATCH, read_rows, sha256_file, to_graph_rows, write_batch
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner

PORTHOR = AgentRef(name="porthor", version="0.1.0")
FORMATS = ("jsonl", "csv", "parquet")
_ROW_ERRORS = (ValidationError, ValueError, TypeError, KeyError)


class IngestInvariantError(RuntimeError):
    pass


@dataclass
class IngestResult:
    sha256: str
    dataset: str | None = None
    format: str | None = None
    rows: int = 0
    loaded: int = 0  # records written by this run
    linked: int = 0  # rows identical to a record already held: linked, not re-ingested
    errors: int = 0  # rows now in human review
    by_reason: dict[str, int] = field(default_factory=dict)
    unknown_columns: dict[str, list[Any]] = field(default_factory=dict)  # column → up to 3 example values
    registry_version: int = 0
    activity_id: str | None = None
    record_keys: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "record_keys"}


async def register_upload(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    path: Path,
    *,
    uploader: str,
    filename: str,
    dataset: str | None = None,
    source_system: str | None = None,
    synthetic: bool = False,
) -> tuple[str, bool]:
    """Record a stored upload as a ``SourceFile`` with its ``Upload`` activity. Returns (sha256, new)."""
    sha = sha256_file(path)
    if await runner.run("MATCH (f:SourceFile {sha256: $sha}) RETURN f.sha256 AS sha", {"sha": sha}):
        return sha, False
    props = {
        "path": filename,
        "bytes": path.stat().st_size,
        "uploaded_by": uploader,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "dataset_hint": dataset,
        "source_system": source_system,
        "synthetic": synthetic,
        "state": "uploaded",
    }
    await runner.run("CREATE (f:SourceFile {sha256: $sha}) SET f += $props", {"sha": sha, "props": props})
    async with tarddiad.activity(ActivityKind.UPLOAD, agent=PORTHOR, trigger=f"uploader:{uploader}") as act:
        act.generated(EntityRef(label="SourceFile", key=sha))
    return sha, True


def _parse(dataset: str, row: dict[str, Any], report: ResolutionReport) -> BaseModel:
    if dataset == "email":
        return EmailEvent.model_validate(row, context={"report": report})
    ds = DATASETS[dataset]
    if any(isinstance(v, dict) for v in row.values()):
        return ds.model.model_validate(row, context={"report": report})
    return unflatten(ds, row, report)


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        e = exc.errors(include_input=False)[0]  # never the value: it may be content or an identifier
        return f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['type']}"
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def _content_hash(event: BaseModel) -> str:
    return hashlib.sha256(event.model_dump_json().encode()).hexdigest()


async def ingest_file(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    cipher: FieldCipher,
    path: Path,
    *,
    dataset: str | None = None,
    fmt: str | None = None,
    registry: AliasRegistry | None = None,
) -> IngestResult:
    """Ingest one stored file. The ``SourceFile`` must exist (``register_upload`` or the loader)."""
    sha = sha256_file(path)
    known = await runner.run("MATCH (f:SourceFile {sha256: $sha}) RETURN f.dataset_hint AS hint", {"sha": sha})
    if not known:
        raise IngestInvariantError(f"no SourceFile {sha}: register the upload first")
    registry = registry or await AliasRegistry.current(runner)
    result = IngestResult(sha, registry_version=registry.version)
    async with tarddiad.activity(
        ActivityKind.INGEST,
        agent=PORTHOR,
        trigger=f"file:{sha[:16]}",
        config_versions={"schema": SCHEMA_VERSION, "alias_registry": str(registry.version)},
    ) as act:
        result.activity_id = act.id
        act.used(EntityRef(label="SourceFile", key=sha))
        try:
            result.format = fmt or detect_format(path)
            if result.format not in FORMATS:
                raise DetectionError(f"unsupported format {result.format!r}")
            rows = read_rows(path, result.format)
            result.rows = len(rows)
            result.dataset = dataset or known[0]["hint"] or detect_dataset(rows)
        except Exception as exc:  # unreadable as a whole: one review item for the file, nothing dropped
            await _file_error(runner, sha, _reason(exc), act)
            result.errors, result.by_reason = 1, {"unreadable_file": 1}
            await _finish(runner, result, state="unreadable")
            return result

        good: dict[str, dict[str, Any]] = {}
        bad: list[dict[str, Any]] = []
        unknown: dict[str, list[Any]] = {}
        for i, raw in enumerate(rows):
            report = ResolutionReport()
            try:
                event = _parse(result.dataset, registry.apply(result.dataset, raw), report)
                g = to_graph_rows(result.dataset, event)
            except _ROW_ERRORS as exc:
                bad.append({"row": i, "raw": raw, "reason": _reason(exc), "report": report.summary()})
            else:
                g["props"]["content_sha256"] = _content_hash(event)
                g["row"] = i
                prior = good.get(g["key"])
                if prior is None:
                    good[g["key"]] = g
                elif prior["props"]["content_sha256"] == g["props"]["content_sha256"]:
                    result.linked += 1
                else:
                    bad.append(
                        {"row": i, "raw": raw, "reason": "duplicate_key: same key, different content", "report": {}}
                    )
            for _, key in report.unknown:
                seen = unknown.setdefault(key, [])
                if len(seen) < 3 and raw.get(key) not in (None, "", *seen):
                    seen.append(raw.get(key))
        result.unknown_columns = unknown

        held = {
            r["key"]: r["sha"]
            for r in await runner.run(
                "UNWIND $keys AS k MATCH (r:Record {key: k}) RETURN r.key AS key, r.content_sha256 AS sha",
                {"keys": list(good)},
            )
        }
        fresh = []
        for key, g in good.items():
            if key not in held:
                fresh.append(g)
            elif held[key] in (None, g["props"]["content_sha256"]):
                result.linked += 1
            else:  # the record of truth is never overwritten by a later file
                bad.append(
                    {
                        "row": g["row"],
                        "raw": rows[g["row"]],
                        "reason": "duplicate_key: differs from the stored record",
                        "report": {},
                    }
                )
        for start in range(0, len(fresh), BATCH):
            await write_batch(runner, fresh[start : start + BATCH], act.id)
        bad_rows = {b["row"] for b in bad}
        keys = [k for k, g in good.items() if g["row"] not in bad_rows]
        for start in range(0, len(keys), BATCH):
            await runner.run(
                "MATCH (f:SourceFile {sha256: $sha}) UNWIND $keys AS k MATCH (r:Record {key: k}) MERGE (f)-[:CONTAINED]->(r)",
                {"sha": sha, "keys": keys[start : start + BATCH]},
            )
        result.loaded, result.record_keys = len(fresh), [g["key"] for g in fresh]
        act.generated(*(EntityRef(label="Record", key=g["key"]) for g in fresh))

        result.errors = len(bad)
        result.by_reason = dict(Counter(b["reason"].split(":")[-1].strip() for b in bad))
        for ref in await _capture_errors(runner, cipher, sha, result, bad):
            act.generated(ref)
        if result.loaded + result.linked + result.errors != result.rows:
            raise IngestInvariantError(
                f"{sha[:16]}: {result.rows} rows but {result.loaded} loaded + {result.linked} linked + {result.errors} errors"
            )
    await _finish(runner, result, state="ingested")
    return result


async def resubmit_row(
    runner: CypherRunner, tarddiad: Tarddiad, error_id: str, fixed: dict[str, Any], *, by: str
) -> str:
    """A reviewer's corrected row for one ``IngestError``: validated like any other row, then written
    with its own activity. Returns the record key. Raises if the row still fails the contract."""
    found = await runner.run(
        "MATCH (f:SourceFile)-[:HAD_ERROR]->(e:IngestError {id: $id}) RETURN f.sha256 AS sha, e.dataset AS dataset",
        {"id": error_id},
    )
    if not found:
        raise IngestInvariantError(f"no ingest error {error_id}")
    sha, dataset = found[0]["sha"], found[0]["dataset"]
    event = _parse(dataset, fixed, ResolutionReport())
    g = to_graph_rows(dataset, event)
    g["props"]["content_sha256"] = _content_hash(event)
    held = await runner.run("MATCH (r:Record {key: $k}) RETURN r.content_sha256 AS sha", {"k": g["key"]})
    if held and held[0]["sha"] not in (None, g["props"]["content_sha256"]):
        raise ValueError(f"record {g['key']} already exists with different content")
    async with tarddiad.activity(
        ActivityKind.INGEST, agent=PORTHOR, trigger=f"reviewer:{by}", config_versions={"schema": SCHEMA_VERSION}
    ) as act:
        act.used(EntityRef(label="SourceFile", key=sha), EntityRef(label="IngestError", key=error_id))
        if not held:
            await write_batch(runner, [g], act.id)
            act.generated(EntityRef(label="Record", key=g["key"]))
        await runner.run(
            "MATCH (f:SourceFile {sha256: $sha}) MATCH (r:Record {key: $key}) MATCH (e:IngestError {id: $id}) "
            "MERGE (f)-[:CONTAINED]->(r) SET e.resolved_at = $now, e.resolved_by = $by, e.state = 'fixed', "
            "f.rows_loaded = coalesce(f.rows_loaded, 0) + 1, f.rows_rejected = coalesce(f.rows_rejected, 1) - 1",
            {"sha": sha, "key": g["key"], "id": error_id, "now": datetime.now(UTC).isoformat(), "by": by},
        )
    return g["key"]


async def _capture_errors(
    runner: CypherRunner, cipher: FieldCipher, sha: str, result: IngestResult, bad: list[dict[str, Any]]
) -> list[EntityRef]:
    """One ``IngestError`` and one open ``ReviewItem`` per failing row; rows that now pass are closed."""
    now, refs = datetime.now(UTC).isoformat(), []
    failing = {f"{sha[:24]}:{b['row']}" for b in bad}
    await runner.run(
        "MATCH (:SourceFile {sha256: $sha})-[:HAD_ERROR]->(e:IngestError) WHERE NOT e.id IN $failing "
        "SET e.resolved_at = coalesce(e.resolved_at, $now) "
        "WITH e MATCH (i:ReviewItem)-[:ABOUT]->(e) WHERE i.state IN $open "
        "SET i.state = 'resolved_by_replay', i.decided_at = $now",
        {"sha": sha, "failing": sorted(failing), "now": now, "open": list(review.OPEN_STATES)},
    )
    for b in bad:
        error_id = f"{sha[:24]}:{b['row']}"
        existed = await runner.run(
            "MATCH (f:SourceFile {sha256: $sha}) MERGE (e:IngestError {id: $id}) "
            "ON CREATE SET e.created_at = $now "
            "SET e.row = $row, e.reason = $reason, e.dataset = $dataset, e.format = $format, e.raw_enc = $raw, "
            "e.report_json = $report, e.registry_version = $registry "
            "MERGE (f)-[:HAD_ERROR]->(e) "
            "WITH e OPTIONAL MATCH (i:ReviewItem)-[:ABOUT]->(e) WHERE i.state IN $open RETURN count(i) AS open",
            {
                "sha": sha,
                "id": error_id,
                "now": now,
                "row": b["row"],
                "reason": b["reason"],
                "dataset": result.dataset,
                "format": result.format,
                "raw": cipher.encrypt_json(b["raw"]),
                "report": json.dumps(b["report"], default=str, ensure_ascii=False),
                "registry": result.registry_version,
                "open": list(review.OPEN_STATES),
            },
        )
        ref = EntityRef(label="IngestError", key=error_id)
        refs.append(ref)
        if not existed[0]["open"]:
            item = await review.create_item(
                runner,
                "ingest_error",
                created_by=PORTHOR.name,
                about=ref,
                summary=f"{result.dataset} row {b['row']}: {b['reason']}",
                payload={"file": sha, "row": b["row"], "dataset": result.dataset, "reason": b["reason"]},
            )
            refs.append(EntityRef(label="ReviewItem", key=item))
    return refs


async def _file_error(runner: CypherRunner, sha: str, reason: str, act: Any) -> None:
    item = await review.create_item(
        runner,
        "ingest_error",
        created_by=PORTHOR.name,
        about=EntityRef(label="SourceFile", key=sha),
        summary=f"file could not be read: {reason}",
        payload={"file": sha, "reason": reason},
    )
    act.generated(EntityRef(label="ReviewItem", key=item))


async def _finish(runner: CypherRunner, result: IngestResult, *, state: str) -> None:
    await runner.run(
        "MATCH (f:SourceFile {sha256: $sha}) SET f.state = $state, f.dataset = $dataset, f.format = $format, "
        "f.rows = $rows, f.rows_loaded = coalesce(f.rows_loaded, 0) + $loaded, f.rows_rejected = $errors, "
        "f.rows_duplicate = $linked, f.ingested_at = $now, f.unknown_columns = $unknown",
        {
            "sha": result.sha256,
            "state": state,
            "dataset": result.dataset,
            "format": result.format,
            "rows": result.rows,
            "loaded": result.loaded,
            "errors": result.errors,
            "linked": result.linked,
            "now": datetime.now(UTC).isoformat(),
            "unknown": sorted(result.unknown_columns),
        },
    )
