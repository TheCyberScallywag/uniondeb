"""Publishers for the ``mail-journal`` stream and its dead-letter topic.

The wire conventions are those of the synthetic stream (``uniondeb.synth.stream``), so Porthor
consumes real and synthetic mail the same way: the key is the Message-ID (duplicate journal
copies collapse, ordering holds per message), the value is canonical ``EmailEvent`` JSON, and
every document carries ``journal_source``, ``direction``, ``normalised`` and ``synthetic``
headers. Cennad adds ``raw_sha256``, the provenance anchor.

``confluent_kafka`` is an optional extra and is imported only when the first document is
produced, so the parsers, the tests and a single-process deployment never need it.

Implements: UND-026
"""

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

TOPIC, DLQ_TOPIC = "mail-journal", "mail-journal-dlq"


class Publisher(Protocol):
    def send(self, key: str, doc: dict[str, Any], headers: dict[str, str]) -> None: ...
    def dead_letter(self, doc: dict[str, Any], error: str, headers: dict[str, str]) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Sent:
    key: str
    doc: dict[str, Any]
    headers: dict[str, str]


@dataclass(frozen=True)
class DeadLetter:
    doc: dict[str, Any]
    error: str
    headers: dict[str, str]


@dataclass
class InMemoryPublisher:
    """Keeps everything in lists: for tests and single-process use."""

    sent: list[Sent] = field(default_factory=list)
    dead: list[DeadLetter] = field(default_factory=list)
    closed: bool = False

    def send(self, key: str, doc: dict[str, Any], headers: dict[str, str]) -> None:
        self.sent.append(Sent(key, doc, dict(headers)))

    def dead_letter(self, doc: dict[str, Any], error: str, headers: dict[str, str]) -> None:
        self.dead.append(DeadLetter(doc, error, dict(headers)))

    def close(self) -> None:
        self.closed = True


class NdjsonPublisher:
    """One JSON line per document on stdout, dead letters on stderr: for running without Kafka."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self.out, self.err = out or sys.stdout, err or sys.stderr

    def send(self, key: str, doc: dict[str, Any], headers: dict[str, str]) -> None:
        self.out.write(json.dumps({"key": key, "headers": headers, "document": doc}, ensure_ascii=False) + "\n")
        self.out.flush()

    def dead_letter(self, doc: dict[str, Any], error: str, headers: dict[str, str]) -> None:
        line = {"dead_letter": True, "error": error, "headers": headers, "document": doc}
        self.err.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self.err.flush()

    def close(self) -> None:
        self.out.flush()


class KafkaPublisher:
    """Idempotent producer. Delivery is confirmed before ``send`` returns, so a 250 means published."""

    def __init__(self, bootstrap: str, topic: str = TOPIC, dlq: str = DLQ_TOPIC, *, timeout_s: float = 15.0) -> None:
        self.bootstrap, self.topic, self.dlq, self.timeout_s = bootstrap, topic, dlq, timeout_s
        self._producer: Any = None

    @property
    def producer(self) -> Any:
        if self._producer is None:
            from confluent_kafka import Producer  # optional extra: uniondeb-mail[kafka]

            self._producer = Producer(
                {
                    "bootstrap.servers": self.bootstrap,
                    "client.id": "uniondeb-cennad",
                    "enable.idempotence": True,
                    "acks": "all",
                    "linger.ms": 5,
                    "compression.type": "zstd",
                }
            )
        return self._producer

    def _produce(self, topic: str, key: str | None, doc: dict[str, Any], headers: dict[str, str]) -> None:
        failed: list[str] = []
        self.producer.produce(
            topic,
            key=key,
            value=json.dumps(doc, ensure_ascii=False, default=str),
            headers=[(k, str(v).encode()) for k, v in headers.items()],
            on_delivery=lambda err, _msg: failed.append(str(err)) if err is not None else None,
        )
        if self.producer.flush(self.timeout_s) or failed:
            raise RuntimeError(f"not delivered to {topic}: {failed[0] if failed else 'timed out'}")

    def send(self, key: str, doc: dict[str, Any], headers: dict[str, str]) -> None:
        self._produce(self.topic, key, doc, headers)

    def dead_letter(self, doc: dict[str, Any], error: str, headers: dict[str, str]) -> None:
        self._produce(self.dlq, None, doc, {**headers, "error": error[:1000]})

    def close(self) -> None:
        if self._producer is not None:
            self._producer.flush(self.timeout_s)
