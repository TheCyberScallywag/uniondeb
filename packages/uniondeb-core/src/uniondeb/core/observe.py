"""Logfire, configured once per service.

``configure`` is idempotent: the first call wins and later calls return the same settings. Data
leaves the process only when a write token is present (``LOGFIRE_TOKEN``); without one, spans stay
local, so tests and air-gapped runs never send anything. ``current_trace_id`` gives provenance the
id of the active span so an ``Activity`` can be joined to its trace.

Implements: UND-016
"""

import os
import threading
from dataclasses import dataclass

import logfire
from opentelemetry import trace

_lock = threading.Lock()
_configured: ObserveSettings | None = None

# Never put record content in spans: scrub these keys on top of Logfire's defaults.
SCRUB_PATTERNS = ("body", "transcript", "subject", "content", "msisdn", "imei", "imsi", "iccid")


@dataclass(frozen=True, slots=True)
class ObserveSettings:
    service_name: str
    service_version: str
    environment: str
    send_to_logfire: bool


def configure(
    service_name: str, *, service_version: str = "0.1.0", environment: str | None = None, token: str | None = None
) -> ObserveSettings:
    global _configured
    with _lock:
        if _configured is not None:
            return _configured
        token = token if token is not None else os.environ.get("LOGFIRE_TOKEN")
        settings = ObserveSettings(
            service_name=service_name,
            service_version=service_version,
            environment=environment or os.environ.get("UNIONDEB_ENV", "dev"),
            send_to_logfire=bool(token),
        )
        logfire.configure(
            service_name=settings.service_name,
            service_version=settings.service_version,
            environment=settings.environment,
            token=token or None,
            send_to_logfire=settings.send_to_logfire,
            console=False,
            scrubbing=logfire.ScrubbingOptions(extra_patterns=list(SCRUB_PATTERNS)),
        )
        _configured = settings
        return settings


def configured() -> ObserveSettings | None:
    return _configured


def current_trace_id() -> str | None:
    """32-hex trace id of the active span, or ``None`` outside a span."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _reset_for_tests() -> None:
    global _configured
    with _lock:
        _configured = None
