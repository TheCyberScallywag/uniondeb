"""Graph signals: patterns in who contacts whom, when and how (design §5.4, §5.2).

Each detector reads only the topology tables of the ``gwyliwr.scan.v1`` extract (pseudonymous
devices, contact edges with a time and a medium, mail metadata) and returns ``Signal``s. A signal
names a pattern and the records that show it. Where the pattern suggests a link that the data
cannot state as fact (this unregistered phone is controlled by that employee), the signal carries
it as a **hypothesis** for a person to test, never as a finding.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from uniondeb.agents.gwyliwr.metrics import robust_z

NIGHT_FROM, NIGHT_TO = 21, 5  # local-time handling is a deployment decision; timestamps here are UTC


class Signal(BaseModel):
    pattern: str
    subject: str | None  # employee pseudonym, when the pattern points at one
    score: float
    explanation: str
    record_keys: list[str] = Field(default_factory=list)
    hypothesis: dict[str, str] | None = None  # {"type": "CONTROLS", "from": pseudonym, "to": pseudonym}
    detail: dict[str, Any] = Field(default_factory=dict)


def _at(c: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(c["at"]).replace("Z", "+00:00"))


def _night(c: dict[str, Any]) -> bool:
    h = _at(c).hour
    return h >= NIGHT_FROM or h < NIGHT_TO


def unregistered_device_contact(devices: dict[str, dict], contacts: list[dict], *, min_nights: int = 2) -> list[Signal]:
    """An unregistered device in repeated late-night contact with an employee's device."""
    nights: dict[tuple[str, str], set[date]] = defaultdict(set)
    records: dict[tuple[str, str], list[str]] = defaultdict(list)
    concern: dict[str, int] = defaultdict(int)
    for c in contacts:
        for u in (c["a"], c["b"]):
            if not devices[u]["registered"] and c["concern"] != "none":
                concern[u] += 1
        for u, e in ((c["a"], c["b"]), (c["b"], c["a"])):
            if not devices[u]["registered"] and devices[e]["owner_kind"] == "employee" and _night(c):
                nights[(u, e)].add((_at(c) - timedelta(hours=NIGHT_TO)).date())
                records[(u, e)].append(c["record"])
    out = []
    for (u, e), ns in nights.items():
        if len(ns) >= min_nights:
            owner = devices[e]["owner_pid"]
            out.append(
                Signal(
                    pattern="unregistered_device_contact",
                    subject=owner,
                    score=round(2.0 + 0.25 * len(ns) + min(3.0, concern[u] / 10), 2),
                    explanation=(
                        f"An unregistered {devices[u]['kind']} was in late-night contact with this employee's "
                        f"{devices[e]['kind']} on {len(ns)} nights; {concern[u]} of that device's records carry concern tags."
                    ),
                    record_keys=sorted(records[(u, e)]),
                    hypothesis={"type": "CONTROLS", "from": owner, "to": u},
                    detail={"nights": len(ns), "device_concern_records": concern[u]},
                )
            )
    return out


def identifier_churn(devices: dict[str, dict], contacts: list[dict], *, min_overlap: float = 0.5) -> list[Signal]:
    """Two unregistered devices used one after the other with largely the same contact set."""
    peers: dict[str, set[str]] = defaultdict(set)
    seen: dict[str, list[datetime]] = defaultdict(list)
    for c in contacts:
        for u, other in ((c["a"], c["b"]), (c["b"], c["a"])):
            if not devices[u]["registered"]:
                peers[u].add(other)
                seen[u].append(_at(c))
    out, ids = [], sorted(peers)
    for i, u1 in enumerate(ids):
        for u2 in ids[i + 1 :]:
            first, second = sorted((u1, u2), key=lambda u: min(seen[u]))
            union = peers[u1] | peers[u2]
            overlap = len(peers[u1] & peers[u2]) / len(union) if union else 0.0
            if (
                overlap >= min_overlap
                and len(peers[u1] & peers[u2]) >= 3
                and max(seen[first]) <= min(seen[second]) + timedelta(days=1)
            ):
                out.append(
                    Signal(
                        pattern="identifier_churn",
                        subject=None,
                        score=round(1.0 + overlap, 2),
                        explanation=(
                            f"Two unregistered devices share {overlap:.0%} of their contacts, and the second "
                            "became active as the first went quiet: possibly one line that changed number."
                        ),
                        hypothesis={"type": "SAME_LINE", "from": first, "to": second},
                        detail={"shared_contacts": len(peers[u1] & peers[u2]), "overlap": round(overlap, 2)},
                    )
                )
    return out


def _pairs(devices: dict[str, dict], contacts: list[dict]) -> dict[tuple[str, str], list[tuple[dict, str, str]]]:
    """Contacts between an employee and a registered external, keyed by (employee, external) owners."""
    out: dict[tuple[str, str], list[tuple[dict, str, str]]] = defaultdict(list)
    for c in contacts:
        for e, x in ((c["a"], c["b"]), (c["b"], c["a"])):
            de, dx = devices[e], devices[x]
            if de["owner_kind"] == "employee" and dx["owner_kind"] == "external":
                out[(de["owner_pid"], dx["owner_pid"])].append((c, e, x))
    return out


def contact_spike(
    devices: dict[str, dict],
    contacts: list[dict],
    sensitive_dates: list[tuple[date, str]],
    *,
    days_before: int = 10,
    min_contacts: int = 6,
) -> list[Signal]:
    """An insider-list employee's contact with one external party standing out in the run-up to a
    sensitive date: far above their contact with anyone else outside, and above its own earlier rate."""
    out = []
    insiders = {d["owner_pid"] for d in devices.values() if d.get("insider_list")}
    earliest = min((_at(c).date() for c in contacts), default=None)
    by_employee: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for (emp, ext), items in _pairs(devices, contacts).items():
        if emp in insiders:
            by_employee[emp][ext] = [c for c, _, _ in items]
    for when, label in sensitive_dates:
        start = when - timedelta(days=days_before)
        history_days = (start - earliest).days if earliest else 0
        for emp, pairs in by_employee.items():
            run_up = {ext: [c for c in cs if start <= _at(c).date() <= when] for ext, cs in pairs.items()}
            counts = [float(len(cs)) for cs in run_up.values()]
            for ext, cs in run_up.items():
                earlier = [c for c in pairs[ext] if _at(c).date() < start]
                before_rate = len(earlier) / history_days if history_days > 0 else 0.0
                z = robust_z(float(len(cs)), counts)
                if len(cs) >= min_contacts and (z or 0) >= 3.5 and len(cs) / (days_before + 1) >= 3 * before_rate:
                    out.append(
                        Signal(
                            pattern="contact_spike",
                            subject=emp,
                            score=round(2.0 + min(3.0, len(cs) / 4), 2),
                            explanation=(
                                f"This insider-list employee had {len(cs)} contacts with one external party in the "
                                f"{days_before} days before {when.isoformat()} ({label}), against {len(earlier)} before "
                                "that and far more than with any other external party."
                            ),
                            record_keys=sorted(c["record"] for c in cs),
                            hypothesis={"type": "TIPPED", "from": emp, "to": ext},
                            detail={
                                "sensitive_date": when.isoformat(),
                                "run_up": len(cs),
                                "earlier": len(earlier),
                                "z": z,
                            },
                        )
                    )
    return out


def channel_switch(devices: dict[str, dict], contacts: list[dict], *, within_hours: int = 48) -> list[Signal]:
    """A pair moves to a landline shortly after a record between them on another channel drew a
    concern tag. A landline call on its own is ordinary; the sequence is what is reported."""
    out = []
    for (emp, _ext), items in _pairs(devices, contacts).items():
        items = sorted(items, key=lambda t: _at(t[0]))
        is_landline = lambda t: "landline" in (devices[t[1]]["kind"], devices[t[2]]["kind"])  # noqa: E731
        flagged = [t for t in items if t[0]["concern"] != "none" and not is_landline(t)]
        after = [
            t
            for t in items
            if is_landline(t)
            and any(timedelta(0) < _at(t[0]) - _at(f[0]) <= timedelta(hours=within_hours) for f in flagged)
        ]
        if after:
            out.append(
                Signal(
                    pattern="channel_switch",
                    subject=emp,
                    score=2.5,
                    explanation=(
                        f"Within {within_hours} hours of a record that drew a concern tag, this employee and one "
                        f"external party moved to a landline ({len(after)} landline contacts)."
                    ),
                    record_keys=sorted({t[0]["record"] for t in (*flagged, *after)}),
                    detail={"flagged": len(flagged), "landline_contacts": len(after)},
                )
            )
    return out


def email_exfiltration(mails: list[dict]) -> list[Signal]:
    """An attachment first seen in internal mail later sent to a personal webmail address."""
    first_internal: dict[str, str] = {}
    for m in sorted(mails, key=lambda m: m["at"]):
        if m["direction"] == "internal":
            for sha in m["attachments"] or []:
                first_internal.setdefault(sha, m["at"])
    out = []
    for m in mails:
        if not m["personal_recipients"]:
            continue
        reused = [sha for sha in m["attachments"] or [] if first_internal.get(sha, "9999") < m["at"]]
        if reused or m["self_forward"]:
            what = "an attachment first seen in internal mail" if reused else "mail"
            to = "their own personal address" if m["self_forward"] else "a personal webmail address"
            out.append(
                Signal(
                    pattern="email_exfiltration",
                    subject=m["sender_pid"],
                    score=(3.0 if reused else 1.0) + (1.0 if m["self_forward"] else 0.0),
                    explanation=f"This employee sent {what} to {to}.",
                    record_keys=[m["record"]],
                    hypothesis={"type": "EXFILTRATED", "from": m["sender_pid"], "to": reused[0]} if reused else None,
                    detail={"attachment_sha256": reused, "self_forward": bool(m["self_forward"])},
                )
            )
    return out


def graph_signals(
    devices: list[dict], contacts: list[dict], mails: list[dict], sensitive_dates: list[tuple[date, str]]
) -> list[Signal]:
    by_id = {d["id"]: d for d in devices}
    contacts = [c for c in contacts if c["a"] in by_id and c["b"] in by_id]
    control = unregistered_device_contact(by_id, contacts)
    controller = {s.hypothesis["to"]: s.subject for s in control if s.hypothesis}
    churn = identifier_churn(by_id, contacts)
    for s in churn:  # a line that changed number belongs with whoever appears to control either phone
        s.subject = controller.get(s.hypothesis["from"]) or controller.get(s.hypothesis["to"])
    return [
        *control,
        *churn,
        *contact_spike(by_id, contacts, sensitive_dates),
        *channel_switch(by_id, contacts),
        *email_exfiltration(mails),
    ]
