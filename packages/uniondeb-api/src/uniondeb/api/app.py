"""The FastAPI application: one process serving the gateway API, the review console and the portal.

Domain errors become HTTP statuses in one place, so every route refuses in the same way: a case
that does not exist and a case the caller may not see are both ``404``, and nothing in an error
body says which.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from uniondeb.agents.models import ModelNotConfigured
from uniondeb.api.auth import AuthSettings
from uniondeb.cases import CaseError, ConflictError, NotFound, PermissionDenied, ReasonRequired, SarValidationError
from uniondeb.extract import ExtractError
from uniondeb.store.review import ItemNotFound, ReviewError
from uniondeb.store.tags import TagRuleError
from uniondeb.worker.services import Services, UploadTooLarge

MAX_UPLOAD_BYTES = 512 * 1024 * 1024

ERRORS: tuple[tuple[type[Exception], int], ...] = (
    (ItemNotFound, 404),
    (NotFound, 404),
    (PermissionDenied, 403),
    (ConflictError, 409),
    (SarValidationError, 422),
    (ReasonRequired, 422),
    (UploadTooLarge, 413),
    (ModelNotConfigured, 503),
    (ReviewError, 422),
    (TagRuleError, 422),
    (ExtractError, 422),
    (CaseError, 409),
)


def create_app(
    services: Services | None = None, auth: AuthSettings | None = None, *, max_upload_bytes: int | None = None
) -> FastAPI:
    """Pass ``services`` and ``auth`` (tests), or neither to build both from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            app.state.services = services
            yield
            return
        from uniondeb.core.observe import configure

        configure("uniondeb-api")
        async with Services.from_env() as built:
            app.state.services = built
            yield

    app = FastAPI(title="Uniondeb", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth or AuthSettings.from_env()
    app.state.max_upload_bytes = max_upload_bytes or int(os.environ.get("UNIONDEB_MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES))
    if services is not None:
        app.state.services = services

    def handler(status: int):
        async def respond(request: Request, exc: Exception) -> JSONResponse:
            body: dict = {"detail": str(exc) if status != 404 else "not found"}
            if isinstance(exc, ConflictError):
                body["rules"] = exc.rules
            if isinstance(exc, SarValidationError):
                body["errors"] = exc.errors
            return JSONResponse(body, status_code=status)

        return respond

    for exc_type, status in ERRORS:
        app.add_exception_handler(exc_type, handler(status))
    app.add_exception_handler(ValidationError, handler(422))

    from uniondeb.api import routes, ui

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        return response

    async def to_login(request: Request, exc: Exception) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    app.add_exception_handler(ui._Login, to_login)
    app.include_router(routes.router)
    app.include_router(ui.router)
    return app
