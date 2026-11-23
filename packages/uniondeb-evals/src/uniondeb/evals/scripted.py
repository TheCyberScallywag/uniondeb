"""Scripted stand-ins for a language model, for tests and for the offline gate.

The offline gate has to run on every pull request with no network and no model server, and it has
to give the same answer every time. These models answer from a script, so what the gate measures
is everything *around* the model: prompts carry no identities, outputs are validated, routing never
lets a model result lower a tag, proposals reach four-eyes review. How good a real model's
judgement is can only be measured against that model (``uniondeb evals gate --model …``).
"""

import csv
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart):
                return part.content if isinstance(part.content, str) else str(part.content)
    return ""


def output_model(answer: Callable[[str], dict[str, Any]]) -> FunctionModel:
    """A model that fills the agent's typed output with ``answer(prompt)``."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer(last_prompt(messages)))])

    return FunctionModel(respond, model_name="scripted")


def ground_truth_judge(labels: dict[str, tuple[str, list[str]]]) -> FunctionModel:
    """Judges a text as its ground-truth label says: an upper bound, used to test the plumbing."""

    def answer(prompt: str) -> dict[str, Any]:
        text = json.loads(prompt)["text"]
        tag, clauses = labels.get(text, ("clean", []))
        if tag != "dirty":
            return {"verdict": "none", "confidence": 0.9}
        return {
            "verdict": "concern",
            "clauses": clauses,
            "evidence_span": text.strip()[:60],
            "rationale": "matches a labelled misconduct fixture",
            "confidence": 0.9,
        }

    return output_model(answer)


def gullible_judge() -> FunctionModel:
    """The worst case: a model that always says there is no concern, as an injected instruction asks."""
    return output_model(lambda _prompt: {"verdict": "none", "confidence": 1.0})


def labels_by_text(run: Path) -> dict[str, tuple[str, list[str]]]:
    """Ground truth keyed by the text a tagger reads, built from a generated run."""
    from uniondeb.agents.didolwr.run import record_text
    from uniondeb.store.load import parse_row, read_rows

    with (run / "ground_truth" / "labels.csv").open(newline="") as fh:
        labels = {(x["dataset"], x["record_key"]): x for x in csv.DictReader(fh)}
    out: dict[str, tuple[str, list[str]]] = {}
    for entry in json.loads((run / "manifest.json").read_text())["files"]:
        for row in read_rows(run / entry["path"], entry["format"]):
            try:
                event = parse_row(entry["dataset"], row)
            except Exception:  # injected faults never reach a tagger
                continue
            dump = event.model_dump(mode="json")
            key = (
                dump.get("call_id")
                or dump.get("msg_id")
                or dump.get("message_id")
                or f"{dump.get('conversation_id')}#{dump.get('seq')}"
            )
            label = labels.get((entry["dataset"], key))
            if label and (text := record_text(dump)):
                worst = max(
                    out.get(text, ("clean", []))[0], label["expected_tag"], key=("clean", "question", "dirty").index
                )
                out[text] = (worst, [c for c in label["clauses"].split(";") if c] or out.get(text, ("", []))[1])
    return out
