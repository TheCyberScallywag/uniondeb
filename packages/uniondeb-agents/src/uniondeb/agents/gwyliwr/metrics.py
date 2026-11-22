"""Concern volume per employee, normalised by activity (plan §5.4).

Raw counts flag busy people, so everything is a rate per 1,000 records. A rate is then compared
with two baselines using a robust z-score (median and MAD, so one extreme person or day does not
move the yardstick): the person's own earlier days, and their team in the same window.

Inputs are rows of the ``gwyliwr.scan.v1`` extract: counts per pseudonym per day. No content is
read, and nothing here knows who anyone is.

Implements: UND-060
"""

from collections import defaultdict
from statistics import median
from typing import Any

from pydantic import BaseModel

MIN_RECORDS = 20  # below this a rate is noise, so it is reported but not scored
DIRTY_WEIGHT = 3.0


class ConcernMetric(BaseModel):
    subject: str  # pseudonym
    team: str | None
    records: int
    question: int
    dirty: int  # current or proposed
    question_per_1000: float
    dirty_per_1000: float
    concern_per_1000: float  # question + DIRTY_WEIGHT × dirty, per 1,000 records
    z_own: float | None  # against this person's own baseline days
    z_team: float | None  # against teammates in the same window
    scored: bool


def robust_z(value: float, population: list[float]) -> float | None:
    """(value − median) / (1.4826 × MAD). ``None`` when there is too little to compare with."""
    if len(population) < 3:
        return None
    mid = median(population)
    mad = median(abs(x - mid) for x in population)
    spread = 1.4826 * mad
    if spread == 0:  # most of the population is identical: fall back to the mean absolute deviation
        spread = 1.2533 * sum(abs(x - mid) for x in population) / len(population)
    if spread == 0:
        return 0.0 if value == mid else None
    return round((value - mid) / spread, 2)


def _rate(concern: float, records: int) -> float:
    return round(1000 * concern / records, 1) if records else 0.0


def concern_metrics(
    people: list[dict[str, Any]], daily: list[dict[str, Any]], *, window_from: str, min_records: int = MIN_RECORDS
) -> list[ConcernMetric]:
    """Metrics for the window ``[window_from, …)``; earlier days in ``daily`` are the baseline."""
    team = {p["pid"]: p.get("team") for p in people}
    window: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    baseline: dict[str, list[float]] = defaultdict(list)
    for d in daily:
        if d["day"] >= window_from[:10]:
            w = window[d["pid"]]
            w[0], w[1], w[2] = w[0] + d["records"], w[1] + d["question"], w[2] + d["dirty"]
        elif d["records"]:
            baseline[d["pid"]].append(_rate(d["question"] + DIRTY_WEIGHT * d["dirty"], d["records"]))
    rates = {pid: _rate(q + DIRTY_WEIGHT * dy, n) for pid, (n, q, dy) in window.items()}
    by_team: dict[str | None, list[float]] = defaultdict(list)
    for pid, (n, _, _) in window.items():
        if n >= min_records:
            by_team[team.get(pid)].append(rates[pid])
    out = []
    for pid, (n, q, dy) in window.items():
        scored = n >= min_records
        out.append(
            ConcernMetric(
                subject=pid,
                team=team.get(pid),
                records=n,
                question=q,
                dirty=dy,
                question_per_1000=_rate(q, n),
                dirty_per_1000=_rate(dy, n),
                concern_per_1000=rates[pid],
                z_own=robust_z(rates[pid], baseline[pid]) if scored else None,
                z_team=robust_z(rates[pid], by_team[team.get(pid)]) if scored else None,
                scored=scored,
            )
        )
    return sorted(out, key=lambda m: (-m.concern_per_1000, m.subject))
