"""Property-name and value variations resolve to the canonical definitions."""

import random
from datetime import UTC, datetime

import pytest
from pydantic import Field, ValidationError

from uniondeb.core.identifiers import gen_iccid, gen_imei, luhn_valid
from uniondeb.core.models import (
    CallEvent,
    ComputerDevice,
    HandoffStep,
    LandlineDevice,
    MessageEvent,
    MobileDevice,
    device_tag,
)
from uniondeb.core.resolve import ResolutionReport, Resolving, aka, normalize, spellings
from uniondeb.core.schemas import DATASETS, unflatten

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
MOBILE = {
    "owner": "Siân",
    "imei": "490154203237518",
    "iccid": "89014103211118510720",
    "imsi": "001010000000001",
    "msisdn": "+447700900001",
}


# ───────────────────────────── key variants ─────────────────────────────────────────
def test_spellings_cover_common_cases() -> None:
    assert spellings("sim_serial") == ["sim_serial", "simSerial", "SimSerial", "SIM_SERIAL", "sim-serial"]
    assert normalize("Caller I.M.E.I") == "callerimei"
    assert normalize("Siân") == "sian"


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("IMEI", "imei"),
        ("imeiNumber", "imei"),
        ("Handset ID", "imei"),
        ("equipment-id", "imei"),
        ("SIM_SERIAL", "iccid"),
        ("simNumber", "iccid"),
        ("ICCID", "iccid"),
        ("SubscriberId", "imsi"),
        ("imsi_number", "imsi"),
        ("MobileNumber", "msisdn"),
        ("phone_number", "msisdn"),
        ("TEL", "msisdn"),
        ("Subscriber Name", "owner"),
        ("account-holder", "owner"),
    ],
)
def test_mobile_key_variants(key: str, field: str) -> None:
    data = {k: v for k, v in MOBILE.items() if k != field} | {key: MOBILE[field]}
    assert getattr(MobileDevice.model_validate(data), field) == MOBILE[field]


def test_every_declared_alias_is_accepted_by_plain_pydantic() -> None:
    """Layer 1 alone: each AliasChoices spelling works without the normalising layer."""
    choices = MobileDevice.model_fields["imei"].validation_alias.choices
    assert {"imei", "IMEI", "handsetId", "HandsetId", "handset-id", "EQUIPMENT_ID"} <= set(choices)
    for alias in choices:
        data = {k: v for k, v in MOBILE.items() if k != "imei"} | {alias: MOBILE["imei"]}
        assert MobileDevice.model_validate(data).imei == MOBILE["imei"]


def test_same_variant_means_different_things_on_different_devices() -> None:
    """'phone_number' is the MSISDN on a mobile and the line number on a landline."""
    assert LandlineDevice.model_validate({"owner": "Huw", "phone_number": "029 2018 0123"}).number == "+442920180123"
    assert (
        MobileDevice.model_validate({**MOBILE, "phone_number": "07700 900002"} | {"msisdn": "07700 900002"}).msisdn
        == "+447700900002"
    )


def test_conflicting_variants_are_rejected_but_duplicates_are_fine() -> None:
    with pytest.raises(ValidationError, match="conflicting values for 'imei'"):
        MobileDevice.model_validate({**MOBILE, "IMEI": "357805023984942"})
    assert MobileDevice.model_validate({**MOBILE, "Handset ID": MOBILE["imei"]}).imei == MOBILE["imei"]


def test_ambiguous_aliases_are_refused_when_the_model_is_defined() -> None:
    with pytest.raises(TypeError, match="ambiguous"):

        class Bad(Resolving):
            a: str = Field(validation_alias=aka("a", "shared_name"))
            b: str = Field(validation_alias=aka("b", "Shared-Name"))


# ───────────────────────────── value variants ───────────────────────────────────────
def test_value_format_variants() -> None:
    d = MobileDevice.model_validate(
        {
            "owner": "Nia",
            "imei": "49-015420-323751-8",
            "iccid": "8901 4103 2111 1851 0720",
            "imsi": "001010000000001",
            "msisdn": "0044 7700 900003",
        }
    )
    assert (d.imei, d.iccid, d.msisdn) == ("490154203237518", "89014103211118510720", "+447700900003")
    pc = ComputerDevice.model_validate({"owner": "Iolo", "IP Address": "2001:db8::1", "mac": "0200.5E10.0001"})
    assert (str(pc.ip), pc.mac) == ("2001:db8::1", "02:00:5e:10:00:01")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"kind": "cell", **MOBILE}, "mobile"),
        ({"device_type": "PSTN", "owner": "x", "number": "02920180001"}, "landline"),
        ({"Terminal Type": "laptop", "owner": "x", "ip": "192.0.2.1"}, "computer"),
    ],
)
def test_device_type_value_synonyms(raw: dict, expected: str) -> None:
    assert device_tag(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"owner": "x", "handset_id": "490154203237518"}, "mobile"),
        ({"owner": "x", "ipv4": "192.0.2.1"}, "computer"),
        ({"owner": "x", "phone_number": "07700 900123"}, "mobile"),
        ({"owner": "x", "phone_number": "(01632) 960123"}, "landline"),
        ({"owner": "x", "line_number": "07700900123"}, "landline"),  # landline-only key wins over the prefix
        ({"owner": "x"}, None),
    ],
)
def test_device_type_inference(raw: dict, expected: str | None) -> None:
    assert device_tag(raw) == expected


def test_unresolvable_device_gives_a_clear_error() -> None:
    with pytest.raises(ValidationError, match="could not tell whether the device"):
        HandoffStep.model_validate(
            {"conv_id": "c", "step": 1, "endpoint": {"owner": "x"}, "channel": "SMS", "event": "e", "ts": NOW}
        )


def test_message_with_smpp_style_names_and_values() -> None:
    report = ResolutionReport()
    ev = MessageEvent.model_validate(
        {
            "message_id": "msg-1",
            "Originator": MOBILE,
            "destination": {"owner": "Huw", "tel": "029 2018 0001"},
            "msg_type": "tts",
            "Message Length": "42",
            "submit_date": "2026-11-19T09:00:00Z",
            "stat": "DELIVRD",
            "esm_class": 4,
        },
        context={"report": report},
    )
    assert (ev.channel, ev.delivered, ev.body_chars, ev.recipient.type) == ("TEXT_TO_LANDLINE", True, 42, "landline")
    s = report.summary()
    assert "MessageEvent: 'stat' → delivered" in s["keys_resolved"]
    assert "MessageEvent.delivered: 'DELIVRD' → True" in s["values_resolved"]
    assert s["unknown_keys"] == ["MessageEvent: 'esm_class'"]


def test_call_with_q850_cause_code() -> None:
    ev = CallEvent.model_validate(
        {
            "cdr_id": "c",
            "a_party": MOBILE,
            "b_party": {"owner": "y", "tel": "02920180009"},
            "setup_time": NOW,
            "billsec": 0,
            "cause": 19,
            "bearer": "isdn",
        }
    )
    assert (ev.status, ev.network) == ("missed", "PSTN")


# ───────────────────────────── flat rows (CSV / Parquet) ────────────────────────────


# ───────────────────────────── identifiers ──────────────────────────────────────────
def test_luhn_and_generators() -> None:
    assert luhn_valid("490154203237518") and luhn_valid("89014103211118510720")
    assert not luhn_valid("490154203237519")
    rng = random.Random(0)
    assert all(luhn_valid(gen_imei(rng)) and luhn_valid(gen_iccid(rng)) for _ in range(200))


def test_contextual_flat_column_follows_the_device_type() -> None:
    """'B Party Phone Number' is an MSISDN for a mobile and a line number for a landline."""
    ds = DATASETS["calls"]
    base = {
        "cdr_id": "c",
        "start_time": NOW,
        "duration": 5,
        "status": "answered",
        "network": "VoLTE",
        "A-Party IMEI": MOBILE["imei"],
        "a_party_sim_serial": MOBILE["iccid"],
        "a_party_imsi": MOBILE["imsi"],
        "a_party_msisdn": MOBILE["msisdn"],
        "a_party_user": "Siân",
        "b_party_user": "Gareth",
    }
    to_mobile = unflatten(
        ds,
        base
        | {
            "B Party Phone Number": "07700 900222",
            "b_party_imei": "357805023984942",
            "b_party_iccid": "89014103211118510720",
            "b_party_imsi": "001010000000002",
        },
    )
    to_landline = unflatten(ds, base | {"B Party Phone Number": "029 2018 0222", "network": "PSTN"})
    assert (to_mobile.callee.type, to_mobile.callee.msisdn) == ("mobile", "+447700900222")
    assert (to_landline.callee.type, to_landline.callee.number) == ("landline", "+442920180222")
