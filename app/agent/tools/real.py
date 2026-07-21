"""Deterministic read-only tools backed by the current incident [FR-5]."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.redaction import redact_text

MAX_LOG_ENTRIES = 20
MAX_EVIDENCE_TEXT_CHARS = 500

_SAFE_LOG_KEYS = frozenset({"level", "message", "msg", "text", "timestamp"})
_KNOWN_STATUSES = frozenset(
    {
        "available",
        "degraded",
        "down",
        "failing",
        "healthy",
        "impaired",
        "offline",
        "operational",
        "unavailable",
        "unknown",
        "up",
    }
)


class IncidentContext(Protocol):
    """The minimal incident surface read tools are allowed to inspect."""

    service: str
    severity: str
    signal: str
    title: str
    raw_payload: Any


def _scrub_text(value: Any) -> str | None:
    return redact_text(value, max_chars=MAX_EVIDENCE_TEXT_CHARS)


def _log_entry(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        message = _scrub_text(value)
        return {"message": message} if message is not None else None

    if not isinstance(value, Mapping):
        return None

    entry: dict[str, str] = {}
    for key in ("timestamp", "level", "message", "msg", "text"):
        if key not in value or key not in _SAFE_LOG_KEYS:
            continue
        scrubbed = _scrub_text(value[key])
        if scrubbed is None:
            continue
        output_key = "message" if key in {"message", "msg", "text"} else key
        entry.setdefault(output_key, scrubbed)
    return entry or None


def _raw_candidates(raw_payload: Any) -> tuple[list[Any], str]:
    """Return only explicitly allowlisted evidence, never the full payload."""

    if not isinstance(raw_payload, Mapping):
        return [], "normalized_alert"

    direct_logs = raw_payload.get("logs")
    if isinstance(direct_logs, list):
        return direct_logs, "alert_payload.logs"

    extra = raw_payload.get("extra")
    if isinstance(extra, Mapping):
        nested_logs = extra.get("logs")
        if isinstance(nested_logs, list):
            return nested_logs, "alert_payload.extra.logs"

    message = raw_payload.get("message")
    if isinstance(message, str):
        return [message], "alert_payload.message"

    return [], "normalized_alert"


async def fetch_logs(context: IncidentContext, *, service: str) -> dict[str, Any]:
    """Read a bounded, redacted evidence view for the incident's service."""

    candidates, evidence = _raw_candidates(context.raw_payload)
    entries: list[dict[str, str]] = []
    for candidate in candidates[:MAX_LOG_ENTRIES]:
        entry = _log_entry(candidate)
        if entry is not None:
            entries.append(entry)

    if not entries:
        entries = [{"message": _scrub_text(context.title) or "Alert received"}]

    return {
        "source": "incident_context",
        "evidence": evidence,
        "service": service,
        "entries": entries,
        "count": len(entries),
        "truncated": len(candidates) > MAX_LOG_ENTRIES,
    }


async def service_status(
    context: IncidentContext,
    *,
    service: str,
) -> dict[str, Any]:
    """Derive a deterministic status from allowlisted incident evidence."""

    observed_status: str | None = None
    raw_payload = context.raw_payload
    if isinstance(raw_payload, Mapping):
        candidate = raw_payload.get("status")
        if isinstance(candidate, str) and candidate.strip().lower() in _KNOWN_STATUSES:
            observed_status = candidate.strip().lower()

    severity = str(context.severity).lower()
    status = observed_status
    if status is None:
        status = {
            "critical": "down",
            "high": "degraded",
            "medium": "impaired",
            "low": "operational",
        }.get(severity, "unknown")

    return {
        "source": "incident_context",
        "evidence": "alert_payload.status" if observed_status else "normalized_alert",
        "service": service,
        "status": status,
        "severity": severity,
        "signal": context.signal,
    }
