"""Dataset schemas for the three implementations, in three formats, from one source.

* **JSON**: the Kafka wire format. A nested event with the device as a discriminated
  union on ``type``, published as JSON Schema 2020-12 straight from the Pydantic models.
* **CSV**: one flat row per event, described by a Frictionless Table Schema
  (``*.tableschema.json``). Device identifiers become prefixed columns that are empty
  when they don't apply to that device type.
* **Parquet**: the same flat columns with real types (UTC timestamps, small ints,
  dictionary-encoded enums), and descriptions stored as field metadata.

``COLUMNS`` below is the single column spec that the CSV and Parquet schemas and the
flatten/unflatten functions are all generated from.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from . import models as m
from .models import (
    CallEvent,
    CallStatus,
    ComputerDevice,
    DeviceType,
    HandoffStep,
    LandlineDevice,
    MessageChannel,
    MessageEvent,
    MobileDevice,
    Network,
    StepChannel,
)
from .resolve import SYNONYMS, ResolutionReport, _choices, normalize

SCHEMA_VERSION = "1.0.0"
type Kind = Literal["string", "integer", "boolean", "datetime"]
type Width = Literal[8, 16, 32, 64]


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    kind: Kind
    description: str
    required: bool = True
    enum: tuple[str, ...] | None = None
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    bits: Width = 32
    applies_to: tuple[str, ...] | None = None  # device types this identifier column is filled for


# ───────────────────────────── device identity columns ───────────────────────────────
M, L, C = DeviceType.MOBILE.value, DeviceType.LANDLINE.value, DeviceType.COMPUTER.value

# (field on the device model, kind, description, applies to, pattern)
DEVICE_FIELDS: tuple[tuple[str, str, tuple[str, ...] | None, str | None], ...] = (
    ("id", "Stable device key: imei:<IMEI> | tel:<number> | ip:<address>", None, r"^(imei|tel|ip):.+$"),
    ("type", "Device type; decides which identifier columns are filled", None, None),
    ("owner", "Person who owns the device", None, None),
    ("address", "How other parties reach it: tel:<E.164> or sip:<ip>", None, r"^(tel|sip):.+$"),
    ("imei", "Handset IMEI: 15 digits, Luhn check digit", (M,), r"^\d{15}$"),
    ("iccid", "SIM card serial (ICCID): 19-20 digits, starts 89, Luhn", (M,), r"^89\d{17,18}$"),
    ("imsi", "Subscriber identity (IMSI): MCC + MNC + MSIN, 15 digits", (M,), r"^\d{15}$"),
    ("msisdn", "Mobile number, E.164 (+447…)", (M,), r"^\+447\d{9}$"),
    ("number", "Landline number, E.164 (+441… / +442…)", (L,), r"^\+44[12]\d{8,9}$"),
    ("ip", "IPv4 or IPv6 address", (C,), None),
    ("mac", "MAC address (lowercase, colon separated)", (C,), r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$"),
    ("hostname", "Computer hostname", (C,), None),
)


def device_columns(prefix: str, role: str) -> list[Column]:
    cols: list[Column] = []
    for name, desc, applies, pattern in DEVICE_FIELDS:
        enum = tuple(t.value for t in DeviceType) if name == "type" else None
        when = f" Filled only when {prefix}_type is {' / '.join(applies)}; empty otherwise." if applies else ""
        cols.append(
            Column(
                name=f"{prefix}_{name}",
                kind="string",
                description=f"{role}: {desc}.{when}",
                required=applies is None,
                enum=enum,
                pattern=pattern,
                applies_to=applies,
            )
        )
    return cols


def _enum(e) -> tuple[str, ...]:
    return tuple(m.value for m in e)


# ───────────────────────────── the three datasets ───────────────────────────────────
COLUMNS: dict[str, list[Column]] = {
    "calls": [
        Column("call_id", "string", "Call detail record id (UUIDv7, time ordered)", pattern=r"^call-[0-9a-f-]{36}$"),
        Column("started_at", "datetime", "Call start, UTC"),
        Column("duration_s", "integer", "Talk time in seconds (0 for missed)", minimum=0, maximum=21600),
        Column("status", "string", "Call outcome", enum=_enum(CallStatus)),
        Column("network", "string", "PSTN = fixed, VoLTE = mobile voice, VoIP = any computer leg", enum=_enum(Network)),
        Column(
            "route",
            "string",
            "<caller_type>-><callee_type>",
            pattern=r"^(mobile|landline|computer)->(mobile|landline|computer)$",
        ),
        Column(
            "transcript",
            "string",
            "Call transcript or notes (free text; scanned by the tagging agents)",
            required=False,
        ),
        *device_columns("caller", "Calling device"),
        *device_columns("callee", "Called device"),
    ],
    "messages": [
        Column("msg_id", "string", "Message id (UUIDv7)", pattern=r"^msg-[0-9a-f-]{36}$"),
        Column("sent_at", "datetime", "Send time, UTC"),
        Column(
            "channel",
            "string",
            "SMS/MMS/RCS from mobile, WEB from computer, TEXT_TO_LANDLINE read out by TTS",
            enum=_enum(MessageChannel),
        ),
        Column("body_chars", "integer", "Message length in characters", minimum=1, maximum=1600, bits=16),
        Column("delivered", "boolean", "Delivery receipt received"),
        Column(
            "body",
            "string",
            "Message text (free text; scanned by the tagging agents). body_chars = its length",
            required=False,
        ),
        *device_columns("sender", "Sending device"),
        *device_columns("recipient", "Receiving device"),
    ],
    "handoff": [
        Column(
            "conversation_id",
            "string",
            "Conversation id (journey name + UUIDv7)",
            pattern=r"^conv-[a-z-]+-[0-9a-f-]{36}$",
        ),
        Column("seq", "integer", "Step number within the conversation, from 1", minimum=1, maximum=1000, bits=16),
        Column("channel", "string", "Channel used for this step", enum=_enum(StepChannel)),
        Column("action", "string", "What happened at this step"),
        Column("at", "datetime", "Step time, UTC"),
        Column("content", "string", "Free-text note for the step (scanned by the tagging agents)", required=False),
        *device_columns("device", "Device used for this step"),
    ],
}

MODELS: dict[str, type[BaseModel]] = {"calls": CallEvent, "messages": MessageEvent, "handoff": HandoffStep}
PRIMARY_KEYS: dict[str, list[str]] = {
    "calls": ["call_id"],
    "messages": ["msg_id"],
    "handoff": ["conversation_id", "seq"],
}
DEVICE_ROLES: dict[str, tuple[str, ...]] = {
    "calls": ("caller", "callee"),
    "messages": ("sender", "recipient"),
    "handoff": ("device",),
}


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    model: type[BaseModel]
    columns: list[Column] = field(repr=False)
    primary_key: list[str]
    roles: tuple[str, ...]


DATASETS: dict[str, Dataset] = {k: Dataset(k, MODELS[k], COLUMNS[k], PRIMARY_KEYS[k], DEVICE_ROLES[k]) for k in COLUMNS}
_DEVICE_MODELS = (MobileDevice, LandlineDevice, ComputerDevice)
_DERIVED = {"route"}  # flat-only convenience columns, recomputed from the devices


# ───────────────────────────── flatten / unflatten ───────────────────────────────────
def flatten(ds: Dataset, event: BaseModel) -> dict[str, Any]:
    """Nested event → one flat row (Python types: datetime, int, bool, str, None)."""
    nested = event.model_dump(mode="python")
    row: dict[str, Any] = {}
    for col in ds.columns:
        role, _, fld = col.name.partition("_")
        if role in ds.roles:
            v = nested[role].get(fld)
            row[col.name] = None if v is None else str(v)
        elif col.name == "route":
            row[col.name] = f"{nested['caller']['type']}->{nested['callee']['type']}"
        else:
            row[col.name] = nested[col.name]
    return row


def _role_prefixes(ds: Dataset) -> list[tuple[str, str]]:
    """(normalised prefix, role) for every alias of every device role, longest first."""
    pairs = {
        (normalize(alias), role)
        for role in ds.roles
        for alias in (role, *_choices(ds.model.model_fields[role].validation_alias))
    }
    return sorted(pairs, key=lambda p: -len(p[0]))


def unflatten(ds: Dataset, row: dict[str, Any], report: ResolutionReport | None = None) -> BaseModel:
    """Flat row (CSV/Parquet) → validated nested event, whatever the column naming.

    ``caller_imei``, ``callerImei``, ``A-Party IMEI``, ``from.handset_id`` and
    ``a_party_equipment_id`` all land on ``caller.imei``: the column is split into the
    longest matching *role* alias and a remainder that must resolve to a device field.
    Anything else is passed to the event model, which resolves its own aliases.
    """
    devices: dict[str, dict[str, Any]] = {r: {} for r in ds.roles}
    top: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "" or normalize(key) in _DERIVED:
            continue
        nk = normalize(key)
        for prefix, role in _role_prefixes(ds):
            rest = nk[len(prefix) :]
            if not (nk.startswith(prefix) and rest):
                continue
            meanings = {n for model in _DEVICE_MODELS if (n := model._key_lookup.get(rest))}
            if meanings or rest in ("id", "address"):
                # Pass the *variant* on: contextual names like 'phone_number' are only decided
                # once the device type is known (msisdn on a mobile, number on a landline).
                devices[role][rest] = value
                if report is not None and meanings and key != f"{role}_{next(iter(meanings))}":
                    target = next(iter(meanings)) if len(meanings) == 1 else "|".join(sorted(meanings))
                    report.keys.append((f"{ds.name} (flat)", str(key), f"{role}.{target}"))
                break
        else:
            top[key] = value
    ctx = {"report": report} if report is not None else None
    return ds.model.model_validate({**top, **devices}, context=ctx)


# ───────────────────────────── alias catalogue ──────────────────────────────────────
VALUE_SYNONYMS: dict[str, dict[str, Any]] = {
    "type": {k.value: list(v) for k, v in m.DEVICE_TYPE_SYNONYMS.items()},
    "status": {k.value: list(v) for k, v in m.CALL_STATUS_SYNONYMS.items()},
    "network": {k.value: list(v) for k, v in m.NETWORK_SYNONYMS.items()},
    "delivered": {"true": list(m.DELIVERED_TRUE), "false": list(m.DELIVERED_FALSE)},
}
CHANNEL_SYNONYMS = {
    "messages": {k.value: list(v) for k, v in m.MESSAGE_CHANNEL_SYNONYMS.items()},
    "handoff": {k.value: list(v) for k, v in m.STEP_CHANNEL_SYNONYMS.items()},
}


def alias_catalogue(ds: Dataset) -> dict[str, Any]:
    """What variants each canonical name accepts, for docs and for non-Python consumers."""
    top = [n for n in ds.model.model_fields if n not in ds.roles]
    values = {k: v for k, v in VALUE_SYNONYMS.items() if k in ds.model.model_fields}
    if ds.name in CHANNEL_SYNONYMS:
        values["channel"] = CHANNEL_SYNONYMS[ds.name]
    values["<role>_type"] = VALUE_SYNONYMS["type"]
    return {
        "matching": "case-, accent- and punctuation-insensitive; snake/camel/Pascal/UPPER/kebab all accepted",
        "fields": {n: list(SYNONYMS.get(n, ())) for n in top},
        "device_roles": {r: list(SYNONYMS.get(r, ())) for r in ds.roles},
        "device_fields": {
            n: list(SYNONYMS.get(n, ()))
            for n in ("type", "owner", "imei", "iccid", "imsi", "msisdn", "number", "ip", "mac", "hostname")
        },
        "flat_columns": "<any role alias><separator?><any device-field alias>, e.g. 'A-Party IMEI' → caller_imei",
        "values": values,
        "device_type_inference": [
            "explicit type (any alias, any value synonym) wins",
            "IMEI / IMSI / ICCID present → mobile",
            "IP / MAC / hostname present → computer",
            "only a phone number → 07…/+447… mobile, 01…/02…/+441…/+442… landline",
        ],
    }


def _column_aliases(ds: Dataset, col: Column) -> Any:
    role, _, fld = col.name.partition("_")
    if role in ds.roles:
        return {"role": list(SYNONYMS.get(role, ())), "field": list(SYNONYMS.get(fld, ()))}
    return list(SYNONYMS.get(col.name, ()))


# ───────────────────────────── JSON Schema (wire format) ────────────────────────────
def json_schema(ds: Dataset) -> dict[str, Any]:
    schema = ds.model.model_json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://uniondeb.cyberscallywags.example/schemas/{ds.name}/{SCHEMA_VERSION}/{ds.name}.schema.json",
        "title": f"uniondeb.{ds.name}",
        "description": f"{ds.model.__doc__ or ''}Kafka wire format for topic telco-{ds.name if ds.name != 'messages' else 'sms'}; "
        "device is a discriminated union on `type`.".strip(),
        "x-schema-version": SCHEMA_VERSION,
        "x-aliases": alias_catalogue(ds),
        **schema,
    }


def _journal_synonyms():
    from .mail import JOURNAL_SYNONYMS

    return JOURNAL_SYNONYMS


def email_json_schema() -> dict[str, Any]:
    """Email is JSON only (nested lists of addresses and attachments don't flatten honestly)."""
    from .mail import EmailEvent

    names = [
        "message_id",
        "sender",
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "sent_at",
        "in_reply_to",
        "attachments",
        "device",
        "journal",
        "received_at",
        "envelope_recipients",
        "address",
        "name",
        "filename",
        "content_type",
        "size",
        "sha256",
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://uniondeb.cyberscallywags.example/schemas/email/{SCHEMA_VERSION}/email.schema.json",
        "title": "uniondeb.email",
        "description": "Journaled email (Kafka topic mail-journal): every message sent from CyberScallywags UK "
        "accounts and equipment, BCC'd or envelope-journaled, normalised to JSON. "
        "Input may also use Microsoft Graph field names (see x-aliases).",
        "x-schema-version": SCHEMA_VERSION,
        "x-aliases": {
            "matching": "case-, accent- and punctuation-insensitive; snake/camel/Pascal/UPPER/kebab all accepted",
            "fields": {n: list(SYNONYMS.get(n, ())) for n in names},
            "values": {"journal.source": {k.value: list(v) for k, v in _journal_synonyms().items()}},
            "shapes": [
                "RFC 5322 address strings and lists",
                "Microsoft Graph {emailAddress: {name, address}}",
                "Graph body {contentType: html, content} → plain text",
            ],
        },
        **EmailEvent.model_json_schema(mode="serialization"),
    }


# ───────────────────────────── CSV: Frictionless Table Schema ───────────────────────
def table_schema(ds: Dataset) -> dict[str, Any]:
    fields = []
    for c in ds.columns:
        f: dict[str, Any] = {"name": c.name, "type": c.kind, "description": c.description}
        if c.kind == "datetime":
            f["format"] = "any"  # ISO 8601 with Z
        if c.kind == "boolean":
            f["trueValues"], f["falseValues"] = ["true"], ["false"]
        cons: dict[str, Any] = {}
        if c.required:
            cons["required"] = True
        if c.enum:
            cons["enum"] = list(c.enum)
        if c.pattern:
            cons["pattern"] = c.pattern
        if c.minimum is not None:
            cons["minimum"] = c.minimum
        if c.maximum is not None:
            cons["maximum"] = c.maximum
        if cons:
            f["constraints"] = cons
        if c.applies_to:
            f["x-applies-to-device-types"] = list(c.applies_to)
        if aliases := _column_aliases(ds, c):
            f["x-aliases"] = aliases
        fields.append(f)
    return {
        "$schema": "https://datapackage.org/profiles/2.0/tableschema.json",
        "name": f"uniondeb-{ds.name}",
        "version": SCHEMA_VERSION,
        "fields": fields,
        "primaryKey": ds.primary_key,
        "missingValues": [""],
    }


def csv_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if hasattr(v, "isoformat"):
        return v.isoformat().replace("+00:00", "Z")
    return str(v)


def parse_csv_row(ds: Dataset, raw: dict[str, str]) -> dict[str, Any]:
    from datetime import datetime

    out: dict[str, Any] = {}
    for c in ds.columns:
        v = raw.get(c.name, "")
        if v == "":
            out[c.name] = None
        elif c.kind == "integer":
            out[c.name] = int(v)
        elif c.kind == "boolean":
            out[c.name] = v == "true"
        elif c.kind == "datetime":
            out[c.name] = datetime.fromisoformat(v)
        else:
            out[c.name] = v
    return out


# ───────────────────────────── Parquet: Arrow schema ─────────────────────────────────
def arrow_schema(ds: Dataset):
    import pyarrow as pa

    ints = {8: pa.int8(), 16: pa.int16(), 32: pa.int32(), 64: pa.int64()}
    fields = []
    for c in ds.columns:
        match c.kind:
            case "datetime":
                t = pa.timestamp("us", tz="UTC")
            case "integer":
                t = ints[c.bits]
            case "boolean":
                t = pa.bool_()
            case _ if c.enum:
                t = pa.dictionary(pa.int8(), pa.string())  # enum → dictionary encoding
            case _:
                t = pa.string()
        meta = {"description": c.description}
        if c.enum:
            meta["enum"] = ",".join(c.enum)
        if c.pattern:
            meta["pattern"] = c.pattern
        if c.applies_to:
            meta["applies_to"] = ",".join(c.applies_to)
        if aliases := _column_aliases(ds, c):
            meta["aliases"] = json.dumps(aliases)
        fields.append(pa.field(c.name, t, nullable=not c.required, metadata=meta))
    return pa.schema(
        fields,
        metadata={
            "uniondeb.dataset": ds.name,
            "uniondeb.schema_version": SCHEMA_VERSION,
            "uniondeb.primary_key": ",".join(ds.primary_key),
            "uniondeb.aliases": json.dumps(alias_catalogue(ds)),
        },
    )


def to_arrow_table(ds: Dataset, rows: list[dict[str, Any]]):
    import pyarrow as pa

    schema = arrow_schema(ds)
    columns = {f.name: [r[f.name] for r in rows] for f in schema}
    return pa.Table.from_pydict(columns, schema=schema)
