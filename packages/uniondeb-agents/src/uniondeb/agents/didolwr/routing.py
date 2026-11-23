"""Routing: what the trigger hits and the judgement together mean for a record (design §5.3 step 3).

Pure code, so the rules can be read and tested in one place:

- trigger hits → **question**, always, from the deterministic pass alone;
- a ``concern`` judgement → **dirty, proposed**, which goes to four-eyes review and is not current
  until two reviewers confirm it;
- a ``none`` judgement changes nothing. There is no path by which a model result lowers a tag:
  ``Routed`` has no way to express "clean because the model said so".
"""

from dataclasses import dataclass
from typing import Literal

from uniondeb.agents.didolwr.judge import EthicsJudgement


@dataclass(frozen=True, slots=True)
class Routed:
    label: Literal["clean", "question"]  # the deterministic tag that becomes current
    propose_dirty: bool  # also write a dirty proposal and open a four-eyes review item
    review_question: bool = False  # open a single-reviewer item for the question tag


def route(hits: list[str], judgement: EthicsJudgement | None, *, sampled_for_review: bool = False) -> Routed:
    label: Literal["clean", "question"] = "question" if hits else "clean"
    concern = judgement is not None and judgement.verdict == "concern"
    return Routed(label, propose_dirty=concern, review_question=bool(hits) and not concern and sampled_for_review)
