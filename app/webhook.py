"""Signed webhook intake, normalization, and incident reads [FR-1, FR-2]."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agent.memory import MemoryMatch
from app.agent.runtime import (
    AgentTaskManager,
    LifecycleAdmissionStatus,
    LIFECYCLE_CAPACITY_ERROR_DETAIL,
    LifecycleJobKind,
)
from app.config import Settings
from app.incidents import (
    IncidentNotFoundError,
    IncidentStore,
    IncidentSummary,
    IncidentView,
    Severity,
)
from app.operator_auth import ReaderAuthDependency
from app.redaction import (
    MAX_PUBLIC_SERVICE_CHARS,
    MAX_PUBLIC_SIGNAL_CHARS,
    MAX_PUBLIC_SOURCE_CHARS,
    MAX_PUBLIC_TITLE_CHARS,
    redact_text,
)


class WebhookResponse(BaseModel):
    incident_id: str
    state: str
    duplicate: bool
    trace_id: str


class IncidentListResponse(BaseModel):
    incidents: list[IncidentSummary]
    trace_id: str


class MemoryMatchResponse(BaseModel):
    match: MemoryMatch | None
    trace_id: str


class NormalizedAlert(BaseModel):
    source: str
    service: str
    severity: Severity
    signal: str
    title: str


_SEVERITY_MAP = {
    "fatal": Severity.CRITICAL,
    "critical": Severity.CRITICAL,
    "emergency": Severity.CRITICAL,
    "error": Severity.HIGH,
    "high": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "warn": Severity.MEDIUM,
    "medium": Severity.MEDIUM,
    "notice": Severity.LOW,
    "info": Severity.LOW,
    "low": Severity.LOW,
    "debug": Severity.LOW,
}


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify the exact signed body using a constant-time digest comparison."""

    prefix = "sha256="
    if not secret or not signature or not signature.startswith(prefix):
        return False
    encoded_digest = signature[len(prefix) :]
    if len(encoded_digest) != 64:
        return False
    try:
        provided_digest = bytes.fromhex(encoded_digest)
    except ValueError:
        return False
    if len(provided_digest) != hashlib.sha256().digest_size:
        return False
    expected_digest = hmac.digest(secret.encode("utf-8"), raw_body, "sha256")
    return hmac.compare_digest(expected_digest, provided_digest)


def normalize_payload(payload: Any) -> NormalizedAlert:
    """Normalize Sentry fields and apply the accepted generic-alert defaults."""

    body = payload if isinstance(payload, dict) else {}
    source = _text(
        body.get("source"),
        "unknown",
        max_chars=MAX_PUBLIC_SOURCE_CHARS,
    )
    service = _text(
        body.get("service"),
        "unknown-service",
        max_chars=MAX_PUBLIC_SERVICE_CHARS,
    )
    level = _text(
        body.get("severity") or body.get("level"),
        "medium",
        max_chars=32,
    ).lower()
    severity = _SEVERITY_MAP.get(level, Severity.MEDIUM)
    title = _text(
        body.get("title") or body.get("message"),
        f"Alert for {service}",
        max_chars=MAX_PUBLIC_TITLE_CHARS,
    )
    explicit_signal = _text(
        body.get("signal"),
        "",
        max_chars=MAX_PUBLIC_SIGNAL_CHARS,
    )
    signal = explicit_signal or _derive_signal(
        title,
        _text(
            body.get("message"),
            "",
            max_chars=MAX_PUBLIC_TITLE_CHARS,
        ),
    )
    return NormalizedAlert(
        source=source,
        service=service,
        severity=severity,
        signal=signal,
        title=title,
    )


def build_webhook_router(
    settings: Settings,
    store: IncidentStore,
    logger: logging.Logger,
    task_manager: AgentTaskManager,
    reader_auth: ReaderAuthDependency,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/webhook",
        response_model=WebhookResponse,
        status_code=202,
        tags=["incidents"],
    )
    async def receive_webhook(request: Request, response: Response) -> Any:
        trace_id = request.state.trace_id
        raw_body = await request.body()
        if not verify_signature(
            raw_body,
            request.headers.get("x-praxis-signature"),
            settings.webhook_signing_secret,
        ):
            logger.warning(
                "webhook_signature_rejected",
                extra={"incident_id": "-", "trace_id": trace_id},
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid webhook signature", "trace_id": trace_id},
            )

        try:
            payload = json.loads(raw_body)
        except (ValueError, RecursionError):
            return _unparseable_payload(logger, raw_body, trace_id)

        # ``json.loads`` accepts lone UTF-16 surrogate escapes and non-finite
        # floating-point constants.  It can also overflow a finite-looking
        # exponent to infinity.  Reject unsupported scalars before
        # normalization, idempotency reservation, or task startup.
        if _contains_unsupported_json_scalar(payload):
            return _unparseable_payload(logger, raw_body, trace_id)

        try:
            normalized = normalize_payload(payload)
            idempotency_key = request.headers.get(
                "x-idempotency-key"
            ) or hashlib.sha256(raw_body).hexdigest()
        except RecursionError:
            return _unparseable_payload(logger, raw_body, trace_id)

        # A retained duplicate is a read-only response and deliberately bypasses
        # admission, even when the process-wide queue is otherwise full.
        duplicate_incident = store.find_retained_duplicate(idempotency_key)
        if duplicate_incident is not None:
            request.state.incident_id = duplicate_incident.id
            logger.info(
                "webhook_duplicate",
                extra={
                    "incident_id": duplicate_incident.id,
                    "trace_id": trace_id,
                },
            )
            return JSONResponse(
                status_code=200,
                content=WebhookResponse(
                    incident_id=duplicate_incident.id,
                    state=duplicate_incident.state.value,
                    duplicate=True,
                    trace_id=trace_id,
                ).model_dump(mode="json"),
            )

        lifecycle = getattr(task_manager, "lifecycle", None)
        lease = None
        if lifecycle is not None:
            key_digest = hashlib.sha256(
                idempotency_key.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            admission = lifecycle.acquire(
                f"intake:{key_digest}",
                LifecycleJobKind.INITIAL_TRIAGE,
                trace_id,
            )
            if admission.status is not LifecycleAdmissionStatus.ADMITTED:
                logger.warning(
                    "lifecycle_admission_rejected",
                    extra={
                        "incident_id": "-",
                        "trace_id": trace_id,
                        "job_kind": LifecycleJobKind.INITIAL_TRIAGE.value,
                        "admission_status": admission.status.value,
                    },
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": LIFECYCLE_CAPACITY_ERROR_DETAIL,
                        "trace_id": trace_id,
                    },
                )
            lease = admission.lease

        try:
            try:
                incident, duplicate = store.create_or_get(
                    source=normalized.source,
                    raw_payload=payload,
                    service=normalized.service,
                    severity=normalized.severity,
                    signal=normalized.signal,
                    title=normalized.title,
                    idempotency_key=idempotency_key,
                )
            except RecursionError:
                return _unparseable_payload(logger, raw_body, trace_id)
            request.state.incident_id = incident.id
            response.status_code = 200 if duplicate else 202
            if not duplicate:
                if lease is not None:
                    lease.bind(incident.id)
                scheduled = task_manager.schedule(incident.id, trace_id)
                if not scheduled:
                    logger.warning(
                        "agent_run_not_scheduled",
                        extra={"incident_id": incident.id, "trace_id": trace_id},
                    )
        finally:
            if lease is not None:
                lease.release()
        logger.info(
            "webhook_duplicate" if duplicate else "webhook_accepted",
            extra={"incident_id": incident.id, "trace_id": trace_id},
        )
        return WebhookResponse(
            incident_id=incident.id,
            state=incident.state.value,
            duplicate=duplicate,
            trace_id=trace_id,
        )

    @router.get(
        "/incidents",
        response_model=IncidentListResponse,
        tags=["incidents"],
        dependencies=[Depends(reader_auth)],
    )
    async def list_incidents(request: Request) -> IncidentListResponse:
        return IncidentListResponse(
            incidents=store.list_summaries(),
            trace_id=request.state.trace_id,
        )

    @router.get(
        "/incidents/{incident_id}/memory-match",
        response_model=MemoryMatchResponse,
        response_model_exclude_none=False,
        tags=["incidents"],
        dependencies=[Depends(reader_auth)],
    )
    async def get_memory_match(incident_id: str, request: Request) -> Any:
        trace_id = request.state.trace_id
        try:
            match = store.get_memory_match(incident_id)
        except IncidentNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": "Incident not found", "trace_id": trace_id},
            )
        request.state.incident_id = incident_id
        return MemoryMatchResponse(match=match, trace_id=trace_id)

    @router.get(
        "/incidents/{incident_id}",
        response_model=IncidentView,
        response_model_exclude_none=True,
        tags=["incidents"],
        dependencies=[Depends(reader_auth)],
    )
    async def get_incident(incident_id: str, request: Request) -> Any:
        trace_id = request.state.trace_id
        try:
            incident = store.view(incident_id, trace_id)
        except IncidentNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": "Incident not found", "trace_id": trace_id},
            )
        request.state.incident_id = incident.id
        return incident

    return router


def _text(value: Any, default: str, *, max_chars: int) -> str:
    candidate = value if isinstance(value, str) and value.strip() else default
    redacted = redact_text(candidate, max_chars=max_chars)
    return redacted if redacted is not None else default


def _contains_unsupported_json_scalar(payload: Any) -> bool:
    """Return whether the JSON tree contains a non-scalar string or number."""

    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                return True
        elif isinstance(value, float) and not math.isfinite(value):
            return True
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _derive_signal(title: str, message: str) -> str:
    combined = f"{title} {message}".lower()
    if "timeout" in combined or "timed out" in combined:
        return "upstream_timeout"
    if "latency" in combined or "slow" in combined:
        return "high_latency"
    if "unavailable" in combined or "down" in combined:
        return "service_unavailable"
    if "exception" in combined or "error" in combined or "crash" in combined:
        return "application_error"
    return "generic_alert"


def _unparseable_payload(
    logger: logging.Logger,
    raw_body: bytes,
    trace_id: str,
) -> JSONResponse:
    logger.warning(
        "webhook_payload_unparseable",
        extra={
            "incident_id": "-",
            "trace_id": trace_id,
            "raw_body_bytes": len(raw_body),
            "raw_body_sha256": hashlib.sha256(raw_body).hexdigest(),
        },
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Unparseable JSON payload", "trace_id": trace_id},
    )
