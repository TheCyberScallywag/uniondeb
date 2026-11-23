"""Porthor ("gatekeeper"): ingest and schema verification (design §5.1)."""

from uniondeb.agents.porthor.pipeline import PORTHOR, IngestResult, ingest_file, register_upload
from uniondeb.agents.porthor.registry import AliasRegistry

__all__ = ["PORTHOR", "AliasRegistry", "IngestResult", "ingest_file", "register_upload"]
