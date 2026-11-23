"""Stream journaled email as JSON: what the journal receiver (Cennad) puts on the wire.

Every message sent from CyberScallywags UK accounts and equipment is blind-copied or
envelope-journaled to the platform. This module plays that stream:

    # replay a generated run in time order, 1 hour of mail per second
    uniondeb synth stream replay --run traffic/run-001 --speed 3600 --sink kafka

    # generate live mail with current timestamps, ~30 a minute, rare scenario injections
    uniondeb synth stream live --rate 30 --scenario-rate 0.01 --sink kafka

    # anywhere: --sink stdout (NDJSON), or --sink http --url <an endpoint of your own that accepts the JSON>

By default each document is **normalised** the way the receiver would: aliases resolved
(Graph exports included), validated against ``EmailEvent``, and published as canonical
JSON keyed by Message-ID. Documents that fail go to a dead-letter destination with the
error, for human review. ``--raw`` publishes documents untouched (to exercise Porthor).
"""

import argparse
import csv
import json
import random
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from uniondeb.core.mail import EmailEvent

from . import content as C
from .directory import Directory
from .scenarios import _ctx, email, email_address

TOPIC, DLQ_TOPIC = "mail-journal", "mail-journal-dlq"


# ───────────────────────────── sinks ────────────────────────────────────────────────
class Sink(Protocol):
    def send(self, key: str | None, doc: dict[str, Any], headers: dict[str, str]) -> None: ...
    def dead_letter(self, doc: dict[str, Any], error: str) -> None: ...
    def close(self) -> None: ...


class StdoutSink:
    def send(self, key, doc, headers) -> None:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")

    def dead_letter(self, doc, error) -> None:
        sys.stderr.write(json.dumps({"error": error, "document": doc}, ensure_ascii=False, default=str) + "\n")

    def close(self) -> None:
        sys.stdout.flush()


class KafkaSink:
    def __init__(self, bootstrap: str, topic: str = TOPIC, dlq: str = DLQ_TOPIC) -> None:
        from confluent_kafka import Producer

        self.topic, self.dlq = topic, dlq
        self.p = Producer(
            {
                "bootstrap.servers": bootstrap,
                "client.id": "uniondeb-mail-journal",
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 20,
                "compression.type": "zstd",
            }
        )

    def send(self, key, doc, headers) -> None:
        self.p.produce(self.topic, key=key, value=json.dumps(doc, ensure_ascii=False), headers=list(headers.items()))
        self.p.poll(0)

    def dead_letter(self, doc, error) -> None:
        self.p.produce(
            self.dlq, value=json.dumps(doc, ensure_ascii=False, default=str), headers=[("error", error[:1000].encode())]
        )
        self.p.poll(0)

    def close(self) -> None:
        self.p.flush(15)


class HttpSink:
    def __init__(self, url: str) -> None:
        self.url = url

    def _post(self, payload: dict[str, Any]) -> None:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, default=str).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()

    def send(self, key, doc, headers) -> None:
        self._post({"key": key, "headers": headers, "document": doc})

    def dead_letter(self, doc, error) -> None:
        self._post({"dead_letter": True, "error": error, "document": doc})

    def close(self) -> None:
        pass


# ───────────────────────────── normalise (the receiver's job) ───────────────────────
@dataclass
class Stats:
    sent: int = 0
    dead_lettered: int = 0
    by_direction: dict[str, int] = field(default_factory=dict)


def publish(sink: Sink, doc: dict[str, Any], stats: Stats, *, raw: bool = False, synthetic: bool = True) -> None:
    if raw:
        key = doc.get("message_id") or doc.get("internetMessageId")
        sink.send(key, doc, {"normalised": "false", "synthetic": str(synthetic).lower()})
        stats.sent += 1
        return
    try:
        ev = EmailEvent.model_validate(doc)
    except (ValidationError, ValueError) as exc:
        sink.dead_letter(doc, str(exc).replace("\n", " | ")[:2000])
        stats.dead_lettered += 1
        return
    canonical = json.loads(ev.model_dump_json())
    sink.send(
        ev.message_id,
        canonical,
        {
            "journal_source": ev.journal.source,
            "direction": ev.direction,
            "normalised": "true",
            "synthetic": str(synthetic).lower(),
        },
    )
    stats.sent += 1
    stats.by_direction[ev.direction] = stats.by_direction.get(ev.direction, 0) + 1


# ───────────────────────────── replay a generated run ───────────────────────────────
def _sent_at(doc: dict[str, Any]) -> datetime | None:
    v = doc.get("sent_at") or doc.get("sentDateTime")
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def replay_docs(run: Path) -> Iterator[dict[str, Any]]:
    manifest = json.loads((run / "manifest.json").read_text())
    docs: list[tuple[datetime, int, dict[str, Any]]] = []
    for n, f in enumerate(f for f in manifest["files"] if f["dataset"] == "email"):
        for i, line in enumerate((run / f["path"]).open()):
            doc = json.loads(line)
            docs.append((_sent_at(doc) or datetime.min.replace(tzinfo=UTC), n * 1_000_000 + i, doc))
    docs.sort(key=lambda t: (t[0], t[1]))
    yield from (d for _, _, d in docs)


def replay(sink: Sink, run: Path, speed: float, raw: bool, limit: int | None) -> Stats:
    stats, prev = Stats(), None
    for i, doc in enumerate(replay_docs(run)):
        if limit is not None and i >= limit:
            break
        at = _sent_at(doc)
        if speed > 0 and prev and at:
            time.sleep(min(2.0, max(0.0, (at - prev).total_seconds() / speed)))
        prev = at or prev
        publish(sink, doc, stats, raw=raw)
    return stats


# ───────────────────────────── live generation ──────────────────────────────────────
LIVE_SCENARIOS = (
    ("insider_dealing", C.INSIDER_EMAILS["forward_to_personal"]),
    ("drug_trafficking", C.DRUG_EMAILS["locker_email"]),
)


def live(
    sink: Sink,
    *,
    employees: int,
    externals: int,
    seed: int,
    rate_per_min: float,
    scenario_rate: float,
    duration_s: float | None,
    labels_out: Path | None,
) -> Stats:
    rng = random.Random(seed)
    d = Directory(seed=seed, employees=employees, externals=externals)
    staff, others = d.employee_list, d.external_list
    stats, start = Stats(), time.time()
    writer = None
    if labels_out:
        labels_out.parent.mkdir(parents=True, exist_ok=True)
        fh = labels_out.open("a", newline="")
        writer = csv.writer(fh)
    try:
        while duration_s is None or time.time() - start < duration_s:
            sender = rng.choice(staff)
            if rng.random() < scenario_rate:
                scenario, t = rng.choice(LIVE_SCENARIOS)
                to = (
                    [email_address(sender, personal=True)]
                    if t.typology == "insider_exfiltration"
                    else [email_address(rng.choice(others or staff))]
                )
            else:
                scenario = "normal"
                t = rng.choice(C.BENIGN_TRIGGER_EMAILS if rng.random() < 0.03 else C.NORMAL_EMAILS)
                pool = [p for p in staff + others if p is not sender]
                to = [email_address(p) for p in rng.sample(pool, k=rng.choice((1, 1, 1, 2, 3)))]
            ctx = {**_ctx(rng, d), "project": "Prosiect Morlo"}
            ev = email(rng, d, sender, to, datetime.now(UTC).replace(microsecond=0), t, ctx)
            publish(sink, json.loads(ev.model_dump_json()), stats)
            if writer:
                writer.writerow([ev.message_id, scenario, t.tag, ";".join(t.clauses), t.typology])
            time.sleep(max(0.0, rng.expovariate(rate_per_min / 60.0)) if rate_per_min > 0 else 0)
    except KeyboardInterrupt:
        pass
    finally:
        if writer:
            fh.close()
    return stats


# ───────────────────────────── CLI ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> Stats:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("replay", "live"):
        sp = sub.add_parser(name)
        sp.add_argument("--sink", choices=("stdout", "kafka", "http"), default="stdout")
        sp.add_argument("--bootstrap", default="localhost:29092")
        sp.add_argument("--topic", default=TOPIC)
        sp.add_argument("--url", help="for --sink http")
        sp.add_argument("--raw", action="store_true", help="publish documents untouched (no normalisation)")
        if name == "replay":
            sp.add_argument("--run", type=Path, required=True)
            sp.add_argument("--speed", type=float, default=3600.0, help="time compression; 0 = as fast as possible")
            sp.add_argument("--limit", type=int)
        else:
            sp.add_argument("--employees", type=int, default=40)
            sp.add_argument("--externals", type=int, default=120)
            sp.add_argument("--seed", type=int, default=1646)
            sp.add_argument("--rate", type=float, default=30.0, help="emails per minute")
            sp.add_argument("--scenario-rate", type=float, default=0.01)
            sp.add_argument("--duration", type=float, help="seconds; default runs until interrupted")
            sp.add_argument("--labels-out", type=Path)
    a = ap.parse_args(argv)
    sink: Sink = {
        "stdout": lambda: StdoutSink(),
        "kafka": lambda: KafkaSink(a.bootstrap, a.topic),
        "http": lambda: HttpSink(a.url),
    }[a.sink]()
    try:
        if a.mode == "replay":
            stats = replay(sink, a.run, a.speed, a.raw, a.limit)
        else:
            stats = live(
                sink,
                employees=a.employees,
                externals=a.externals,
                seed=a.seed,
                rate_per_min=a.rate,
                scenario_rate=a.scenario_rate,
                duration_s=a.duration,
                labels_out=a.labels_out,
            )
    finally:
        sink.close()
    print(
        json.dumps({"sent": stats.sent, "dead_lettered": stats.dead_lettered, "by_direction": stats.by_direction}),
        file=sys.stderr,
    )
    return stats


if __name__ == "__main__":
    main()
