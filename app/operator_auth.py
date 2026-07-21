"""Single-operator bearer authentication for incident surfaces [ADR-025]."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import JSONResponse

from app.config import (
    MAX_OPERATOR_TOKEN_LENGTH,
    MIN_OPERATOR_TOKEN_LENGTH,
    require_nontrivial_secret,
)


OPERATOR_AUTH_FAILURE_DETAIL = "Operator authentication required"
OperatorAuthDependency = Callable[..., Awaitable[None]]
ReaderAuthDependency = Callable[..., Awaitable[None]]

OPERATOR_ROLE = "operator"
VIEWER_ROLE = "viewer"

_BEARER_SCHEME = HTTPBearer(
    auto_error=False,
    scheme_name="PraxisOperatorToken",
    description="Strong single-operator bearer token",
)


def operator_token_matches(configured_token: object, supplied_token: object) -> bool:
    """Compare only tokens that pass the bounded visible-ASCII secret policy."""

    try:
        configured = require_nontrivial_secret(
            "PRAXIS_OPERATOR_TOKEN",
            configured_token,
            min_length=MIN_OPERATOR_TOKEN_LENGTH,
            max_length=MAX_OPERATOR_TOKEN_LENGTH,
            http_header_safe=True,
        )
        supplied = require_nontrivial_secret(
            "Authorization bearer token",
            supplied_token,
            min_length=MIN_OPERATOR_TOKEN_LENGTH,
            max_length=MAX_OPERATOR_TOKEN_LENGTH,
            http_header_safe=True,
        )
    except ValueError:
        return False

    return hmac.compare_digest(
        configured.encode("ascii"),
        supplied.encode("ascii"),
    )


def bearer_token_from_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> str | None:
    """Return one syntactically exact ASCII bearer credential, else ``None``."""

    authorization_values = [
        value for name, value in headers if name.lower() == b"authorization"
    ]
    if len(authorization_values) != 1:
        return None
    try:
        value = authorization_values[0].decode("ascii")
    except UnicodeDecodeError:
        return None

    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token:
        return None
    return token


def raw_operator_authorization_is_valid(
    configured_token: object,
    headers: Sequence[tuple[bytes, bytes]],
) -> bool:
    """Validate an ASGI header list without allocating an unbounded credential."""

    supplied_token = bearer_token_from_headers(headers)
    return supplied_token is not None and operator_token_matches(
        configured_token,
        supplied_token,
    )


def log_operator_auth_rejected(
    logger: logging.Logger,
    trace_id: str,
) -> None:
    """Record only a fixed outcome label and server-owned request context."""

    logger.warning(
        "operator_auth_rejected",
        extra={"incident_id": "-", "trace_id": trace_id},
    )


def operator_auth_failure_response(trace_id: str) -> JSONResponse:
    """Build the fixed trace-bearing challenge used at every auth boundary."""

    return JSONResponse(
        status_code=401,
        content={"detail": OPERATOR_AUTH_FAILURE_DETAIL, "trace_id": trace_id},
        headers={
            "WWW-Authenticate": "Bearer",
            "X-Trace-Id": trace_id,
        },
    )


def build_operator_auth_dependency(
    configured_token: object,
    logger: logging.Logger,
) -> OperatorAuthDependency:
    """Create the FastAPI security dependency for protected operator routes."""

    async def require_operator(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER_SCHEME),
        ],
    ) -> None:
        supplied_token = bearer_token_from_headers(request.scope.get("headers", []))
        if (
            credentials is None
            or credentials.scheme.casefold() != "bearer"
            or supplied_token is None
            or not operator_token_matches(configured_token, supplied_token)
        ):
            trace_id = getattr(request.state, "trace_id", "-")
            log_operator_auth_rejected(logger, trace_id)
            raise HTTPException(
                status_code=401,
                detail=OPERATOR_AUTH_FAILURE_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.auth_role = OPERATOR_ROLE

    return require_operator


def resolve_role(
    operator_token: object,
    viewer_token: object,
    supplied_token: object,
) -> str | None:
    """Return the least-privilege role a supplied bearer token authenticates."""

    if operator_token_matches(operator_token, supplied_token):
        return OPERATOR_ROLE
    if viewer_token and operator_token_matches(viewer_token, supplied_token):
        return VIEWER_ROLE
    return None


def build_reader_auth_dependency(
    operator_token: object,
    viewer_token: object,
    logger: logging.Logger,
    *,
    public_demo_reads: bool = False,
) -> ReaderAuthDependency:
    """Accept the operator token or the ADR-029 read-only viewer token for reads.

    The resolved role is stashed on ``request.state.auth_role`` so a session
    endpoint can report it; mutation routes keep the operator-only dependency,
    so a viewer token is rejected there with the same fixed challenge.

    When ``public_demo_reads`` is set (ADR-031), an anonymous or unrecognized
    caller is admitted as a read-only ``viewer`` instead of being rejected, so a
    public demo needs no token at all. A valid operator token is still resolved
    to the operator role, and mutation routes remain operator-only regardless.
    """

    async def require_reader(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER_SCHEME),
        ],
    ) -> None:
        supplied_token = bearer_token_from_headers(request.scope.get("headers", []))
        role = (
            resolve_role(operator_token, viewer_token, supplied_token)
            if credentials is not None
            and credentials.scheme.casefold() == "bearer"
            and supplied_token is not None
            else None
        )
        if role is None:
            if public_demo_reads:
                # ADR-031: least-privilege anonymous read-only demo access.
                request.state.auth_role = VIEWER_ROLE
                return
            trace_id = getattr(request.state, "trace_id", "-")
            log_operator_auth_rejected(logger, trace_id)
            raise HTTPException(
                status_code=401,
                detail=OPERATOR_AUTH_FAILURE_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.auth_role = role

    return require_reader
