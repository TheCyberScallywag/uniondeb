"""Online evals: what reviewers' decisions say about the agents, and who gets flagged (design §8).

Reviewer decisions are labelled examples for free. This report turns them into the numbers a
governance owner should watch: how often two reviewers confirm what Didolwr proposed, what they
decide instead, and how concern rates differ by team and by language, so that over-flagging of
Welsh-language messages or of one team is visible rather than assumed away.

It reads tags, decisions and roster teams only: no content, no names.
"""

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel

from uniondeb.store.runner import CypherRunner

MIN_GROUP = 50  # a rate from fewer records than this is shown but never raises a warning
DISPARITY = 2.0


class Rate(BaseModel):
    records: int
    question: int
    dirty: int  # current, or proposed and waiting
    concern_per_1000: float


class OnlineReport(BaseModel):
    dirty_reviews: dict[str, int]  # settled decision → count
    dirty_agreement: float | None  # share of settled dirty proposals that reviewers confirmed
    agreement_by_language: dict[str, float]
    agreement_by_medium: dict[str, float]
    question_reviews: dict[str, int]
    alert_reviews: dict[str, int]
    concern_by_team: dict[str, Rate]
    concern_by_language: dict[str, Rate]
    pack_ratings: dict[str, float]
    warnings: list[str]


def _rate(c: Counter) -> Rate:
    n = sum(c.values())
    concern = c["question"] + c["dirty"]
    return Rate(
        records=n, question=c["question"], dirty=c["dirty"], concern_per_1000=round(1000 * concern / n, 1) if n else 0.0
    )


def _disparity(groups: dict[str, Rate], what: str) -> list[str]:
    big = {k: v for k, v in groups.items() if v.records >= MIN_GROUP}
    if len(big) < 2:
        return []
    low = min(v.concern_per_1000 for v in big.values())
    return [
        f"{what} {k!r} has a concern rate of {v.concern_per_1000} per 1,000 records, more than {DISPARITY:g}× the lowest "
        f"group ({low}): check the trigger list and the judge for bias"
        for k, v in sorted(big.items())
        if v.concern_per_1000 > DISPARITY * max(low, 1.0)
    ]


async def online_report(runner: CypherRunner) -> OnlineReport:
    decided = await runner.run(
        "MATCH (i:ReviewItem {state: 'decided'}) WHERE i.kind IN ['dirty_tag', 'question_tag', 'alert'] "
        "OPTIONAL MATCH (i)-[:ABOUT]->(r:Record) OPTIONAL MATCH (f:Tag {method: 'trigger'})-[:ON]->(r) "
        "WITH i, r, collect(f.lang)[0] AS lang "
        "RETURN i.kind AS kind, i.decision AS decision, coalesce(lang, 'unknown') AS lang, "
        "coalesce(r.channel, r.network, r.dataset, 'unknown') AS medium"
    )
    by_kind: dict[str, Counter] = defaultdict(Counter)
    confirmed: dict[str, dict[str, list[int]]] = {
        "lang": defaultdict(lambda: [0, 0]),
        "medium": defaultdict(lambda: [0, 0]),
    }
    for d in decided:
        by_kind[d["kind"]][d["decision"]] += 1
        if d["kind"] == "dirty_tag":
            for dim in ("lang", "medium"):
                confirmed[dim][d[dim]][0] += d["decision"] == "confirm_dirty"
                confirmed[dim][d[dim]][1] += 1
    dirty = by_kind["dirty_tag"]
    share = lambda groups: {k: round(a / n, 3) for k, (a, n) in sorted(groups.items()) if n}  # noqa: E731

    tagged = await runner.run(
        "MATCH (t:Tag {current: true})-[:ON]->(r:Record) "
        "OPTIONAL MATCH (f:Tag {method: 'trigger'})-[:ON]->(r) "
        "WITH t, r, collect(f.lang)[0] AS lang, COUNT { (q:Tag {proposed: true, label: 'dirty'})-[:ON]->(r) WHERE q.superseded_by IS NULL } AS waiting "
        "OPTIONAL MATCH (p:Person {kind: 'employee'})-[:USES]->(:Device)<-[:FROM|SENT_FROM|ON]-(r) "
        "WITH r, t, lang, waiting, collect(DISTINCT p.team)[0] AS team "
        "RETURN coalesce(team, 'no employee sender') AS team, coalesce(lang, 'unknown') AS lang, "
        "CASE WHEN waiting > 0 THEN 'dirty' ELSE t.label END AS label, count(*) AS n"
    )
    teams: dict[str, Counter] = defaultdict(Counter)
    langs: dict[str, Counter] = defaultdict(Counter)
    for row in tagged:
        teams[row["team"]][row["label"]] += row["n"]
        langs[row["lang"]][row["label"]] += row["n"]
    by_team = {k: _rate(v) for k, v in sorted(teams.items())}
    by_lang = {k: _rate(v) for k, v in sorted(langs.items())}

    ratings: list[dict[str, Any]] = await runner.run(
        "MATCH (:Reviewer)-[r:RATED]->(:CasePack) RETURN count(r) AS n, avg(r.rating) AS mean"
    )
    n = ratings[0]["n"] if ratings else 0
    settled = sum(dirty.values())
    warnings = _disparity({k: v for k, v in by_lang.items() if k != "unknown"}, "language")
    warnings += _disparity({k: v for k, v in by_team.items() if k != "no employee sender"}, "team")
    if settled >= 20 and dirty["confirm_dirty"] / settled < 0.5:
        warnings.append(
            f"reviewers confirmed only {dirty['confirm_dirty']} of {settled} dirty proposals: the judge is over-proposing"
        )
    return OnlineReport(
        dirty_reviews=dict(dirty),
        dirty_agreement=round(dirty["confirm_dirty"] / settled, 3) if settled else None,
        agreement_by_language=share(confirmed["lang"]),
        agreement_by_medium=share(confirmed["medium"]),
        question_reviews=dict(by_kind["question_tag"]),
        alert_reviews=dict(by_kind["alert"]),
        concern_by_team=by_team,
        concern_by_language=by_lang,
        pack_ratings={"count": float(n), "mean": round(ratings[0]["mean"], 2) if n else 0.0},
        warnings=warnings,
    )
