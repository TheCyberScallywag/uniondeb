"""Dataset schemas (JSON, CSV Table Schema, Parquet) agree with the contracts and round-trip.

Verifies: UND-011
"""

import csv
import io
import json
from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest

from uniondeb.core.cli import main as cli
from uniondeb.core.mail import EmailEvent
from uniondeb.core.models import (
    CallEvent,
    CallStatus,
    ComputerDevice,
    HandoffStep,
    LandlineDevice,
    MessageChannel,
    MessageEvent,
    MobileDevice,
    Network,
    StepChannel,
)
from uniondeb.core.schemas import (
    DATASETS,
    csv_value,
    email_json_schema,
    flatten,
    json_schema,
    parse_csv_row,
    table_schema,
    to_arrow_table,
    unflatten,
)

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
MOB = MobileDevice(
    owner="Siân", imei="490154203237518", iccid="89014103211118510720", imsi="001010000000001", msisdn="+447700900001"
)
LAND = LandlineDevice(owner="Dafydd", number="+442920180001")
PC = ComputerDevice(owner="Rhys", ip="192.0.2.10", mac="02:00:5e:10:00:01", hostname="rhys-laptop")

EVENTS = {
    "calls": [
        CallEvent(
            call_id="call-01a1f305-5dad-71dd-b480-b6abe5ecc544",
            caller=MOB,
            callee=LAND,
            started_at=NOW,
            duration_s=61,
            status=CallStatus.ANSWERED,
            network=Network.PSTN,
            transcript="Rota, Saturday",
        ),
        CallEvent(
            call_id="call-01a1f305-5dad-71dd-b480-b6abe5ecc545",
            caller=PC,
            callee=MOB,
            started_at=NOW,
            duration_s=0,
            status=CallStatus.MISSED,
            network=Network.VOIP,
        ),
    ],
    "messages": [
        MessageEvent(
            msg_id="msg-01a1f305-5dad-71dd-b480-b6abe5ecc546",
            sender=PC,
            recipient=LAND,
            channel=MessageChannel.TEXT_TO_LANDLINE,
            body_chars=9,
            sent_at=NOW,
            delivered=True,
            body="Ffonia fi",
        )
    ],
    "handoff": [
        HandoffStep(
            conversation_id="conv-text-then-call-01a1f305-5dad-71dd-b480-b6abe5ecc547",
            seq=1,
            device=MOB,
            channel=StepChannel.SMS,
            action="free to talk?",
            at=NOW,
        )
    ],
}


@pytest.mark.parametrize("name", list(DATASETS))
def test_csv_and_parquet_round_trip(name: str, tmp_path) -> None:
    ds, events = DATASETS[name], EVENTS[name]
    rows = [flatten(ds, e) for e in events]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[f["name"] for f in table_schema(ds)["fields"]])
    w.writeheader()
    w.writerows({k: csv_value(v) for k, v in r.items()} for r in rows)
    assert [unflatten(ds, parse_csv_row(ds, r)) for r in csv.DictReader(io.StringIO(buf.getvalue()))] == events
    pq.write_table(to_arrow_table(ds, rows), tmp_path / "t.parquet")
    assert [unflatten(ds, r) for r in pq.read_table(tmp_path / "t.parquet").to_pylist()] == events


@pytest.mark.parametrize("name", list(DATASETS))
def test_json_schema_accepts_events_and_rejects_a_mobile_without_imei(name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json_schema(DATASETS[name]))
    docs = [json.loads(e.model_dump_json()) for e in EVENTS[name]]
    assert all(validator.is_valid(d) for d in docs)
    role = DATASETS[name].roles[0]
    bad = json.loads(json.dumps(docs[0]))
    bad[role] = {**bad[role], "type": "mobile"}
    bad[role].pop("imei", None)
    assert not validator.is_valid(bad)


def test_email_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    e = EmailEvent.model_validate(
        {
            "message_id": "<m1@cyberscallywags.example>",
            "sender": "nia@cyberscallywags.example",
            "to": "huw@example.org",
            "sent_at": NOW,
            "journal": {"source": "m365", "received_at": NOW},
        }
    )
    assert jsonschema.Draft202012Validator(email_json_schema()).is_valid(json.loads(e.model_dump_json()))


def test_cli_exports_schemas(tmp_path) -> None:
    assert cli(["schemas", "export", "--out", str(tmp_path)]) == 0
    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert {
        "json/calls.schema.json",
        "csv/messages.tableschema.json",
        "parquet/handoff.arrow-schema.txt",
        "json/email.schema.json",
    } <= files
