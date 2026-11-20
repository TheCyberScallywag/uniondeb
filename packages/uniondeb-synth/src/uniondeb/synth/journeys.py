"""Cross-device hand-off journeys: one person's conversation moving between devices and channels.

Implements: UND-012
"""

from uniondeb.core.models import DeviceType, StepChannel

L, M, C = DeviceType.LANDLINE, DeviceType.MOBILE, DeviceType.COMPUTER

# (device type, channel, action) templates
JOURNEYS: dict[str, list[tuple[DeviceType, StepChannel, str]]] = {
    "missed-landline-to-desktop": [
        (L, StepChannel.CALL, "incoming call missed"),
        (L, StepChannel.VOICEMAIL, "voicemail recorded"),
        (M, StepChannel.SMS, "voicemail transcript texted"),
        (C, StepChannel.CHAT, "replied from desktop"),
    ],
    "mobile-call-to-laptop": [
        (M, StepChannel.CALL, "call started on mobile"),
        (C, StepChannel.VIDEO, "call moved to laptop as video"),
        (C, StepChannel.EMAIL, "notes emailed after call"),
    ],
    "chat-escalates-to-call": [
        (C, StepChannel.CHAT, "web chat opened"),
        (M, StepChannel.SMS, "one-time code sent"),
        (M, StepChannel.CALL, "call-back from support"),
        (L, StepChannel.CALL, "transferred to home landline"),
    ],
    "text-then-call": [
        (M, StepChannel.SMS, "'free to talk?' text"),
        (M, StepChannel.CALL, "mobile call"),
        (C, StepChannel.EMAIL, "follow-up email"),
    ],
}
