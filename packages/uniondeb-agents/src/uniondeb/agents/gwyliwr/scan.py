"""A security scan: extract, measure, raise alerts for people to review (design §5.4).

``analyse`` is the agent's side: it sees one metrics extract and returns pseudonymous findings.
``run_scan`` is the trusted side around it: it builds the extract, and afterwards resolves the
pseudonyms so each alert can be linked to its subject in the system of record. An alert never
carries a name. It shows a stable pseudonym; the link to the person is followed only inside the
review console and the case workspace, by people with the role to do so.

Every alert goes to four-eyes review. Gwyliwr decides nothing.
"""

import hashlib
import hmac
import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from uniondeb.agents.gwyliwr.metrics import ConcernMetric, concern_metrics
from uniondeb.agents.gwyliwr.signals import Signal, graph_signals
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.extract import Echdynnwr, ExtractReader
from uniondeb.store import review
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner

GWYLIWR = AgentRef(name="gwyliwr", version="0.1.0")
CONTRACT = "gwyliwr.scan.v1"
Z_ALERT = 3.0  # a concern rate this many robust deviations above the team raises an alert on its own
type ScanKind = Literal["daily", "weekly", "on_demand"]
WINDOW_DAYS: dict[str, int] = {"daily": 1, "weekly": 7}


class SensitiveDate(BaseModel):
    """One entry of the reference calendar of client-sensitive dates."""

    date: date
    label: str


class Ranked(BaseModel):
    subject: str
    score: float
    metric: ConcernMetric | None
    signals: list[Signal] = Field(default_factory=list)


class Findings(BaseModel):
    window_from: str
    window_to: str
    metrics: list[ConcernMetric]
    signals: list[Signal]
    ranking: list[Ranked]


class ScanResult(BaseModel):
    scan_id: str
    kind: str
    extract_id: str
    findings: Findings
    alerts: list[str]


def stable_pseudonym(key: bytes, person_id: str) -> str:
    """The same person has the same pseudonym in every scan, so dashboards can follow a trend."""
    return "S-" + hmac.new(key, person_id.encode(), hashlib.sha256).hexdigest()[:12].upper()


_DEVICE = ("imei:", "tel:", "ip:")


def public_ref(key: bytes, real: str) -> tuple[str, str | None]:
    """(what an alert may show, the label of the node it stands for). Person and device ids embed
    names and numbers, so an alert shows a keyed pseudonym; the node is reached only by a link."""
    if real.startswith("owner:"):
        return stable_pseudonym(key, real), "Person"
    if real.startswith(_DEVICE):
        return "D-" + hmac.new(key, real.encode(), hashlib.sha256).hexdigest()[:12].upper(), "Device"
    if len(real) == 64:
        return real, "Attachment"  # an attachment hash identifies a file, not a person
    return real, None


async def analyse(
    reader: ExtractReader, *, window_from: str, window_to: str, calendar: list[SensitiveDate]
) -> Findings:
    people, daily = await reader.rows("people"), await reader.rows("daily")
    devices, contacts, mails = await reader.rows("devices"), await reader.rows("contacts"), await reader.rows("mails")
    metrics = concern_metrics(people, daily, window_from=window_from)
    in_window = lambda rows: [r for r in rows if window_from <= str(r["at"]) < window_to]  # noqa: E731
    signals = graph_signals(devices, contacts, in_window(mails), [(c.date, c.label) for c in calendar])
    by_subject: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        if s.subject:
            by_subject[s.subject].append(s)
    metric = {m.subject: m for m in metrics}
    ranking = []
    for subject in {*metric, *by_subject}:
        m = metric.get(subject)
        z = max(0.0, (m.z_team or 0.0) if m else 0.0, (m.z_own or 0.0) if m else 0.0)
        ranking.append(
            Ranked(
                subject=subject,
                score=round(min(z, 10.0) + sum(s.score for s in by_subject[subject]), 2),
                metric=m,
                signals=by_subject[subject],
            )
        )
    ranking.sort(key=lambda r: (-r.score, r.subject))
    return Findings(window_from=window_from, window_to=window_to, metrics=metrics, signals=signals, ranking=ranking)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_scan(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    echdynnwr: Echdynnwr,
    *,
    pseudonym_key: bytes,
    kind: ScanKind = "on_demand",
    window_to: datetime | None = None,
    window_days: int | None = None,
    baseline_days: int = 28,
    calendar: list[SensitiveDate] | None = None,
) -> ScanResult:
    end = window_to or datetime.now(UTC)
    start = end - timedelta(days=window_days or WINDOW_DAYS.get(kind, 7))
    handle = await echdynnwr.build(CONTRACT, {"from": _iso(start - timedelta(days=baseline_days)), "to": _iso(end)})
    async with echdynnwr.open(handle) as reader:
        findings = await analyse(reader, window_from=_iso(start), window_to=_iso(end), calendar=calendar or [])

    wanted = {r.subject for r in findings.ranking} | {
        v for s in findings.signals if s.hypothesis for v in (s.hypothesis["from"], s.hypothesis["to"])
    }
    real = await echdynnwr.resolve(handle.id, sorted(wanted))  # the trusted write-back side from here on
    public = {p: stable_pseudonym(pseudonym_key, pid) for p, pid in real.items() if pid.startswith("owner:")}

    scan_id, alerts = str(uuid.uuid7()), []
    async with tarddiad.activity(
        ActivityKind.SCAN, agent=GWYLIWR, trigger=kind, config_versions={"contract": handle.contract_hash[:16]}
    ) as act:
        act.used(EntityRef(label="Extract", key=handle.id))
        rows = [m.model_dump() | {"subject": public[m.subject]} for m in findings.metrics if m.subject in public]
        await runner.run(
            "CREATE (s:Scan {id: $id, kind: $kind, window_from: $from, window_to: $to, created_at: $now, "
            "extract_id: $extract, metrics_json: $metrics, signals: $signals})",
            {
                "id": scan_id,
                "kind": kind,
                "from": findings.window_from,
                "to": findings.window_to,
                "now": datetime.now(UTC).isoformat(),
                "extract": handle.id,
                "metrics": json.dumps(rows),
                "signals": len(findings.signals),
            },
        )
        act.generated(EntityRef(label="Scan", key=scan_id))
        for ranked in findings.ranking:
            m = ranked.metric
            over = bool(m and m.scored and max(m.z_team or 0.0, m.z_own or 0.0) >= Z_ALERT and (m.question or m.dirty))
            person = real.get(ranked.subject)
            if not person or not (ranked.signals or over):
                continue
            patterns = sorted({s.pattern for s in ranked.signals} | ({"concern_volume"} if over else set()))
            if await runner.run(
                "MATCH (a:Alert {state: 'open'})-[:ABOUT]->(:Person {id: $p}) WHERE a.patterns = $patterns "
                "AND a.window_to > $from RETURN a.id AS id LIMIT 1",
                {"p": person, "patterns": patterns, "from": findings.window_from},
            ):
                continue  # the same finding is already waiting for a reviewer
            lines = [s.explanation for s in ranked.signals]
            if m and over:
                lines.insert(
                    0,
                    f"Concern rate {m.concern_per_1000} per 1,000 records ({m.question} question, {m.dirty} dirty of "
                    f"{m.records}); robust z {m.z_team} against the team and {m.z_own} against their own baseline.",
                )
            hypotheses, links = [], []
            for s in ranked.signals:
                if not s.hypothesis:
                    continue
                shown = {"type": s.hypothesis["type"]}
                for end in ("from", "to"):
                    value = real.get(s.hypothesis[end], s.hypothesis[end])
                    shown[end], label = public_ref(pseudonym_key, value)
                    if label:
                        links.append({"label": label, "key": value, "type": s.hypothesis["type"], "side": end})
                hypotheses.append(shown)
            alert_id = str(uuid.uuid7())
            await runner.run(
                "MATCH (p:Person {id: $person}) CREATE (a:Alert) SET a = $props CREATE (a)-[:ABOUT]->(p)",
                {
                    "person": person,
                    "props": {
                        "id": alert_id,
                        "state": "open",
                        "pattern": patterns[0],
                        "patterns": patterns,
                        "score": ranked.score,
                        "subject_pseudonym": public[ranked.subject],
                        "window_from": findings.window_from,
                        "window_to": findings.window_to,
                        "metrics_json": json.dumps(m.model_dump(exclude={"subject"}) if m else {}),
                        "hypotheses_json": json.dumps(hypotheses),
                        "record_keys": sorted({k for s in ranked.signals for k in s.record_keys})[:500],
                        "explanation": " ".join(lines),
                        "scan_id": scan_id,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                },
            )
            for label in ("Person", "Device", "Attachment"):  # followed only by roles that may resolve identities
                prop = {"Person": "id", "Device": "id", "Attachment": "sha256"}[label]
                await runner.run(
                    f"MATCH (a:Alert {{id: $id}}) UNWIND $links AS l MATCH (n:`{label}` {{`{prop}`: l.key}}) "
                    "CREATE (a)-[:SUGGESTS {type: l.type, side: l.side}]->(n)",
                    {"id": alert_id, "links": [x for x in links if x["label"] == label]},
                )
            ref = EntityRef(label="Alert", key=alert_id)
            item = await review.create_item(
                runner,
                "alert",
                created_by=GWYLIWR.name,
                about=ref,
                summary=f"{', '.join(patterns)} (score {ranked.score}) for {public[ranked.subject]}",
                payload={"alert_id": alert_id, "patterns": patterns, "subject_pseudonym": public[ranked.subject]},
            )
            alerts.append(alert_id)
            act.generated(ref, EntityRef(label="ReviewItem", key=item))
    return ScanResult(scan_id=scan_id, kind=kind, extract_id=handle.id, findings=findings, alerts=alerts)


def calendar_from(entries: list[dict[str, Any]]) -> list[SensitiveDate]:
    return [SensitiveDate.model_validate(e) for e in entries]
