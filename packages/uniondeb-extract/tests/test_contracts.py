"""Contracts are validated before anything is built, and the shipped ones are all valid."""

import pytest

from uniondeb.extract import ContractError, ContractRegistry
from uniondeb.extract.contracts import FORBIDDEN_BY_ROLE, parse_contract
from uniondeb.store.roles import CONTENT_PROPERTIES

BASE = """
id: gwyliwr.test.v1
purpose: test
agent: gwyliwr
aura_role: {role}
params: [from]
{extra}
tables:
  - name: records
    node: Record
    key: key
    columns: [{columns}]
    cypher: |
      {cypher}
"""


def contract(cypher="MATCH (r:Record) WHERE r.occurred_at >= $from RETURN r.key AS key", **kw):
    kw = {"role": "svc_extract_metrics", "extra": "", "columns": "key"} | kw
    return parse_contract(BASE.format(cypher=cypher, **kw))


def test_shipped_contracts_load_with_stable_distinct_hashes():
    registry = ContractRegistry.load()
    ids = {c.id for c in registry}
    assert {"porthor.dedupe.v1", "didolwr.tag-batch.v1", "gwyliwr.scan.v1", "ymchwiliad.case.v1"} <= ids
    assert len({c.hash for c in registry}) == len(registry)
    assert ContractRegistry.load().get("gwyliwr.scan.v1").hash == registry.get("gwyliwr.scan.v1").hash
    with pytest.raises(ContractError, match="no extract contract"):
        registry.get("nope.v1")


def test_the_metrics_contract_cannot_name_a_content_property():
    scan = ContractRegistry.load().get("gwyliwr.scan.v1")
    for table in scan.tables:
        assert not (set(table.columns) | table.mentioned_properties) & set(CONTENT_PROPERTIES)
    assert set(CONTENT_PROPERTIES) <= FORBIDDEN_BY_ROLE["svc_extract_metrics"]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"cypher": "MATCH (r:Record) RETURN r.key AS key, r.body AS body", "columns": "key, body"}, "may not read"),
        ({"cypher": "MATCH (r:Record) WHERE r.transcript CONTAINS 'x' RETURN r.key AS key"}, "may not read"),
        ({"cypher": "MATCH (r:Record) WHERE r.`body` IS NOT NULL RETURN r.key AS key"}, "may not read"),
        ({"cypher": "MATCH (r:Record) SET r.seen = true RETURN r.key AS key"}, "read-only"),
        ({"cypher": "MATCH (r:Record) DETACH DELETE r"}, "read-only"),
        ({"cypher": "CALL apoc.export.csv.all('x', {}) YIELD file RETURN file AS key"}, "read-only"),
        ({"cypher": "MATCH (r:Record) WHERE r.at > $since RETURN r.key AS key"}, "undeclared parameters"),
        ({"role": "svc_writeback"}, "not an extract role"),
        ({"extra": "pseudonymise: [Record.nope]"}, "does not name a column"),
        ({"extra": "max_rows: 9999999"}, "less than or equal"),
        ({"extra": "forbid: [key]"}, "may not read"),
        ({"columns": "other"}, "must be declared"),
    ],
)
def test_bad_contracts_are_refused(kw, message):
    with pytest.raises(ContractError, match=message):
        contract(**kw)


def test_write_words_inside_string_literals_are_not_mistaken_for_writes():
    assert contract("MATCH (r:Record) WHERE r.action <> 'DELETE' AND r.occurred_at >= $from RETURN r.key AS key")


def test_any_edit_changes_the_hash():
    a = contract()
    assert a.hash != contract(extra="ttl_minutes: 5").hash
    assert a.hash != contract("MATCH (r:Record) WHERE r.occurred_at > $from RETURN r.key AS key").hash
    assert a.hash == contract().hash


def test_pseudonymisation_follows_keys_into_relationship_ends():
    batch = ContractRegistry.load().get("didolwr.tag-batch.v1")
    tables = {t.name: t for t in batch.tables}
    assert batch.pseudonymised(tables["parties"]) == {"id"}
    assert batch.pseudonymised(tables["involves"]) == {"party"}
    assert batch.pseudonymised(tables["records"]) == set()
