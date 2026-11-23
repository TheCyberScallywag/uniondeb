"""Didolwr ("sorter"): clean, question or dirty for every record (design §5.3)."""

from uniondeb.agents.didolwr.judge import CodeOfEthics, EthicsJudge, EthicsJudgement
from uniondeb.agents.didolwr.routing import Routed, route
from uniondeb.agents.didolwr.run import DIDOLWR, DidolwrConfig, DidolwrResult, tag_file
from uniondeb.agents.didolwr.triggers import TriggerList

__all__ = [
    "DIDOLWR",
    "CodeOfEthics",
    "DidolwrConfig",
    "DidolwrResult",
    "EthicsJudge",
    "EthicsJudgement",
    "Routed",
    "TriggerList",
    "route",
    "tag_file",
]
