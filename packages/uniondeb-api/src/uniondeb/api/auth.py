"""Authentication: OIDC bearer tokens, verified; roles from a claim; nothing is open by default.

In production the token is an OIDC access token checked against the provider's JWKS, issuer and
audience. For development and tests a shared HS256 secret can sign tokens instead (``mint``). With
neither configured the application refuses to start. The browser UIs send the same token in a
``SameSite=Strict`` cookie, and any cookie-authenticated write must come from the same origin.

Roles: ``uploader``, ``reviewer``, ``compliance``, ``compliance_lead``, ``investigator``,
``nominated_officer``, ``auditor``, ``case_admin`` and ``service`` (agents writing back).
"""

import os
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr

from uniondeb.cases import ROLES as CASE_ROLES
from uniondeb.cases import Actor
from uniondeb.store.review import Reviewer

COOKIE = "uniondeb_token"
ALL_ROLES = frozenset(
    {
        "uploader",
        "reviewer",
        "compliance",
        "compliance_lead",
        "investigator",
        "nominated_officer",
        "auditor",
        "case_admin",
        "service",
    }
)


class Principal(BaseModel):
    sub: str
    name: str = ""
    roles: frozenset[str] = frozenset()
    person_id: str | None = None  # the user's own roster id, for conflict checks

    @property
    def actor(self) -> Actor:
        return Actor(id=self.sub, roles=self.roles & frozenset(CASE_ROLES), person_id=self.person_id)

    @property
    def reviewer(self) -> Reviewer:
        return Reviewer(id=self.sub, roles=self.roles)


class AuthSettings(BaseModel):
    secret: SecretStr | None = None
    jwks_url: str | None = None
    issuer: str | None = None
    audience: str | None = None
    roles_claim: str = "roles"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthSettings:
        env = os.environ if env is None else env
        settings = cls(
            secret=env.get("UNIONDEB_AUTH_SECRET") or None,
            jwks_url=env.get("UNIONDEB_OIDC_JWKS_URL") or None,
            issuer=env.get("UNIONDEB_OIDC_ISSUER") or None,
            audience=env.get("UNIONDEB_OIDC_AUDIENCE") or None,
            roles_claim=env.get("UNIONDEB_OIDC_ROLES_CLAIM", "roles"),
        )
        if not settings.secret and not settings.jwks_url:
            raise RuntimeError("configure UNIONDEB_OIDC_JWKS_URL (production) or UNIONDEB_AUTH_SECRET (development)")
        if settings.secret and len(settings.secret.get_secret_value()) < 32:
            raise RuntimeError("UNIONDEB_AUTH_SECRET must be at least 32 characters")
        return settings

    def decode(self, token: str) -> Principal:
        options: dict[str, Any] = {"require": ["exp", "sub"]}
        kwargs: dict[str, Any] = {"audience": self.audience, "issuer": self.issuer}
        if self.jwks_url:
            key = _jwks(self.jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], options=options, **kwargs)
        else:
            claims = jwt.decode(token, self.secret.get_secret_value(), algorithms=["HS256"], options=options, **kwargs)
        roles = claims.get(self.roles_claim) or []
        roles = roles.split() if isinstance(roles, str) else roles
        return Principal(
            sub=str(claims["sub"]),
            name=str(claims.get("name") or ""),
            roles=frozenset(roles) & ALL_ROLES,
            person_id=claims.get("person_id"),
        )


@lru_cache(maxsize=4)
def _jwks(url: str) -> jwt.PyJWKClient:
    """One client per provider: it caches signing keys, so a request does not mean a fetch."""
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def mint(
    secret: str, sub: str, roles: list[str], *, person_id: str | None = None, ttl_s: int = 3600, **claims: Any
) -> str:
    """A development token. Production tokens come from the OIDC provider."""
    body = {"sub": sub, "roles": roles, "exp": int(time.time()) + ttl_s, **claims}
    if person_id:
        body["person_id"] = person_id
    return jwt.encode(body, secret, algorithm="HS256")


def current(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    from_cookie = token is None and COOKIE in request.cookies
    token = token or request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    if from_cookie and request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is None or origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "cross-origin request refused")
    try:
        return request.app.state.auth.decode(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from exc


def require(*roles: str) -> Callable[..., Principal]:
    """A dependency that admits a principal holding any of ``roles``."""

    def check(principal: Principal = Depends(current)) -> Principal:
        if not principal.roles & set(roles):
            raise HTTPException(403, f"needs one of the roles: {', '.join(sorted(roles))}")
        return principal

    return check
