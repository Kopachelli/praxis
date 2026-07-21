"""Pure-ASGI request-size guards applied before request parsing [NFR-2, NFR-3]."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.operator_auth import (
    log_operator_auth_rejected,
    operator_auth_failure_response,
    raw_operator_authorization_is_valid,
)


class _WebhookPayloadTooLarge(Exception):
    def __init__(self, observed_body_bytes: int, detection: str) -> None:
        self.observed_body_bytes = observed_body_bytes
        self.detection = detection


@dataclass(frozen=True)
class _ContentLengthDecision:
    exceeds_limit: bool
    observed_body_bytes: int | None = None


_MAX_EXACT_LENGTH_DIGITS = 20


class WebhookBodyLimitMiddleware:
    """Limit guarded POST bodies without buffering or trusting invalid lengths."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_approval_body_bytes: int | None = None,
        operator_token: object = "",
        logger: logging.Logger,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be greater than zero")
        if max_approval_body_bytes is not None and max_approval_body_bytes <= 0:
            raise ValueError("max_approval_body_bytes must be greater than zero")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_approval_body_bytes = max_approval_body_bytes
        self.operator_token = operator_token
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = _request_body_limit(
            scope,
            webhook_limit=self.max_body_bytes,
            approval_limit=self.max_approval_body_bytes,
        )
        if limit is None:
            await self.app(scope, receive, send)
            return
        max_body_bytes, rejection_event = limit

        if (
            rejection_event == "approval_payload_too_large"
            and not raw_operator_authorization_is_valid(
                self.operator_token,
                scope.get("headers", []),
            )
        ):
            trace_id = _trace_id(scope)
            log_operator_auth_rejected(self.logger, trace_id)
            response = operator_auth_failure_response(trace_id)
            await response(scope, receive=_never_receive, send=send)
            return

        declared_length = _consistent_content_length(
            scope.get("headers", []),
            comparison_limit=max_body_bytes,
        )
        if declared_length is not None and declared_length.exceeds_limit:
            await self._reject(
                scope,
                send,
                observed_body_bytes=declared_length.observed_body_bytes
                or max_body_bytes + 1,
                detection="content_length",
                max_body_bytes=max_body_bytes,
                rejection_event=rejection_event,
            )
            return

        observed_body_bytes = 0

        async def receive_with_limit() -> Message:
            nonlocal observed_body_bytes
            message = await receive()
            if message["type"] == "http.request":
                observed_body_bytes += len(message.get("body", b""))
                if observed_body_bytes > max_body_bytes:
                    raise _WebhookPayloadTooLarge(
                        observed_body_bytes,
                        "stream",
                    )
            return message

        try:
            await self.app(scope, receive_with_limit, send)
        except _WebhookPayloadTooLarge as exc:
            await self._reject(
                scope,
                send,
                observed_body_bytes=exc.observed_body_bytes,
                detection=exc.detection,
                max_body_bytes=max_body_bytes,
                rejection_event=rejection_event,
            )

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        *,
        observed_body_bytes: int,
        detection: str,
        max_body_bytes: int,
        rejection_event: str,
    ) -> None:
        trace_id = _trace_id(scope)
        self.logger.warning(
            rejection_event,
            extra={
                "incident_id": "-",
                "trace_id": trace_id,
                "max_body_bytes": max_body_bytes,
                "observed_body_bytes": observed_body_bytes,
                "limit_detection": detection,
            },
        )
        response = JSONResponse(
            status_code=413,
            content={"detail": "Payload too large", "trace_id": trace_id},
            headers={"X-Trace-Id": trace_id},
        )
        await response(scope, receive=_never_receive, send=send)


def _is_webhook_post(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and scope.get("path") == "/webhook"
    )


def _request_body_limit(
    scope: Scope,
    *,
    webhook_limit: int,
    approval_limit: int | None,
) -> tuple[int, str] | None:
    if _is_webhook_post(scope):
        return webhook_limit, "webhook_payload_too_large"
    if approval_limit is None:
        return None
    if scope["type"] != "http" or scope.get("method") != "POST":
        return None
    parts = scope.get("path", "").split("/")
    if (
        len(parts) == 4
        and parts[0] == ""
        and parts[1] == "incidents"
        and bool(parts[2])
        and parts[3] == "approve"
    ):
        return approval_limit, "approval_payload_too_large"
    return None


def _consistent_content_length(
    headers: list[tuple[bytes, bytes]],
    *,
    comparison_limit: int,
) -> _ContentLengthDecision | None:
    """Compare equal numeric values without parsing an unbounded integer.

    Decimal tokens are normalized and compared as bytes. Only a bounded, ordinary
    declared size is converted for exact safe logging; pathological values use the
    ``limit + 1`` sentinel and never reach ``int()``.
    """

    normalized_value: bytes | None = None
    for name, raw_value in headers:
        if name.lower() != b"content-length":
            continue
        for raw_item in raw_value.split(b","):
            item = raw_item.strip()
            if not item or not item.isdigit():
                return None
            item = item.lstrip(b"0") or b"0"
            if normalized_value is None:
                normalized_value = item
            elif item != normalized_value:
                return None
    if normalized_value is None:
        return None

    limit_digits = str(comparison_limit).encode("ascii")
    exceeds_limit = len(normalized_value) > len(limit_digits) or (
        len(normalized_value) == len(limit_digits)
        and normalized_value > limit_digits
    )
    if not exceeds_limit:
        return _ContentLengthDecision(exceeds_limit=False)
    observed_body_bytes = (
        int(normalized_value)
        if len(normalized_value) <= _MAX_EXACT_LENGTH_DIGITS
        else comparison_limit + 1
    )
    return _ContentLengthDecision(
        exceeds_limit=True,
        observed_body_bytes=observed_body_bytes,
    )


def _trace_id(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        trace_id = uuid.uuid4().hex
        state["trace_id"] = trace_id
    return trace_id


async def _never_receive() -> Message:
    raise RuntimeError("response receive must not be called")
