"""Uniondeb core: record contracts, identifiers, alias resolution and dataset schemas.

Implements: UND-011
"""

from .mail import EmailEvent
from .models import CallEvent, ComputerDevice, Device, HandoffStep, LandlineDevice, MessageEvent, MobileDevice
from .resolve import ResolutionReport

__all__ = [
    "CallEvent",
    "ComputerDevice",
    "Device",
    "EmailEvent",
    "HandoffStep",
    "LandlineDevice",
    "MessageEvent",
    "MobileDevice",
    "ResolutionReport",
]
