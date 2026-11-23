"""Deliveries and the event hub that reports on them.

A delivery is one file on its way to Uniondeb: uploaded as a file, or cut from a stream. Every
change of state is published as an event; SSE clients read those events, and a late client can
replay recent ones with ``Last-Event-ID``.

States: ``received`` → ``forwarding`` → ``queued`` → ``ingesting`` → ``ingested``, or one of
``duplicate`` (Uniondeb already holds these bytes), ``unreadable`` (Uniondeb took the file but could
not parse it, and raised a review item), ``rejected`` (Uniondeb refused the file),
``failed`` (the ingest job died), ``undeliverable`` (Uniondeb could not be reached).
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

TERMINAL = frozenset({"ingested", "duplicate", "unreadable", "rejected", "failed", "undeliverable"})
RETRYABLE = frozenset({"rejected", "failed", "undeliverable"})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Delivery(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source: Literal["file", "stream"]
    filename: str
    dataset: str | None = None
    source_system: str | None = None
    sha256: str
    bytes: int
    records: int | None = None  # known for stream batches
    state: str = "received"
    detail: str | None = None
    attempts: int = 0
    job: str | None = None  # Uniondeb's ingest job
    uniondeb: dict[str, Any] = Field(default_factory=dict)  # what Uniondeb last said about the file
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    @property
    def done(self) -> bool:
        return self.state in TERMINAL


class Event(BaseModel):
    id: int
    type: str
    at: str
    data: dict[str, Any]

    def sse(self) -> str:
        body = json.dumps({"at": self.at, **self.data}, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type}\ndata: {body}\n\n"


class Hub:
    """Fan events out to subscribers and keep the last ``history`` for replay."""

    def __init__(self, history: int = 2000) -> None:
        self._events: deque[Event] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._next = 1

    def publish(self, type: str, data: dict[str, Any]) -> Event:
        event = Event(id=self._next, type=type, at=now(), data=data)
        self._next += 1
        self._events.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    async def subscribe(self, after: int = 0, *, replay: bool = True) -> AsyncIterator[Event | None]:
        """Events with an id above ``after``; ``None`` every 15 seconds of silence (a heartbeat)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            if replay:
                for event in [e for e in self._events if e.id > after]:
                    after = event.id
                    yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15.0)
                except TimeoutError:
                    yield None
                    continue
                if event.id > after:
                    yield event
        finally:
            self._subscribers.discard(queue)


class Registry:
    """The deliveries this process has seen. In memory: the spool directory is the durable part."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._items: dict[str, Delivery] = {}

    def add(self, delivery: Delivery) -> Delivery:
        self._items[delivery.id] = delivery
        self.hub.publish("delivery", delivery.model_dump())
        return delivery

    def get(self, delivery_id: str) -> Delivery | None:
        return self._items.get(delivery_id)

    def list(self, state: str | None = None) -> list[Delivery]:
        items = sorted(self._items.values(), key=lambda d: d.created_at, reverse=True)
        return [d for d in items if state is None or d.state == state]

    def update(self, delivery: Delivery, **changes: Any) -> Delivery:
        """Apply ``changes`` and publish an event, but only if something actually changed."""
        if all(getattr(delivery, k) == v for k, v in changes.items()):
            return delivery
        for key, value in changes.items():
            setattr(delivery, key, value)
        delivery.updated_at = now()
        self.hub.publish("delivery", delivery.model_dump())
        return delivery

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self._items.values():
            out[d.state] = out.get(d.state, 0) + 1
        return out
