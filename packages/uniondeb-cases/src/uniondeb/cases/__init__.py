"""Ymchwiliad's domain layer: cases, flags, the audit log and SAR reporting (plan §7).

The services here are what the investigation portal's HTTP layer calls. They decide who may do
what from the ``Actor`` they are given, write every action (reads included) to a hash-chained
audit log, and keep a subject pseudonymous until someone with access asks for the name and says
why. Build them once per process, sharing one ``AuditLog``::

    audit = AuditLog(runner)
    cases = CaseService(runner, audit, Tarddiad(runner))
    flags = FlagService(runner, audit, cases)
    sars = SarService(runner, audit, cases, flags)

Implements: UND-070, UND-071, UND-072, UND-074
"""

from uniondeb.cases.audit import GENESIS, Anchor, AnchorCheck, AuditEvent, AuditLog, VerifyResult
from uniondeb.cases.cases import OUTCOMES, Case, CaseNote, CaseService, Identity
from uniondeb.cases.flags import Flag, FlagDef, FlagService, FlagVocabulary, PublicFlag
from uniondeb.cases.models import (
    AUDITOR,
    CASE_ADMIN,
    INVESTIGATOR,
    NOMINATED_OFFICER,
    REVIEWER,
    ROLES,
    Actor,
    AuditChainError,
    CaseError,
    ConflictError,
    FlagError,
    NotFound,
    PermissionDenied,
    ReasonRequired,
    SarError,
    SarValidationError,
)
from uniondeb.cases.sar import (
    UNKNOWN,
    MockSarPortal,
    SarClient,
    SarDraft,
    SarGlossary,
    SarPayload,
    SarReceipt,
    SarRoute,
    SarService,
    SarSubject,
    SarSubmission,
    validate_payload,
)

__all__ = [
    "AUDITOR",
    "CASE_ADMIN",
    "GENESIS",
    "INVESTIGATOR",
    "NOMINATED_OFFICER",
    "OUTCOMES",
    "REVIEWER",
    "ROLES",
    "UNKNOWN",
    "Actor",
    "Anchor",
    "AnchorCheck",
    "AuditChainError",
    "AuditEvent",
    "AuditLog",
    "Case",
    "CaseError",
    "CaseNote",
    "CaseService",
    "ConflictError",
    "Flag",
    "FlagDef",
    "FlagError",
    "FlagService",
    "FlagVocabulary",
    "Identity",
    "MockSarPortal",
    "NotFound",
    "PermissionDenied",
    "PublicFlag",
    "ReasonRequired",
    "SarClient",
    "SarDraft",
    "SarError",
    "SarGlossary",
    "SarPayload",
    "SarReceipt",
    "SarRoute",
    "SarService",
    "SarSubject",
    "SarSubmission",
    "SarValidationError",
    "VerifyResult",
    "validate_payload",
]
