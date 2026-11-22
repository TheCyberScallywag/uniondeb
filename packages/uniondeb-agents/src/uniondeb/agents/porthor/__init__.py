"""Porthor ("gatekeeper"): ingest and schema verification (plan §5.1).

Implements: UND-021, UND-022, UND-023, UND-027
"""

from uniondeb.agents.porthor.pipeline import PORTHOR, IngestResult, ingest_file, register_upload
from uniondeb.agents.porthor.registry import AliasRegistry

__all__ = ["PORTHOR", "AliasRegistry", "IngestResult", "ingest_file", "register_upload"]
