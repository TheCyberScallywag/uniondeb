"""Aura service roles and fine-grained privileges, as code (plan §4, "Aura service accounts").

Each role is a list of privileges rendered to Cypher ``GRANT``/``DENY`` against one graph. ``apply``
reconciles the server with this file: it creates missing roles, revokes whatever each role holds
now, then grants exactly what is declared, so a privilege removed here is removed in Aura too.

Two limits of Neo4j's model, handled elsewhere:
- Creating a node with properties needs ``SET PROPERTY`` on that label, so "create but never edit"
  cannot be expressed. Audit events are made tamper-evident by their hash chain (UND-071) and no
  role may delete them.
- Per-case ABAC (an investigator sees only assigned cases) is enforced in the API layer with
  OIDC claims (UND-070); these roles set the outer bound.

Implements: UND-014
"""

from dataclasses import dataclass, field
from typing import Literal

from uniondeb.store.runner import Neo4jRunner

CONTENT_PROPERTIES = ("body", "subject", "transcript", "content", "text")
IDENTITY_PROPERTIES = ("msisdn", "number", "ip", "imei", "imsi", "iccid", "mac")

Verb = Literal["GRANT", "DENY"]
Action = Literal["ACCESS", "TRAVERSE", "READ", "MATCH", "CREATE", "MERGE", "SET PROPERTY", "DELETE"]
Segment = Literal["NODES", "RELATIONSHIPS", "ELEMENTS"]
WITH_PROPS = {"READ", "MATCH", "MERGE", "SET PROPERTY"}


def _ident(name: str) -> str:
    return name if name == "*" else f"`{name.replace('`', '``')}`"


@dataclass(frozen=True, slots=True)
class Privilege:
    verb: Verb
    action: Action
    segment: Segment = "NODES"
    labels: tuple[str, ...] = ("*",)
    properties: tuple[str, ...] = ("*",)

    def render(self, graph: str, role: str) -> str:
        if self.action == "ACCESS":
            return f"{self.verb} ACCESS ON DATABASE {_ident(graph)} TO {_ident(role)}"
        props = f" {{{', '.join(_ident(p) for p in self.properties)}}}" if self.action in WITH_PROPS else ""
        what = f"{self.segment} {', '.join(_ident(lbl) for lbl in self.labels)}"
        return f"{self.verb} {self.action}{props} ON GRAPH {_ident(graph)} {what} TO {_ident(role)}"


def grant(action: Action, *labels: str, segment: Segment = "NODES", props: tuple[str, ...] = ("*",)) -> Privilege:
    return Privilege("GRANT", action, segment, labels or ("*",), props)


def deny(action: Action, *labels: str, segment: Segment = "NODES", props: tuple[str, ...] = ("*",)) -> Privilege:
    return Privilege("DENY", action, segment, labels or ("*",), props)


@dataclass(frozen=True, slots=True)
class Role:
    name: str
    purpose: str
    privileges: tuple[Privilege, ...] = field(default_factory=tuple)

    def render(self, graph: str) -> list[str]:
        return [p.render(graph, self.name) for p in (*self.privileges, *GUARDS)]


# Every role: no deleting or relinking the audit chain.
GUARDS = (
    deny("DELETE", "AuditEvent"),
    deny("DELETE", "PREV", segment="RELATIONSHIPS"),
)

INGEST_NODES = (
    "SourceFile",
    "Record",
    "Device",
    "Identifier",
    "Person",
    "Activity",
    "SoftwareAgent",
    "Email",
    "EmailAddress",
    "Attachment",
    "IngestError",
    "MappingProposal",
    "AliasRegistry",
)
INGEST_RELS = (
    "CONTAINED",
    "FROM",
    "TO",
    "ON",
    "USES",
    "HAS_IDENTIFIER",
    "USED",
    "GENERATED",
    "WAS_ASSOCIATED_WITH",
    "SENT_FROM",
    "BELONGS_TO",
    "ATTACHED",
    "HAD_ERROR",
    "FOR",
)
PROVENANCE_RELS = ("USED", "GENERATED", "WAS_ASSOCIATED_WITH")
CASE_NODES = ("Case", "Flag", "CaseNote", "SarDraft", "SarSubmission")
RECORD_META = ("key", "dataset", "schema_version", "channel", "network", "action", "direction", "occurred_at")

ROLES: tuple[Role, ...] = (
    Role(
        "svc_ingest",
        "Porthor and Cennad write source files, records, devices and identifiers",
        (
            grant("ACCESS"),
            grant("MERGE", *INGEST_NODES),
            grant("TRAVERSE", *INGEST_RELS, segment="RELATIONSHIPS"),
            grant("CREATE", *INGEST_RELS, segment="RELATIONSHIPS"),
            grant("SET PROPERTY", *INGEST_RELS, segment="RELATIONSHIPS"),
            deny("MATCH", "Tag", "ReviewItem", "Alert", *CASE_NODES),
            deny("DELETE"),
        ),
    ),
    Role(
        "svc_writeback",
        "Agents write tag proposals, review items, alerts and case packs",
        (
            grant("ACCESS"),
            grant("TRAVERSE", "Record", "SourceFile"),
            grant("READ", "Record", props=("key",)),
            grant(
                "MATCH", "Tag", "ReviewItem", "Alert", "CasePack", "Activity", "SoftwareAgent", "Scan", "TriggerList"
            ),
            grant("MATCH", "Extract", "IngestError", "MappingProposal"),
            grant("CREATE", "Tag", "ReviewItem", "Alert", "CasePack", "Activity", "Scan", "TriggerList"),
            grant("SET PROPERTY", "Tag", "ReviewItem", "Alert", "CasePack", "Activity", "Scan", "TriggerList"),
            grant("TRAVERSE", "Person", "Device", "Attachment"),
            grant("CREATE", "ON", "ABOUT", "SUPERSEDES", "SUGGESTS", *PROVENANCE_RELS, segment="RELATIONSHIPS"),
            deny("SET PROPERTY", "Record", "SourceFile", "Device", "Identifier", "Person"),
            deny("DELETE"),
        ),
    ),
    Role(
        "svc_extract_content",
        "Didolwr extracts: record content, never names or identifiers",
        (
            grant("ACCESS"),
            grant("TRAVERSE", "Record", "SourceFile", "Tag", "Device", "Person"),
            grant("TRAVERSE", "CONTAINED", "ON", "FROM", "TO", "USES", segment="RELATIONSHIPS"),
            grant("READ", "Record", props=(*RECORD_META, *CONTENT_PROPERTIES)),
            grant("READ", "SourceFile", props=("sha256", "synthetic")),
            grant("READ", "Tag", props=("label", "current")),
            grant("READ", "Device", "Person", props=("id", "kind")),
            deny("READ", "Person", props=("name",)),
            deny("READ", "Device", "Identifier", props=IDENTITY_PROPERTIES + ("value",)),
        ),
    ),
    Role(
        "svc_extract_metrics",
        "Gwyliwr scans: counts and topology, never content",
        (
            grant("ACCESS"),
            grant("MATCH", "Record", "Tag", "Person", "Device", "SourceFile", "EmailAddress", "Attachment"),
            grant("TRAVERSE", segment="RELATIONSHIPS"),
            deny("READ", "Record", "Email", props=CONTENT_PROPERTIES),
            deny("READ", "Person", "EmailAddress", props=("name",)),
        ),
    ),
    Role(
        "svc_review_api",
        "Review queue API: assigned items only (ABAC in the API)",
        (
            grant("ACCESS"),
            grant("MATCH", "Record", "Tag", "ReviewItem", "SourceFile", "Activity", "SoftwareAgent"),
            grant("TRAVERSE", segment="RELATIONSHIPS"),
            grant("CREATE", "Tag", "Activity"),
            grant("SET PROPERTY", "Tag", "Activity"),
            grant("SET PROPERTY", "ReviewItem", props=("state", "decided_at", "decided_by")),
            grant("CREATE", "ON", "ABOUT", "SUPERSEDES", *PROVENANCE_RELS, segment="RELATIONSHIPS"),
            deny("SET PROPERTY", "Record"),
            deny("DELETE"),
        ),
    ),
    Role(
        "svc_investigation",
        "Ymchwiliad portal: cases, flags, notes, SAR drafts and audit events",
        (
            grant("ACCESS"),
            grant("MATCH", segment="ELEMENTS"),
            grant("CREATE", "Case", "Flag", "CaseNote", "SarDraft", "AuditEvent", "Tag", "Activity"),
            grant("SET PROPERTY", "Case", "Flag", "CaseNote", "SarDraft", "AuditEvent", "Tag", "Activity"),
            grant(
                "CREATE",
                "SUBJECT",
                "HAS",
                "ON",
                "ABOUT",
                "PREV",
                "SUPERSEDES",
                *PROVENANCE_RELS,
                segment="RELATIONSHIPS",
            ),
            deny("SET PROPERTY", "Record", "SourceFile", "SarSubmission"),
            deny("DELETE"),
        ),
    ),
    Role(
        "svc_sar",
        "Mock SAR submission after nominated-officer approval",
        (
            grant("ACCESS"),
            grant("MATCH", "SarDraft", "Case", "AuditEvent"),
            grant("TRAVERSE", "HAS", "PREV", segment="RELATIONSHIPS"),
            grant("CREATE", "SarSubmission", "AuditEvent"),
            grant("SET PROPERTY", "SarSubmission", "AuditEvent"),
            grant("SET PROPERTY", "SarDraft", props=("state",)),
            grant("CREATE", "REPORTED", "ABOUT", "PREV", segment="RELATIONSHIPS"),
            deny("DELETE"),
        ),
    ),
    Role(
        "auditor",
        "Read provenance and the audit log; never content",
        (
            grant("ACCESS"),
            grant("MATCH", "Activity", "SoftwareAgent", "AuditEvent", "SourceFile", "Migration", "Case"),
            grant("TRAVERSE", "Record", "Tag"),
            grant("READ", "Record", props=RECORD_META),
            grant("READ", "Tag", props=("label", "current", "method", "created_at")),
            grant("TRAVERSE", segment="RELATIONSHIPS"),
            deny("READ", "Record", "Email", props=CONTENT_PROPERTIES),
            deny("SET PROPERTY", segment="ELEMENTS"),
            deny("DELETE"),
        ),
    ),
    Role(
        "svc_retention",
        "Clears content properties on schedule; honours legal hold",
        (
            grant("ACCESS"),
            grant("TRAVERSE", "Record", "Email", "Case", "Flag", "Tag", "Person", "Device", "IngestError"),
            grant("TRAVERSE", "HAS", "ON", "SUBJECT", "USES", "FROM", "TO", "SENT_FROM", segment="RELATIONSHIPS"),
            grant("READ", "Record", "Email", props=("key", "occurred_at", "content_purged_at")),
            grant("READ", "Tag", props=("label", "current")),
            grant("READ", "Flag", props=("label", "active", "expires_at")),
            grant("READ", "Case", props=("id", "window_from", "window_to")),
            grant("READ", "IngestError", props=("id", "created_at")),
            grant(
                "SET PROPERTY", "Record", "Email", props=(*CONTENT_PROPERTIES, "content_purged_at", "content_purged_by")
            ),
            grant("SET PROPERTY", "IngestError", props=("raw_enc", "raw_purged_at")),
            grant("MATCH", "Activity", "SoftwareAgent"),
            grant("CREATE", "Activity"),
            grant("SET PROPERTY", "Activity"),
            grant("CREATE", *PROVENANCE_RELS, segment="RELATIONSHIPS"),
            deny("DELETE"),
        ),
    ),
)

ROLE_NAMES = tuple(r.name for r in ROLES)


def render(graph: str, roles: tuple[Role, ...] = ROLES) -> list[str]:
    """The full script, as a reviewer would read it."""
    out = []
    for role in roles:
        out.append(f"CREATE ROLE {_ident(role.name)} IF NOT EXISTS")
        out.extend(role.render(graph))
    return out


async def apply(runner: Neo4jRunner, graph: str, roles: tuple[Role, ...] = ROLES) -> dict[str, int]:
    """Reconcile the server's roles with this module. Runs against the ``system`` database."""
    counts = {}
    for role in roles:
        await runner.run_system(f"CREATE ROLE {_ident(role.name)} IF NOT EXISTS")
        current = await runner.run_system(f"SHOW ROLE {_ident(role.name)} PRIVILEGES AS REVOKE COMMANDS")
        for row in current:
            await runner.run_system(row["command"])
        statements = role.render(graph)
        for stmt in statements:
            await runner.run_system(stmt)
        counts[role.name] = len(statements)
    return counts
