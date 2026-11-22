"""Shared fixtures for the Cennad tests: sample events, a local store and an in-memory publisher.

Verifies: UND-025
Verifies: UND-026
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from uniondeb.core.mail import EmailEvent
from uniondeb.mail import Cennad, InMemoryPublisher, LocalObjectStore

SIAN = '"Jones, Siân" <sian.jones@cyberscallywags.example>'
WELSH_SUBJECT = "Cyfarfod bwrdd: pecyn Prosiect Morlo – ŵyl y tŷ"
SENT = datetime(2026, 3, 2, 9, 30, 15, tzinfo=UTC)
RECEIVED = datetime(2026, 3, 2, 9, 30, 19, tzinfo=UTC)


@pytest.fixture
def make_event():
    def _make(**over: Any) -> EmailEvent:
        source = over.pop("source", "m365_envelope")
        envelope = over.pop("envelope_recipients", [])
        doc: dict[str, Any] = {
            "message_id": "<0199a1b2-c3d4@cyberscallywags.example>",
            "sender": SIAN,
            "to": ["Dafydd Puw <dafydd.puw@cyberscallywags.example>", "ext@example.org"],
            "cc": ["Gwen Llwyd <gwen.llwyd@cyberscallywags.example>"],
            "bcc": ["sian.private@mail.example"],
            "subject": WELSH_SUBJECT,
            "body": "Bore da,\n\nDyma'r pecyn bwrdd. Peidiwch â'i rannu.\n\nSiân",
            "sent_at": SENT,
            "journal": {"source": source, "received_at": RECEIVED, "envelope_recipients": envelope},
        }
        doc.update(over)
        return EmailEvent.model_validate(doc)

    return _make


@pytest.fixture
def store(tmp_path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "store")


@pytest.fixture
def publisher() -> InMemoryPublisher:
    return InMemoryPublisher()


@pytest.fixture
def cennad(store, publisher) -> Cennad:
    return Cennad(store, publisher)
