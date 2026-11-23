"""Which model each agent uses.

Content-bearing agents (Didolwr, Crynhoi, Holwr) default to a locally hosted model; a frontier
model is an explicit opt-in per agent (design §5, decision D5). The model is named by
``UNIONDEB_<AGENT>_MODEL`` in Pydantic AI's ``provider:model`` form. With nothing configured an
agent that needs a model refuses to run, so no content is ever sent to a default provider by accident.
"""

import os
from typing import Any


class ModelNotConfigured(RuntimeError):
    pass


def model_for(agent: str, override: Any = None) -> Any:
    """The configured model for ``agent``; ``override`` (a Pydantic AI model object or name) wins."""
    if override is not None:
        return override
    name = os.environ.get(f"UNIONDEB_{agent.upper()}_MODEL")
    if not name:
        raise ModelNotConfigured(f"set UNIONDEB_{agent.upper()}_MODEL (for example ollama:llama3.3) to run {agent}")
    return name


def model_name(model: Any) -> str:
    return model if isinstance(model, str) else getattr(model, "model_name", type(model).__name__)
