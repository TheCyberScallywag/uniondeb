"""Verifies: UND-015"""

import uuid

import pytest
from pydantic import ValidationError

from uniondeb.core.prov import ActivityKind, ActivityRecord, AgentRef, EntityRef


def test_entity_labels_are_checked():
    assert str(EntityRef(label="SourceFile", key="ab")) == "SourceFile:ab"
    assert EntityRef(label="Record", key="calls:1").key_property == "key"
    with pytest.raises(ValidationError):
        EntityRef(label="Nope", key="x")


def test_activity_node_properties_are_flat_and_versioned():
    a = ActivityRecord(
        kind=ActivityKind.TAG,
        agent=AgentRef(name="didolwr", version="0.3.1"),
        model="anthropic:claude-sonnet-5-5",
        prompt_version="tag-v4",
        config_versions={"trigger_list": "v7", "ethics_clauses": "2026-10"},
    )
    assert uuid.UUID(a.id).version == 7
    props = a.node_properties()
    assert props["kind"] == "Tag" and props["agent_version"] == "0.3.1"
    assert props["cfg_trigger_list"] == "v7" and props["prompt_version"] == "tag-v4"
    assert all(not isinstance(v, dict | list) for v in props.values())
    assert "error" not in props


def test_config_keys_must_be_identifiers():
    with pytest.raises(ValidationError):
        ActivityRecord(kind="Scan", agent=AgentRef(name="gwyliwr", version="1"), config_versions={"Bad Key": "1"})
