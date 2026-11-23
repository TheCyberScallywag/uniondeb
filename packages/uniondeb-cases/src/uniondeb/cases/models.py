"""Actors, roles and the errors the investigation services raise.

An ``Actor`` is the authenticated human a request acts for; the HTTP layer builds it from the
session and the services decide from its roles what it may do. Errors are typed so the layer above
can map them without parsing messages. ``NotFound`` is deliberately raised both for a case that
does not exist and for one the actor may not see, so the portal never confirms a case exists.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

INVESTIGATOR = "investigator"
NOMINATED_OFFICER = "nominated_officer"
AUDITOR = "auditor"
CASE_ADMIN = "case_admin"
REVIEWER = "reviewer"
ROLES = frozenset({INVESTIGATOR, NOMINATED_OFFICER, AUDITOR, CASE_ADMIN, REVIEWER})


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    roles: frozenset[str] = frozenset()
    person_id: str | None = None  # the actor's own roster id, for conflict checks

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, v: frozenset[str]) -> frozenset[str]:
        if unknown := sorted(v - ROLES):
            raise ValueError(f"unknown roles {unknown}; expected a subset of {sorted(ROLES)}")
        return v

    def has(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


class CaseError(Exception):
    """Base of every error raised by ``uniondeb.cases``."""


class NotFound(CaseError):
    """The thing does not exist, or the actor may not know that it does."""


class PermissionDenied(CaseError):
    """The actor's roles do not allow the action."""


class ReasonRequired(CaseError, ValueError):
    """A written reason (or other mandatory text) was missing or blank."""


class ConflictError(CaseError):
    """The investigator may not be assigned to this case; ``rules`` names every rule that fired."""

    def __init__(self, rules: list[str]) -> None:
        self.rules = rules
        super().__init__(f"conflict of interest: {', '.join(rules)}")


class FlagError(CaseError, ValueError):
    """The flag is not in the vocabulary, or is used on the wrong target or without its parameter."""


class SarError(CaseError):
    """The SAR workflow refused the step."""


class SarValidationError(SarError, ValueError):
    """The draft is not a valid ``SarPayload``; ``errors`` has one message per problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class AuditChainError(CaseError):
    """The audit log could not be appended to."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def required(text: str | None, what: str = "reason") -> str:
    if not isinstance(text, str) or not text.strip():
        raise ReasonRequired(f"a written {what} is required")
    return text.strip()
