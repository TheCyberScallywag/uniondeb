"""Communication record contracts: devices, calls, messages and cross-device hand-offs.

Every ingest path (file upload, mail journal stream) validates against these models, through
the alias layer in ``uniondeb.core.resolve``.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Discriminator, Field, IPvAnyAddress, Tag, computed_field, model_validator

from .identifiers import ICCID, IMEI, IMSI, MSISDN, LandlineNumber, MacAddress
from .resolve import Resolving, aka, boolean_of, normalize, one_of

_CONFIG = ConfigDict(
    frozen=True,
    use_enum_values=True,
    extra="ignore",
    validate_by_name=True,  # canonical names always work…
    validate_by_alias=True,  # …and so does every AliasChoices variant
)


def known_as(canonical: str, **kw: Any) -> Any:
    """Field whose input may use any synonym of ``canonical`` (see resolve.SYNONYMS)."""
    return Field(validation_alias=aka(canonical), **kw)


class DeviceType(StrEnum):
    MOBILE = "mobile"
    LANDLINE = "landline"
    COMPUTER = "computer"


DEVICE_TYPE_SYNONYMS: dict[DeviceType, tuple[str, ...]] = {
    DeviceType.MOBILE: ("cell", "cellphone", "cellular", "mobile phone", "handset", "smartphone", "gsm", "ue"),
    DeviceType.LANDLINE: ("fixed", "fixed line", "pstn", "home phone", "wired", "pots", "analogue", "isdn"),
    DeviceType.COMPUTER: ("pc", "laptop", "desktop", "softphone", "workstation", "web client", "sip client", "host"),
}
DeviceTypeValue = one_of(DeviceType, DEVICE_TYPE_SYNONYMS)


class _DeviceBase(Resolving):
    model_config = _CONFIG

    owner: Annotated[str, known_as("owner")]


class MobileDevice(_DeviceBase):
    """A handset (IMEI) carrying a SIM (ICCID) for a subscriber (IMSI) with a number (MSISDN)."""

    type: Annotated[Literal[DeviceType.MOBILE], DeviceTypeValue, known_as("type")] = DeviceType.MOBILE
    imei: Annotated[IMEI, known_as("imei")]
    iccid: Annotated[ICCID, known_as("iccid")]
    imsi: Annotated[IMSI, known_as("imsi")]
    msisdn: Annotated[MSISDN, known_as("msisdn")]

    @computed_field(description="Stable device key: the handset IMEI")
    @property
    def id(self) -> str:
        return f"imei:{self.imei}"

    @computed_field(description="How other parties reach it on calls/SMS/MMS/RCS")
    @property
    def address(self) -> str:
        return f"tel:{self.msisdn}"


class LandlineDevice(_DeviceBase):
    """A fixed line, identified only by its geographic number: no SIM, no IMEI."""

    type: Annotated[Literal[DeviceType.LANDLINE], DeviceTypeValue, known_as("type")] = DeviceType.LANDLINE
    number: Annotated[LandlineNumber, known_as("number")]

    @computed_field(description="Stable device key: the line number")
    @property
    def id(self) -> str:
        return f"tel:{self.number}"

    @computed_field(description="How other parties reach it (PSTN)")
    @property
    def address(self) -> str:
        return f"tel:{self.number}"


class ComputerDevice(_DeviceBase):
    """A desktop/laptop softphone or messaging client, identified by IP address."""

    type: Annotated[Literal[DeviceType.COMPUTER], DeviceTypeValue, known_as("type")] = DeviceType.COMPUTER
    ip: Annotated[IPvAnyAddress, known_as("ip")]
    mac: Annotated[MacAddress | None, known_as("mac")] = None
    hostname: Annotated[str | None, known_as("hostname")] = None

    @computed_field(description="Stable device key: the IP address")
    @property
    def id(self) -> str:
        return f"ip:{self.ip}"

    @computed_field(description="How other parties reach it (VoIP/SIP, web messaging)")
    @property
    def address(self) -> str:
        return f"sip:{self.ip}"


_DEVICE_MODELS = (MobileDevice, LandlineDevice, ComputerDevice)
_TYPE_TABLE = {normalize(t.value): t for t in DeviceType} | {
    normalize(v): t for t, vs in DEVICE_TYPE_SYNONYMS.items() for v in vs
}


def device_tag(value: Any) -> str | None:
    """Pick the device model, tolerating key/value variants and a missing ``type``.

    1. an explicit type under any alias (``device_type``, ``kind``…) with any value
       synonym (``cell``, ``pstn``, ``laptop``…);
    2. otherwise infer from which identifiers are present: IMEI/IMSI/ICCID → mobile,
       IP/MAC/hostname → computer;
    3. a bare phone number decides by its prefix: 07…/+447… mobile, 01…/02…/+441…/+442… landline.
    """
    if isinstance(value, _DeviceBase):
        return str(value.type)
    if not isinstance(value, dict):
        return None
    found: dict[str, Any] = {}
    for key, v in value.items():
        for model in _DEVICE_MODELS:
            if (name := model._key_lookup.get(normalize(key))) is not None:
                found.setdefault(name, v)
    if "type" in found and (t := _TYPE_TABLE.get(normalize(found["type"]))) is not None:
        return t.value
    if found.keys() & {"imei", "iccid", "imsi"}:
        return DeviceType.MOBILE.value
    if found.keys() & {"ip", "mac", "hostname"}:
        return DeviceType.COMPUTER.value
    has_m, has_l = "msisdn" in found, "number" in found
    if has_m and not has_l:
        return DeviceType.MOBILE.value
    if has_l and not has_m:
        return DeviceType.LANDLINE.value
    digits = normalize(found.get("msisdn") or found.get("number") or "")
    if digits.startswith(("07", "447", "00447")):
        return DeviceType.MOBILE.value
    if digits.startswith(("01", "02", "441", "442", "00441", "00442")):
        return DeviceType.LANDLINE.value
    return None


type Device = Annotated[
    Annotated[MobileDevice, Tag("mobile")]
    | Annotated[LandlineDevice, Tag("landline")]
    | Annotated[ComputerDevice, Tag("computer")],
    Discriminator(
        device_tag,
        custom_error_type="device_type_unresolved",
        custom_error_message="could not tell whether the device is a mobile, landline or computer",
    ),
]


class _Event(Resolving):
    model_config = _CONFIG

    def kafka_key(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


# ───────────────────────────── 1. Galwadau — phone calls ─────────────────────────────
class CallStatus(StrEnum):
    ANSWERED = "answered"
    MISSED = "missed"
    DROPPED = "dropped"
    VOICEMAIL = "voicemail"


class Network(StrEnum):
    PSTN = "PSTN"  # fixed line
    VOLTE = "VoLTE"  # mobile network voice
    VOIP = "VoIP"  # any leg on a computer


CALL_STATUS_SYNONYMS: dict[CallStatus, tuple[str, ...]] = {
    CallStatus.ANSWERED: ("connected", "completed", "success", "ok", "normal clearing", "16"),
    CallStatus.MISSED: ("no answer", "unanswered", "noanswer", "not answered", "ring no answer", "19"),
    CallStatus.DROPPED: ("failed", "lost", "abnormal", "call drop", "dropped call", "41"),
    CallStatus.VOICEMAIL: ("vm", "voice mail", "diverted to voicemail", "mailbox"),
}
NETWORK_SYNONYMS: dict[Network, tuple[str, ...]] = {
    Network.PSTN: ("fixed", "landline", "isdn", "tdm", "circuit switched"),
    Network.VOLTE: ("lte", "4g", "4g voice", "ims", "vonr", "5g voice", "mobile"),
    Network.VOIP: ("sip", "voice over ip", "internet", "ott", "webrtc"),
}


class CallEvent(_Event):
    call_id: Annotated[str, known_as("call_id")]
    caller: Annotated[Device, known_as("caller")]
    callee: Annotated[Device, known_as("callee")]
    started_at: Annotated[datetime, known_as("started_at")]
    duration_s: Annotated[int, known_as("duration_s", ge=0, le=6 * 3600)]
    status: Annotated[CallStatus, one_of(CallStatus, CALL_STATUS_SYNONYMS), known_as("status")]
    network: Annotated[Network, one_of(Network, NETWORK_SYNONYMS), known_as("network")]
    transcript: Annotated[str | None, known_as("transcript", max_length=20_000)] = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.caller.id == self.callee.id:
            raise ValueError("a device cannot call itself")
        if self.status == CallStatus.MISSED and self.duration_s:
            raise ValueError("missed calls must have duration_s == 0")
        legs = {self.caller.type, self.callee.type}
        if DeviceType.COMPUTER in legs and self.network != Network.VOIP:
            raise ValueError("calls with a computer leg must be VoIP")
        return self

    def kafka_key(self) -> str:
        return self.call_id


# ───────────────────────────── 2. Negeseuon — text messages ──────────────────────────
class MessageChannel(StrEnum):
    SMS = "SMS"
    MMS = "MMS"
    RCS = "RCS"
    WEB = "WEB"  # messages sent from a computer (web/desktop messaging)
    TEXT_TO_LANDLINE = "TEXT_TO_LANDLINE"  # text read out by TTS on a landline


MESSAGE_CHANNEL_SYNONYMS: dict[MessageChannel, tuple[str, ...]] = {
    MessageChannel.SMS: ("text", "text message", "short message", "sms mt", "sms mo"),
    MessageChannel.MMS: ("picture message", "multimedia", "mms mt"),
    MessageChannel.RCS: ("chat", "rich communication", "rbm", "rcs chat"),
    MessageChannel.WEB: ("web message", "desktop", "browser", "web chat", "messages for web"),
    MessageChannel.TEXT_TO_LANDLINE: ("text to speech", "tts", "landline text", "text to landline", "sms to fixed"),
}
# SMPP delivery-receipt ``stat`` values plus everyday spellings
DELIVERED_TRUE = ("true", "yes", "y", "1", "delivered", "DELIVRD", "ACCEPTD", "success")
DELIVERED_FALSE = (
    "false",
    "no",
    "n",
    "0",
    "undelivered",
    "UNDELIV",
    "EXPIRED",
    "DELETED",
    "REJECTD",
    "UNKNOWN",
    "failed",
)
DELIVERED = boolean_of(true=DELIVERED_TRUE, false=DELIVERED_FALSE)


class MessageEvent(_Event):
    msg_id: Annotated[str, known_as("msg_id")]
    sender: Annotated[Device, known_as("sender")]
    recipient: Annotated[Device, known_as("recipient")]
    channel: Annotated[MessageChannel, one_of(MessageChannel, MESSAGE_CHANNEL_SYNONYMS), known_as("channel")]
    body_chars: Annotated[int, known_as("body_chars", ge=1, le=1600)]
    sent_at: Annotated[datetime, known_as("sent_at")]
    delivered: Annotated[bool, DELIVERED, known_as("delivered")]
    body: Annotated[str | None, known_as("body", max_length=1600)] = None

    @classmethod
    def _after_resolve(cls, data: dict[str, Any]) -> dict[str, Any]:
        """``body_chars`` may be omitted when ``body`` is present."""
        if data.get("body_chars") is None and isinstance(data.get("body"), str):
            data = {**data, "body_chars": len(data["body"])}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.sender.id == self.recipient.id:
            raise ValueError("sender and recipient must differ")
        if self.body is not None and len(self.body) != self.body_chars:
            raise ValueError(f"body_chars={self.body_chars} but body has {len(self.body)} characters")
        if self.sender.type == DeviceType.LANDLINE:
            raise ValueError("landlines cannot originate text messages")
        to_landline = self.recipient.type == DeviceType.LANDLINE
        if to_landline != (self.channel == MessageChannel.TEXT_TO_LANDLINE):
            raise ValueError("TEXT_TO_LANDLINE is used iff the recipient is a landline")
        if self.sender.type == DeviceType.COMPUTER and self.channel not in {
            MessageChannel.WEB,
            MessageChannel.TEXT_TO_LANDLINE,
        }:
            raise ValueError("computers send via WEB (or TEXT_TO_LANDLINE)")
        return self

    def kafka_key(self) -> str:
        # Key by sender: one sender's messages stay ordered within a partition.
        return self.sender.id


# ───────────────────────────── 3. Trosglwyddo — cross-device hand-off ────────────────
class StepChannel(StrEnum):
    CALL = "CALL"
    VOICEMAIL = "VOICEMAIL"
    SMS = "SMS"
    EMAIL = "EMAIL"
    CHAT = "CHAT"
    VIDEO = "VIDEO"


STEP_CHANNEL_SYNONYMS: dict[StepChannel, tuple[str, ...]] = {
    StepChannel.CALL: ("voice", "phone call", "voice call", "call back", "callback"),
    StepChannel.VOICEMAIL: ("vm", "voice mail", "mailbox"),
    StepChannel.SMS: ("text", "text message"),
    StepChannel.EMAIL: ("e-mail", "mail", "smtp"),
    StepChannel.CHAT: ("web chat", "live chat", "im", "instant message"),
    StepChannel.VIDEO: ("video call", "teams", "meet", "facetime", "webrtc video"),
}


class HandoffStep(_Event):
    conversation_id: Annotated[str, known_as("conversation_id")]
    seq: Annotated[int, known_as("seq", ge=1)]
    device: Annotated[Device, known_as("device")]
    channel: Annotated[StepChannel, one_of(StepChannel, STEP_CHANNEL_SYNONYMS), known_as("channel")]
    action: Annotated[str, known_as("action")]
    at: Annotated[datetime, known_as("at")]
    content: Annotated[str | None, known_as("content", max_length=20_000)] = None

    def kafka_key(self) -> str:
        # Key by conversation: all steps of one conversation land on one partition, in order.
        return self.conversation_id


type TelcoEvent = CallEvent | MessageEvent | HandoffStep


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
