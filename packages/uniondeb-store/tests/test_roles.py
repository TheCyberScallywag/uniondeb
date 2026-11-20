"""Roles as code: every planned role exists and the least-privilege rules hold in the rendered Cypher.

Verifies: UND-014
"""

import re

from uniondeb.core.cli import main
from uniondeb.store.roles import CONTENT_PROPERTIES, ROLE_NAMES, ROLES, Privilege, render

PLANNED = {
    "svc_ingest",
    "svc_writeback",
    "svc_extract_content",
    "svc_extract_metrics",
    "svc_review_api",
    "svc_investigation",
    "svc_sar",
    "auditor",
    "svc_retention",
}


def by_name(name):
    return next(r for r in ROLES if r.name == name)


def test_every_planned_role_is_defined_once():
    assert set(ROLE_NAMES) == PLANNED and len(ROLE_NAMES) == len(PLANNED)


def test_render_shapes():
    assert Privilege("GRANT", "ACCESS").render("uniondeb", "r") == "GRANT ACCESS ON DATABASE `uniondeb` TO `r`"
    p = Privilege("DENY", "READ", "NODES", ("Record",), ("body", "subject"))
    assert p.render("uniondeb", "r") == "DENY READ {`body`, `subject`} ON GRAPH `uniondeb` NODES `Record` TO `r`"
    assert Privilege("GRANT", "CREATE", "RELATIONSHIPS", ("ON",)).render("g", "r") == (
        "GRANT CREATE ON GRAPH `g` RELATIONSHIPS `ON` TO `r`"
    )
    assert Privilege("GRANT", "MATCH", "ELEMENTS").render("g", "r") == "GRANT MATCH {*} ON GRAPH `g` ELEMENTS * TO `r`"


def test_script_is_grant_or_deny_after_create_role():
    script = render("uniondeb")
    assert sum(s.startswith("CREATE ROLE") for s in script) == len(PLANNED)
    assert all(re.match(r"^(CREATE ROLE|GRANT|DENY) ", s) for s in script)


def test_metrics_role_is_denied_every_content_property():
    lines = by_name("svc_extract_metrics").render("g")
    denied = next(s for s in lines if s.startswith("DENY READ") and "`Record`" in s)
    for prop in CONTENT_PROPERTIES:
        assert f"`{prop}`" in denied


def test_auditor_never_reads_content():
    lines = by_name("auditor").render("g")
    assert any(s.startswith("DENY READ") and "`body`" in s for s in lines)
    assert not any(s.startswith("GRANT") and "`body`" in s for s in lines)


def test_content_extract_cannot_see_names_or_identifiers():
    lines = " ".join(by_name("svc_extract_content").render("g"))
    assert "DENY READ {`name`} ON GRAPH `g` NODES `Person`" in lines
    assert "`imei`" in lines and "`msisdn`" in lines


def test_no_role_can_delete_audit_events_and_only_retention_touches_content():
    for role in ROLES:
        lines = role.render("g")
        assert "DENY DELETE ON GRAPH `g` NODES `AuditEvent` TO " + f"`{role.name}`" in lines
        assert not any(s.startswith("GRANT DELETE") for s in lines), role.name
        sets_content = any(s.startswith("GRANT SET PROPERTY") and "`body`" in s for s in lines)
        assert sets_content == (role.name == "svc_retention"), role.name


def test_ingest_cannot_read_tags_or_cases():
    denied = next(s for s in by_name("svc_ingest").render("g") if s.startswith("DENY MATCH"))
    for label in ("Tag", "ReviewItem", "Alert", "Case", "Flag", "SarSubmission"):
        assert f"`{label}`" in denied


def test_cli_prints_the_script(capsys):
    assert main(["store", "--database", "uniondeb", "roles"]) == 0
    out = capsys.readouterr().out
    assert out.count("CREATE ROLE") == len(PLANNED) and "ON GRAPH `uniondeb`" in out


def test_committed_script_matches_code():
    from pathlib import Path

    committed = Path(__file__).parents[3] / "deploy" / "neo4j" / "roles.cypher"
    body = "\n".join(line for line in committed.read_text().splitlines() if not line.startswith("//"))
    assert body.strip() == (";\n".join(render("uniondeb")) + ";"), "regenerate deploy/neo4j/roles.cypher"
