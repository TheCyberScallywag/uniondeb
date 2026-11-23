"""Generated flat rows with renamed columns resolve back to the same events."""

import random
from datetime import UTC, datetime

import pytest

from uniondeb.core.resolve import ResolutionReport
from uniondeb.core.schemas import DATASETS, flatten, unflatten
from uniondeb.synth.directory import Directory
from uniondeb.synth.registry import REGISTRY

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
MOBILE = {
    "owner": "Siân",
    "imei": "490154203237518",
    "iccid": "89014103211118510720",
    "imsi": "001010000000001",
    "msisdn": "+447700900001",
}


# ───────────────────────────── key variants ─────────────────────────────────────────


# ───────────────────────────── value variants ───────────────────────────────────────


# ───────────────────────────── flat rows (CSV / Parquet) ────────────────────────────
RENAMES = {
    "calls": {
        "call_id": "CDR ID",
        "started_at": "startTime",
        "duration_s": "billsec",
        "status": "Disposition",
        "network": "RAT",
        "caller_imei": "A-Party IMEI",
        "caller_iccid": "aPartySimSerial",
        "caller_msisdn": "from.mobile_number",
        "callee_number": "B Party Line Number",
        "callee_ip": "to_ip_address",
        "caller_owner": "Calling Party Subscriber Name",
        "callee_type": "destination-kind",
    },
    "messages": {
        "msg_id": "MessageId",
        "sender_imsi": "Originator Subscriber ID",
        "recipient_owner": "receiver.user",
        "channel": "msg_type",
        "delivered": "dlr_status",
        "sender_ip": "source_client_ip",
    },
    "handoff": {
        "conversation_id": "Thread ID",
        "seq": "Step No",
        "at": "event_time",
        "device_imei": "Handset IMEI",
        "device_type": "Terminal Type",
        "device_number": "endpoint.dn",
    },
}


@pytest.mark.parametrize("name", list(DATASETS))
def test_flat_rows_with_renamed_columns_round_trip(name: str) -> None:
    ds = DATASETS[name]
    uc = REGISTRY[{"calls": "calls", "messages": "sms", "handoff": "handoff"}[name]]
    for event in uc.generate(random.Random(11), Directory(), 40, NOW):
        row = flatten(ds, event)
        renamed = {RENAMES[name].get(k, k): v for k, v in row.items()}
        assert unflatten(ds, renamed) == event


def test_flat_report_shows_where_columns_went() -> None:
    ds = DATASETS["calls"]
    event = REGISTRY["calls"].generate(random.Random(3), Directory(), 1, NOW)[0]
    renamed = {RENAMES["calls"].get(k, k): v for k, v in flatten(ds, event).items()} | {"Trunk Group": "T7"}
    report = ResolutionReport()
    unflatten(ds, renamed, report)
    s = report.summary()
    assert any("'CDR ID' → call_id" in x for x in s["keys_resolved"])
    assert s["unknown_keys"] == ["CallEvent: 'Trunk Group'"]


# ───────────────────────────── identifiers ──────────────────────────────────────────
