"""Write labelled traffic as partitioned files, the way it would arrive from source systems.

* **formats:** JSONL (nested, wire format), CSV and Parquet (flat), fixed or mixed per file.
* **dialects:** a share of files use a source system's own naming (``A-Party IMEI``,
  ``short_message``, ``Thread ID``…) and value spellings (``NO ANSWER``, ``DELIVRD``,
  national-format numbers). The ingest agent must resolve all of them via the alias layer.
* **injected errors:** a small share of normal rows are corrupted (bad Luhn, missing or
  impossible timestamps, unidentifiable devices, conflicting aliases, duplicates). Each
  is recorded in ``ground_truth/expected_errors.csv`` so the human-review path can be
  evaluated too.

Implements: UND-012
"""

import csv
import hashlib
import json
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from uniondeb.core.schemas import DATASETS, csv_value, flatten, to_arrow_table

from .scenarios import Labelled

FORMATS = ("jsonl", "csv", "parquet")

# ───────────────────────────── source-system dialects ───────────────────────────────
DEVICE_FIELDS = {
    "owner": "Subscriber Name",
    "type": "Terminal Type",
    "imei": "IMEI",
    "iccid": "SIM Serial",
    "imsi": "IMSI",
    "msisdn": "MSISDN",
    "number": "Line Number",
    "ip": "IP Address",
    "mac": "MAC",
    "hostname": "Host Name",
}
SMPP_DEVICE_FIELDS = {
    "owner": "user",
    "type": "kind",
    "imei": "handset_id",
    "iccid": "sim_number",
    "imsi": "subscriber_id",
    "msisdn": "mobile_number",
    "number": "tel",
    "ip": "client_ip",
    "mac": "mac_address",
    "hostname": "host",
}


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    roles: dict[str, str]
    fields: dict[str, str]
    device_fields: dict[str, str]
    sep: str = " "
    values: dict[str, dict[str, str]] = field(default_factory=dict)
    drop: frozenset[str] = frozenset({"id", "address", "route", "body_chars"})
    national_numbers: bool = True


DIALECTS: dict[str, Dialect] = {
    "calls": Dialect(
        "switch_cdr",
        {"caller": "A-Party", "callee": "B-Party"},
        {
            "call_id": "CDR ID",
            "started_at": "Seizure Time",
            "duration_s": "billsec",
            "status": "Disposition",
            "network": "Bearer",
            "transcript": "Call Notes",
        },
        DEVICE_FIELDS,
        values={
            "status": {"answered": "ANSWERED", "missed": "NO ANSWER", "dropped": "FAILED", "voicemail": "VOICEMAIL"},
            "network": {"VoLTE": "IMS", "PSTN": "TDM", "VoIP": "SIP"},
            "type": {"mobile": "handset", "landline": "PSTN", "computer": "softphone"},
        },
    ),
    "messages": Dialect(
        "smpp_export",
        {"sender": "originator", "recipient": "receiver"},
        {
            "msg_id": "message_id",
            "sent_at": "submit_date",
            "channel": "msg_type",
            "body": "short_message",
            "delivered": "stat",
        },
        SMPP_DEVICE_FIELDS,
        sep="_",
        values={
            "channel": {
                "SMS": "sms",
                "RCS": "rcs chat",
                "MMS": "picture message",
                "WEB": "web chat",
                "TEXT_TO_LANDLINE": "tts",
            },
            "delivered": {"True": "DELIVRD", "False": "UNDELIV"},
            "type": {"mobile": "cell", "landline": "fixed line", "computer": "laptop"},
        },
    ),
    "handoff": Dialect(
        "crm_export",
        {"device": "Endpoint"},
        {
            "conversation_id": "Thread ID",
            "seq": "Step No",
            "channel": "Medium",
            "action": "Activity",
            "at": "Event Time",
            "content": "Notes",
        },
        DEVICE_FIELDS,
        values={
            "channel": {
                "CALL": "Voice Call",
                "VOICEMAIL": "Voice Mail",
                "SMS": "Text",
                "EMAIL": "E-mail",
                "CHAT": "Live Chat",
                "VIDEO": "Video Call",
            },
            "type": {"mobile": "smartphone", "landline": "home phone", "computer": "workstation"},
        },
    ),
}


def _national(e164: str) -> str:
    if e164.startswith("+447"):
        n = "0" + e164[3:]
        return f"{n[:5]} {n[5:]}"
    if e164.startswith("+4429"):
        n = "0" + e164[3:]
        return f"{n[:3]} {n[3:7]} {n[7:]}"
    return "0" + e164[3:] if e164.startswith("+44") else e164


def _imei_dashed(v: str) -> str:
    return f"{v[:2]}-{v[2:8]}-{v[8:14]}-{v[14:]}"


def _value(dialect: Dialect, field_name: str, v: Any) -> Any:
    if v is None:
        return None
    mapped = dialect.values.get(field_name, {}).get(str(v))
    if mapped is not None:
        return mapped
    if dialect.national_numbers and field_name in ("msisdn", "number") and isinstance(v, str):
        return _national(v)
    if field_name == "imei" and isinstance(v, str) and len(v) == 15:
        return _imei_dashed(v)
    return v


def to_dialect_flat(name: str, row: dict[str, Any]) -> dict[str, Any]:
    d, ds = DIALECTS[name], DATASETS[name]
    out: dict[str, Any] = {}
    for k, v in row.items():
        role, _, fld = k.partition("_")
        if role in ds.roles:
            if fld in d.drop:
                continue
            out[f"{d.roles[role]}{d.sep}{d.device_fields.get(fld, fld)}"] = _value(d, fld, v)
        elif k not in d.drop:
            out[d.fields.get(k, k)] = _value(d, k, v)
    return out


def to_dialect_nested(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    d, ds = DIALECTS[name], DATASETS[name]
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if k in ds.roles and isinstance(v, dict):
            out[d.roles[k]] = {d.device_fields.get(f, f): _value(d, f, x) for f, x in v.items() if f not in d.drop}
        elif k not in d.drop:
            out[d.fields.get(k, k)] = _value(d, k, v)
    return out


# ───────────────────────────── error injection (on canonical shape) ──────────────────
TIME_FIELD = {"calls": "started_at", "messages": "sent_at", "handoff": "at"}
CONFLICT = {
    "calls": ("duration", lambda r: (r.get("duration_s") or 0) + 17),
    "messages": ("text", lambda r: "a different message body"),
    "handoff": ("step", lambda r: (r.get("seq") or 1) + 5),
}
ERROR_TYPES = (
    "bad_imei_check_digit",
    "missing_timestamp",
    "impossible_timestamp",
    "unidentifiable_device",
    "conflicting_aliases",
    "duplicate_record",
)


def inject_error(rng: random.Random, name: str, rec: dict[str, Any], flat: bool) -> tuple[str, dict[str, Any]]:
    ds = DATASETS[name]
    rec = dict(rec) if flat else json.loads(json.dumps(rec))
    kind = rng.choice(ERROR_TYPES)
    role = rng.choice(ds.roles)

    def dev() -> dict[str, Any]:
        return rec if flat else rec[role]

    def key(f: str) -> str:
        return f"{role}_{f}" if flat else f

    if kind == "bad_imei_check_digit":
        if dev().get(key("imei")):
            v = dev()[key("imei")]
            dev()[key("imei")] = v[:-1] + str((int(v[-1]) + 1) % 10)
        else:
            kind = "missing_timestamp"
    if kind == "missing_timestamp":
        rec.pop(TIME_FIELD[name], None)
    elif kind == "impossible_timestamp":
        rec[TIME_FIELD[name]] = "31/02/2026 25:61"
    elif kind == "unidentifiable_device":
        for f in ("type", "imei", "iccid", "imsi", "msisdn", "number", "ip", "mac", "hostname", "id", "address"):
            dev().pop(key(f), None)
    elif kind == "conflicting_aliases":
        alias, make = CONFLICT[name]
        rec[alias] = make(rec)
    return kind, rec


# ───────────────────────────── email: Graph-style export + faults ───────────────────
def _graph_addr(a: dict[str, Any]) -> dict[str, Any]:
    return {"emailAddress": {"name": a.get("name"), "address": a["address"]}}


def to_graph(doc: dict[str, Any]) -> dict[str, Any]:
    """Canonical email JSON → the shape a Microsoft Graph export would have (camelCase,
    ``emailAddress`` wrappers, HTML body, envelope as a header-style string)."""
    import html as _html

    dev = doc.get("device")
    out: dict[str, Any] = {
        "internetMessageId": doc["message_id"],
        "subject": doc.get("subject", ""),
        "body": {
            "contentType": "html",
            "content": "<html><body><p>"
            + _html.escape(doc.get("body") or "").replace("\n", "<br>")
            + "</p></body></html>",
        },
        "from": _graph_addr(doc["sender"]),
        "toRecipients": [_graph_addr(a) for a in doc.get("to", [])],
        "ccRecipients": [_graph_addr(a) for a in doc.get("cc", [])],
        "bccRecipients": [_graph_addr(a) for a in doc.get("bcc", [])],
        "sentDateTime": doc.get("sent_at"),
        "hasAttachments": bool(doc.get("attachments")),
        "attachments": [
            {"name": a["filename"], "contentType": a["content_type"], "size": a["size"], "sha256": a["sha256"]}
            for a in doc.get("attachments", [])
        ],
        "journal": {
            "journal_type": {
                "m365_envelope": "Journal report",
                "gws_bcc": "Google Workspace",
                "bcc_rule": "transport rule",
            }[doc["journal"]["source"]],
            "receivedDateTime": doc["journal"]["received_at"],
            "envelope_to": "; ".join(a["address"] for a in doc["journal"].get("envelope_recipients", [])),
        },
    }
    if doc.get("in_reply_to"):
        out["inReplyTo"] = doc["in_reply_to"]
    if dev:
        kind = {"computer": "laptop", "mobile": "smartphone"}.get(dev["type"], dev["type"])
        out["device"] = {
            "kind": kind,
            "user": dev["owner"],
            **{DEVICE_FIELDS.get(k, k): v for k, v in dev.items() if k not in ("type", "owner", "id", "address")},
        }
    return out


EMAIL_ERRORS = (
    "missing_timestamp",
    "impossible_timestamp",
    "invalid_sender_address",
    "external_sender",
    "no_recipients",
    "duplicate_record",
)


def inject_email_error(rng: random.Random, doc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    doc = json.loads(json.dumps(doc))
    kind = rng.choice(EMAIL_ERRORS)
    match kind:
        case "missing_timestamp":
            doc.pop("sent_at", None)
        case "impossible_timestamp":
            doc["sent_at"] = "31/02/2026 25:61"
        case "invalid_sender_address":
            doc["sender"]["address"] = "not-an-address"
        case "external_sender":  # mis-routed journal copy of inbound mail
            doc["sender"]["address"] = "someone@example.org"
        case "no_recipients":
            doc["to"], doc["cc"], doc["bcc"] = [], [], []
            doc["journal"]["envelope_recipients"] = []
    return kind, doc


# ───────────────────────────── file writing ─────────────────────────────────────────
@dataclass
class WriteOptions:
    out: Path
    formats: str = "mixed"  # jsonl | csv | parquet | mixed
    rows_per_file: int = 5000
    messy_share: float = 0.3
    error_rate: float = 0.002
    seed: int = 1646


@dataclass
class Sink:
    opts: WriteOptions
    manifest: list[dict[str, Any]] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.opts.seed + 99)

    def _fmt(self) -> str:
        return self.rng.choice(FORMATS) if self.opts.formats == "mixed" else self.opts.formats

    def write_partition(self, scenario: str, name: str, day: str, records: list[Labelled]) -> None:
        if name == "email":
            return self._write_email(scenario, day, records)
        ds = DATASETS[name]
        records.sort(key=lambda r: r.at)
        for part, i in enumerate(range(0, len(records), self.opts.rows_per_file)):
            chunk = records[i : i + self.opts.rows_per_file]
            fmt = self._fmt()
            flat = fmt != "jsonl"
            dialect = "canonical" if self.rng.random() >= self.opts.messy_share else DIALECTS[name].name
            rel = Path("data") / scenario / name / f"date={day}" / f"part-{part:04d}.{fmt}"
            rows: list[dict[str, Any]] = []
            injected = False
            for lab in chunk:
                base = flatten(ds, lab.event) if flat else json.loads(lab.event.model_dump_json())
                variants = [("none", base)]
                if scenario == "normal" and self.rng.random() < self.opts.error_rate:
                    kind, bad = inject_error(self.rng, name, base, flat)
                    variants = (
                        [("duplicate_record", base), ("duplicate_record", base)]
                        if kind == "duplicate_record"
                        else [(kind, bad)]
                    )
                    injected = True
                for idx, (kind, rec) in enumerate(variants):
                    if dialect != "canonical":
                        rec = to_dialect_flat(name, rec) if flat else to_dialect_nested(name, rec)
                    if kind != "none" and not (kind == "duplicate_record" and idx == 0):
                        self.errors.append(
                            {
                                "file": rel.as_posix(),
                                "row": len(rows),
                                "dataset": name,
                                "record_key": lab.key,
                                "error_type": kind,
                            }
                        )
                    rows.append(rec)
                self.labels.append(
                    {
                        "dataset": name,
                        "record_key": lab.key,
                        "scenario": lab.scenario,
                        "expected_tag": lab.tag,
                        "clauses": ";".join(lab.clauses),
                        "typology": lab.typology,
                        "subjects": ";".join(lab.subjects),
                        "file": rel.as_posix(),
                        "note": lab.note,
                    }
                )
            path = self.opts.out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, fmt, name, rows, typed=(dialect == "canonical" and not injected and flat))
            self.manifest.append(
                {
                    "path": rel.as_posix(),
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                    "rows": len(rows),
                    "format": fmt,
                    "dialect": dialect,
                    "dataset": name,
                    "scenario": scenario,
                    "date": day,
                    "synthetic": True,
                }
            )

    def _write_email(self, scenario: str, day: str, records: list[Labelled]) -> None:
        """Email is JSON only: one JSON document per line, as the journal stream carries it."""
        records.sort(key=lambda r: r.at)
        for part, i in enumerate(range(0, len(records), self.opts.rows_per_file)):
            chunk = records[i : i + self.opts.rows_per_file]
            dialect = "canonical" if self.rng.random() >= self.opts.messy_share else "graph_export"
            rel = Path("data") / scenario / "email" / f"date={day}" / f"part-{part:04d}.jsonl"
            rows: list[dict[str, Any]] = []
            for lab in chunk:
                base = json.loads(lab.event.model_dump_json())
                variants = [("none", base)]
                if scenario == "normal" and self.rng.random() < self.opts.error_rate:
                    kind, bad = inject_email_error(self.rng, base)
                    variants = [(kind, base), (kind, base)] if kind == "duplicate_record" else [(kind, bad)]
                for idx, (kind, rec) in enumerate(variants):
                    if dialect == "graph_export":
                        rec = to_graph(rec) if "sender" in rec else rec
                    if kind != "none" and not (kind == "duplicate_record" and idx == 0):
                        self.errors.append(
                            {
                                "file": rel.as_posix(),
                                "row": len(rows),
                                "dataset": "email",
                                "record_key": lab.key,
                                "error_type": kind,
                            }
                        )
                    rows.append(rec)
                self.labels.append(
                    {
                        "dataset": "email",
                        "record_key": lab.key,
                        "scenario": lab.scenario,
                        "expected_tag": lab.tag,
                        "clauses": ";".join(lab.clauses),
                        "typology": lab.typology,
                        "subjects": ";".join(lab.subjects),
                        "file": rel.as_posix(),
                        "note": lab.note,
                    }
                )
            path = self.opts.out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, "jsonl", "email", rows, typed=False)
            self.manifest.append(
                {
                    "path": rel.as_posix(),
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                    "rows": len(rows),
                    "format": "jsonl",
                    "dialect": dialect,
                    "dataset": "email",
                    "scenario": scenario,
                    "date": day,
                    "synthetic": True,
                }
            )

    def _write(self, path: Path, fmt: str, name: str, rows: list[dict[str, Any]], typed: bool) -> None:
        match fmt:
            case "jsonl":
                with path.open("w") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, ensure_ascii=False, default=_json_default) + "\n")
            case "csv":
                cols = _union_keys(rows)
                with path.open("w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=cols)
                    w.writeheader()
                    w.writerows({k: csv_value(r.get(k)) for k in cols} for r in rows)
            case "parquet":
                if typed:
                    table = to_arrow_table(DATASETS[name], rows)
                else:  # source-system export: loosely typed, timestamps as text
                    cols = _union_keys(rows)
                    table = pa.Table.from_pylist([{k: _loose(r.get(k)) for k in cols} for r in rows])
                pq.write_table(table, path, compression="zstd")


def _union_keys(rows: Iterable[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(k for r in rows for k in r))


def _loose(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat().replace("+00:00", "Z")
    if isinstance(v, bool) or v is None:
        return v
    return str(v)


def _json_default(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat().replace("+00:00", "Z")
    return str(v)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
