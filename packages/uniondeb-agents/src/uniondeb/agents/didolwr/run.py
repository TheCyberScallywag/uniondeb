"""One Didolwr run: tag the untagged records of a source file from a content extract.

Didolwr never touches the system of record for reading. It asks Echdynnwr for the
``didolwr.tag-batch.v1`` extract, in which parties are pseudonyms, and writes back only through the
typed tag and review functions. The run is one ``Tag`` activity that used the extract and the
trigger-list version and generated every tag and review item, with the model and prompt version.

Without a judge the run still does the deterministic pass, so trigger tagging never waits for a model.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from uniondeb.agents.didolwr.judge import EthicsJudge, EthicsJudgement
from uniondeb.agents.didolwr.routing import route
from uniondeb.agents.didolwr.triggers import TriggerList, language
from uniondeb.core.prov import ActivityKind, AgentRef, EntityRef
from uniondeb.extract import Echdynnwr
from uniondeb.store import review
from uniondeb.store.provenance import Tarddiad
from uniondeb.store.runner import CypherRunner
from uniondeb.store.tags import TagWrite, append_initial_tags, append_tag

DIDOLWR = AgentRef(name="didolwr", version="0.1.0")
CONTRACT = "didolwr.tag-batch.v1"

# Text addressed to a classifier. Deterministic, and only ever a reason to look, never to clear.
_INJECTION = re.compile(
    r"ignore (?:all |your |any )?(?:previous |prior |above )?instructions|system\s*:|"
    r"note to the (?:assistant|model|ai|classifier)|"
    r"(?:tag|mark|classif\w+|label)\b.{0,40}\b(?:as )?(?:clean|no concern|safe)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DidolwrConfig:
    clean_sample_rate: float = 0.05  # share of trigger-free records the judge also reads (coded language)
    question_review_rate: float = 0.1  # share of question tags sampled to the single-reviewer queue
    concurrency: int = 4
    salt: str = "didolwr"


@dataclass
class DidolwrResult:
    file: str
    extract_id: str | None = None
    activity_id: str | None = None
    records: int = 0
    clean: int = 0
    question: int = 0
    proposed_dirty: int = 0
    judged: int = 0
    judge_failures: int = 0
    injection_suspected: int = 0
    review_items: list[str] = field(default_factory=list)


def record_text(row: dict[str, Any]) -> str:
    return "\n".join(str(row[k]) for k in ("subject", "body", "transcript", "content") if row.get(k))


def sampled(key: str, rate: float, salt: str) -> bool:
    """Deterministic sampling: the same record is always in or out for a given rate and salt."""
    if rate <= 0:
        return False
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:8], 16) / 2**32 < rate


def context_line(row: dict[str, Any], links: list[dict[str, Any]], parties: dict[str, dict[str, Any]]) -> str:
    """ "A (employee, mobile) → B (external, mobile), SMS, 21:40": roles and kinds, no identities."""
    order = {"FROM": 0, "SENT_FROM": 0, "ON": 0, "TO": 1}
    names = []
    for letter, link in zip("ABCD", sorted(links, key=lambda x: order.get(x["role"], 2)), strict=False):
        p = parties.get(link["party"], {})
        names.append(f"{letter} ({p.get('party_kind', 'unknown')}, {p.get('device_kind', 'unknown')})")
    who = " → ".join(names) or "unknown parties"
    medium = row.get("channel") or row.get("network") or row.get("dataset")
    extras = [x for x in (medium, row.get("direction"), str(row.get("occurred_at") or "")[11:16]) if x]
    return ", ".join([who, *extras])


async def tag_file(
    runner: CypherRunner,
    tarddiad: Tarddiad,
    echdynnwr: Echdynnwr,
    file_sha256: str,
    *,
    triggers: TriggerList | None = None,
    judge: EthicsJudge | None = None,
    config: DidolwrConfig | None = None,
) -> DidolwrResult:
    triggers, config = triggers or TriggerList.load(), config or DidolwrConfig()
    result = DidolwrResult(file_sha256)
    await runner.run(
        "MERGE (t:TriggerList {version: $v}) ON CREATE SET t.sha256 = $sha, t.owner = $owner, t.status = $status",
        {"v": triggers.version, "sha": triggers.sha256, "owner": triggers.owner, "status": triggers.status},
    )
    handle = await echdynnwr.build(CONTRACT, {"file": file_sha256})
    result.extract_id = handle.id
    async with echdynnwr.open(handle) as reader:
        records = await reader.rows("records")
        parties = {p["id"]: p for p in await reader.rows("parties")}
        links: dict[str, list[dict[str, Any]]] = {}
        for link in await reader.rows("involves"):
            links.setdefault(link["record"], []).append(link)
    result.records = len(records)

    versions = {"trigger_list": triggers.version, "trigger_list_sha": triggers.sha256[:16]}
    if judge is not None:
        versions["code_of_ethics"] = judge.code.version
    async with tarddiad.activity(
        ActivityKind.TAG,
        agent=DIDOLWR,
        model=judge.model_name if judge else None,
        prompt_version=judge.prompt_version if judge else None,
        config_versions=versions,
        trigger=f"file:{file_sha256[:16]}",
    ) as act:
        result.activity_id = act.id
        act.used(EntityRef(label="Extract", key=handle.id), EntityRef(label="TriggerList", key=triggers.version))

        scanned = {r["key"]: (r, record_text(r)) for r in records}
        hits = {key: triggers.scan(text) for key, (_, text) in scanned.items()}
        first = [
            TagWrite(
                record_key=key,
                label="question" if hits[key] else "clean",
                method="trigger",
                created_by=DIDOLWR.name,
                trigger_hits=hits[key],
                trigger_list_version=triggers.version,
                lang=language(scanned[key][1]) if scanned[key][1] else None,
            )
            for key in scanned
        ]
        act.generated(
            *(EntityRef(label="Tag", key=t) for t in await append_initial_tags(runner, first, activity_id=act.id))
        )
        result.question = sum(1 for h in hits.values() if h)
        result.clean = result.records - result.question

        suspects = {k for k, (_, text) in scanned.items() if _INJECTION.search(text)}
        result.injection_suspected = len(suspects)
        to_judge = [
            k
            for k, (_, text) in scanned.items()
            if judge and text and (hits[k] or k in suspects or sampled(k, config.clean_sample_rate, config.salt))
        ]
        gate = asyncio.Semaphore(config.concurrency)

        async def assess(key: str) -> tuple[str, EthicsJudgement | None]:
            row, text = scanned[key]
            async with gate:
                try:
                    return key, await judge.judge(text, context_line(row, links.get(key, []), parties))
                except Exception:  # a model failure must not lose the record: it goes to a person instead
                    return key, None

        judgements = dict(await asyncio.gather(*(assess(k) for k in to_judge)))
        result.judged = len(judgements)
        result.judge_failures = sum(1 for j in judgements.values() if j is None)

        for key in scanned:
            j = judgements.get(key)
            failed = key in judgements and j is None
            routed = route(
                hits[key], j, sampled_for_review=sampled(key, config.question_review_rate, config.salt + ":q")
            )
            about = EntityRef(label="Record", key=key)
            if routed.propose_dirty:
                proposal = TagWrite(
                    record_key=key,
                    label="dirty",
                    method="llm",
                    created_by=DIDOLWR.name,
                    trigger_hits=hits[key],
                    trigger_list_version=triggers.version,
                    clauses=j.clauses,
                    evidence_span=j.evidence_span,
                    rationale=j.rationale,
                    model=judge.model_name,
                    prompt_version=judge.prompt_version,
                    confidence=j.confidence,
                )
                tag_id = await append_tag(runner, proposal, proposed=True, activity_id=act.id)
                item = await review.create_item(
                    runner,
                    "dirty_tag",
                    created_by=DIDOLWR.name,
                    about=about,
                    summary=f"dirty proposed ({', '.join(j.clauses)}), confidence {j.confidence:.0%}",
                    payload={
                        "record_key": key,
                        "tag_id": tag_id,
                        "clauses": j.clauses,
                        "evidence_span": j.evidence_span,
                        "injection_suspected": key in suspects,
                    },
                )
                result.proposed_dirty += 1
                result.review_items.append(item)
                act.generated(EntityRef(label="Tag", key=tag_id), EntityRef(label="ReviewItem", key=item))
            elif routed.review_question or failed or (key in suspects):
                why = "judge unavailable" if failed else "text addresses a classifier" if key in suspects else "sampled"
                item = await review.create_item(
                    runner,
                    "question_tag",
                    created_by=DIDOLWR.name,
                    about=about,
                    summary=f"{routed.label} tag for review: {why}",
                    payload={"record_key": key, "reason": why, "trigger_hits": hits[key]},
                )
                result.review_items.append(item)
                act.generated(EntityRef(label="ReviewItem", key=item))
    return result
