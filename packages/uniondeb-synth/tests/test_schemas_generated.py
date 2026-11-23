"""Generated events round-trip through the JSON, CSV and Parquet schemas."""

import csv
import io
import json
import random
from datetime import UTC, datetime

import pytest

from uniondeb.core.schemas import DATASETS, csv_value, flatten, json_schema, parse_csv_row, table_schema, unflatten
from uniondeb.synth.directory import Directory
from uniondeb.synth.registry import REGISTRY

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
UC = {"calls": "calls", "messages": "sms", "handoff": "handoff"}


def _events(name: str):
    return REGISTRY[UC[name]].generate(random.Random(21), Directory(), 60, NOW)


@pytest.mark.parametrize("name", list(DATASETS))
def test_json_schema_validates_samples_and_rejects_missing_identifiers(name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    ds = DATASETS[name]
    validator = jsonschema.Draft202012Validator(json_schema(ds))
    docs = [json.loads(e.model_dump_json()) for e in _events(name)]
    assert all(validator.is_valid(d) for d in docs)
    mobile = next(d for d in docs for r in ds.roles if d[r]["type"] == "mobile")
    role = next(r for r in ds.roles if mobile[r]["type"] == "mobile")
    mobile[role].pop("imei")
    assert not validator.is_valid(mobile)


@pytest.mark.parametrize("name", list(DATASETS))
def test_csv_round_trip(name: str) -> None:
    ds = DATASETS[name]
    events = _events(name)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[f["name"] for f in table_schema(ds)["fields"]])
    w.writeheader()
    w.writerows({k: csv_value(v) for k, v in flatten(ds, e).items()} for e in events)
    back = [unflatten(ds, parse_csv_row(ds, r)) for r in csv.DictReader(io.StringIO(buf.getvalue()))]
    assert back == events


@pytest.mark.parametrize("name", list(DATASETS))
def test_parquet_round_trip_and_metadata(name: str, tmp_path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    from uniondeb.core.schemas import to_arrow_table

    ds = DATASETS[name]
    events = _events(name)
    path = tmp_path / f"{name}.parquet"
    pq.write_table(to_arrow_table(ds, [flatten(ds, e) for e in events]), path)
    table = pq.read_table(path)
    assert [unflatten(ds, r) for r in table.to_pylist()] == events
    meta = table.schema.metadata
    assert meta[b"uniondeb.dataset"].decode() == name and b"uniondeb.aliases" in meta
    imei = next(f for f in table.schema if f.name.endswith("_imei"))
    assert imei.nullable and imei.metadata[b"applies_to"] == b"mobile"
