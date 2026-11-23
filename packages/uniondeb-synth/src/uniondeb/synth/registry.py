"""Quick, uniform generators per dataset (calls, sms, handoff): handy for tests and schema samples.
The realistic roster-driven traffic and scenarios live in ``uniondeb.synth.scenarios``.
"""

import random
import uuid
from datetime import datetime, timedelta

from uniondeb.core.models import (
    CallEvent,
    CallStatus,
    DeviceType,
    HandoffStep,
    MessageChannel,
    MessageEvent,
    Network,
    TelcoEvent,
)

from .directory import Directory
from .journeys import JOURNEYS

_STATUS = (CallStatus.ANSWERED, CallStatus.MISSED, CallStatus.VOICEMAIL, CallStatus.DROPPED)
_MOBILE_CHANNELS = (MessageChannel.SMS, MessageChannel.RCS, MessageChannel.MMS)


def _network(a: DeviceType, b: DeviceType) -> Network:
    if DeviceType.COMPUTER in (a, b):
        return Network.VOIP
    if a == b == DeviceType.MOBILE:
        return Network.VOLTE
    return Network.PSTN


class Calls:
    key = "calls"

    def generate(self, rng: random.Random, directory: Directory, count: int, now: datetime) -> list[TelcoEvent]:
        events: list[TelcoEvent] = []
        for _ in range(count):
            caller = directory.pick(rng)
            callee = directory.pick(rng, exclude=caller)
            net = _network(caller.type, callee.type)
            drop_bias = 0.14 if net == Network.VOLTE else 0.05
            status = rng.choices(_STATUS, weights=(0.70, 0.12, 0.10, drop_bias))[0]
            match status:
                case CallStatus.ANSWERED:
                    duration = max(5, min(5400, int(rng.lognormvariate(4.6, 0.9))))
                case CallStatus.DROPPED:
                    duration = rng.randint(3, 240)
                case CallStatus.VOICEMAIL:
                    duration = rng.randint(5, 90)
                case _:
                    duration = 0
            events.append(
                CallEvent(
                    call_id=f"call-{uuid.uuid7()}",  # Python 3.14: time-ordered UUIDv7
                    caller=caller,
                    callee=callee,
                    started_at=now - timedelta(seconds=rng.randint(0, 86_400)),
                    duration_s=duration,
                    status=status,
                    network=net,
                )
            )
        return events


class Messages:
    key = "sms"

    def generate(self, rng: random.Random, directory: Directory, count: int, now: datetime) -> list[TelcoEvent]:
        events: list[TelcoEvent] = []
        for _ in range(count):
            sender = directory.pick(rng, DeviceType.MOBILE, DeviceType.COMPUTER)
            recipient = directory.pick(rng, exclude=sender)
            if recipient.type == DeviceType.LANDLINE:
                channel = MessageChannel.TEXT_TO_LANDLINE
            elif sender.type == DeviceType.COMPUTER:
                channel = MessageChannel.WEB
            else:
                channel = rng.choices(_MOBILE_CHANNELS, weights=(0.55, 0.30, 0.15))[0]
            body = (
                rng.randint(4, 160)
                if channel in (MessageChannel.SMS, MessageChannel.TEXT_TO_LANDLINE)
                else rng.randint(10, 900)
            )
            delivered = rng.random() < (0.85 if channel == MessageChannel.TEXT_TO_LANDLINE else 0.97)
            events.append(
                MessageEvent(
                    msg_id=f"msg-{uuid.uuid7()}",
                    sender=sender,
                    recipient=recipient,
                    channel=channel,
                    body_chars=body,
                    sent_at=now - timedelta(seconds=rng.randint(0, 86_400)),
                    delivered=delivered,
                )
            )
        return events


class Handoff:
    key = "handoff"

    def generate(self, rng: random.Random, directory: Directory, count: int, now: datetime) -> list[TelcoEvent]:
        events: list[TelcoEvent] = []
        for _ in range(count):
            name, template = rng.choice(list(JOURNEYS.items()))
            needed = {t for t, _, _ in template}
            owners = [o for o, devs in directory.by_owner.items() if needed <= {d.type for d in devs}]
            owner = rng.choice(owners)
            devices = {d.type: d for d in directory.by_owner[owner]}
            conv_id = f"conv-{name}-{uuid.uuid7()}"
            at = now - timedelta(minutes=rng.randint(10, 24 * 60))
            for seq, (dtype, channel, action) in enumerate(template, start=1):
                at += timedelta(seconds=rng.randint(20, 1800))
                events.append(
                    HandoffStep(
                        conversation_id=conv_id,
                        seq=seq,
                        device=devices[dtype],
                        channel=channel,
                        action=action,
                        at=at,
                    )
                )
        return events


REGISTRY = {g.key: g for g in (Calls(), Messages(), Handoff())}
