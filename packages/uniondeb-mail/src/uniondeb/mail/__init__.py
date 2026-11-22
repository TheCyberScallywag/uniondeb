"""Cennad, the mail-journal receiver: journal parsers, raw store, publisher and SMTP endpoint.

Every email sent from company accounts is journaled to Cennad, which stores the raw message by
SHA-256, parses it into the ``EmailEvent`` contract and publishes canonical JSON to
``mail-journal`` keyed by Message-ID; what cannot be parsed goes to ``mail-journal-dlq``.

Implements: UND-025
Implements: UND-026
"""

from .parse import Parsed, ParseError, parse
from .publish import DLQ_TOPIC, TOPIC, DeadLetter, InMemoryPublisher, KafkaPublisher, NdjsonPublisher, Publisher, Sent
from .receiver import Cennad, JournalHandler, Outcome, from_settings, make_controller
from .render import render, to_eml, to_gws_copy, to_journal_report
from .settings import CennadSettings
from .storage import LocalObjectStore, ObjectStore

__all__ = [
    "DLQ_TOPIC",
    "TOPIC",
    "Cennad",
    "CennadSettings",
    "DeadLetter",
    "InMemoryPublisher",
    "JournalHandler",
    "KafkaPublisher",
    "LocalObjectStore",
    "NdjsonPublisher",
    "ObjectStore",
    "Outcome",
    "ParseError",
    "Parsed",
    "Publisher",
    "Sent",
    "from_settings",
    "make_controller",
    "parse",
    "render",
    "to_eml",
    "to_gws_copy",
    "to_journal_report",
]
