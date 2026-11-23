"""Record contracts enforce the domain rules."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from uniondeb.core.models import (
    CallEvent,
    CallStatus,
    ComputerDevice,
    LandlineDevice,
    MessageChannel,
    MessageEvent,
    MobileDevice,
    Network,
)

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
MOB = MobileDevice(
    owner="Siân", imei="490154203237518", iccid="89014103211118510720", imsi="001010000000001", msisdn="+447700900001"
)
LAND = LandlineDevice(owner="Dafydd", number="+442920180001")
PC = ComputerDevice(owner="Rhys", ip="192.0.2.10")


def test_computer_call_must_be_voip() -> None:
    with pytest.raises(ValidationError):
        CallEvent(
            call_id="c1",
            caller=PC,
            callee=MOB,
            started_at=NOW,
            duration_s=60,
            status=CallStatus.ANSWERED,
            network=Network.PSTN,
        )


def test_missed_call_has_no_duration() -> None:
    with pytest.raises(ValidationError):
        CallEvent(
            call_id="c2",
            caller=MOB,
            callee=LAND,
            started_at=NOW,
            duration_s=12,
            status=CallStatus.MISSED,
            network=Network.PSTN,
        )


def test_landline_text_rules() -> None:
    ok = MessageEvent(
        msg_id="m1",
        sender=MOB,
        recipient=LAND,
        channel=MessageChannel.TEXT_TO_LANDLINE,
        body_chars=20,
        sent_at=NOW,
        delivered=True,
    )
    assert ok.channel == "TEXT_TO_LANDLINE"
    with pytest.raises(ValidationError):  # landlines can't send texts
        MessageEvent(
            msg_id="m2",
            sender=LAND,
            recipient=MOB,
            channel=MessageChannel.SMS,
            body_chars=20,
            sent_at=NOW,
            delivered=True,
        )
    with pytest.raises(ValidationError):  # SMS to a landline must be TEXT_TO_LANDLINE
        MessageEvent(
            msg_id="m3",
            sender=MOB,
            recipient=LAND,
            channel=MessageChannel.SMS,
            body_chars=20,
            sent_at=NOW,
            delivered=True,
        )
