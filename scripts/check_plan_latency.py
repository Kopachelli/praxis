"""Verify NFR-2 triage-to-plan latency from exported incident views."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TARGET_SECONDS = 30.0
MIN_SAMPLES = 5
MAX_SAMPLES = 50
MAX_EVIDENCE_BYTES = 1_048_576
MAX_TRAIL_ENTRIES = 1_000
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_INCIDENT_ID = re.compile(r"^inc_[0-9a-f]{32}$")
_MEASURABLE_STATES = frozenset(
    {"AWAITING_APPROVAL", "EXECUTING", "RESOLVED"}
)
_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_RISK_LEVELS = frozenset({"safe", "caution", "dangerous"})
_TRAIL_TYPES = frozenset(
    {
        "thought",
        "tool_call",
        "tool_result",
        "approval",
        "fallback",
        "qwen_attempt",
        "execution",
    }
)
_INCIDENT_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "source",
        "service",
        "severity",
        "signal",
        "title",
        "state",
        "created_at",
        "plan",
        "trail",
        "trace_id",
    }
)
_INCIDENT_OPTIONAL_FIELDS = frozenset({"memory_match"})
_PLAN_FIELDS = frozenset({"status", "steps"})
_STEP_FIELDS = frozenset(
    {"seq", "action", "tool", "args", "risk_level", "rollback"}
)
_TRAIL_REQUIRED_FIELDS = frozenset({"seq", "type", "content", "timestamp"})
_TRAIL_OPTIONAL_FIELDS = frozenset({"model_used", "tokens"})
_CLASSIFICATION_FIELDS = frozenset(
    {"stage", "classification", "provider", "model", "trace_id"}
)
_PLAN_READY_FIELDS = frozenset({"stage", "status", "trace_id"})
_PUBLIC_TEXT_LIMITS = {
    "source": 120,
    "service": 128,
    "signal": 160,
    "title": 1_000,
}


class PlanLatencyEvidenceError(ValueError):
    """Raised when an evidence document cannot prove a bounded measurement."""


@dataclass(frozen=True)
class PlanLatencyResult:
    sample_count: int
    p95_seconds: float

    @property
    def ok(self) -> bool:
        return self.p95_seconds < TARGET_SECONDS


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise PlanLatencyEvidenceError("invalid timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PlanLatencyEvidenceError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanLatencyEvidenceError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or _INCIDENT_ID.fullmatch(value) is None:
        raise PlanLatencyEvidenceError("invalid incident identity")
    return value


def _trace_id(value: Any, *, reason: str) -> str:
    if not isinstance(value, str) or _TRACE_ID.fullmatch(value) is None:
        raise PlanLatencyEvidenceError(reason)
    return value


def _non_empty_text(value: Any, *, reason: str, max_chars: int | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or (max_chars is not None and len(value) > max_chars)
    ):
        raise PlanLatencyEvidenceError(reason)
    return value


def _validate_plan(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise PlanLatencyEvidenceError("invalid plan")
    if value.get("status") != "proposed":
        raise PlanLatencyEvidenceError("invalid plan")
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanLatencyEvidenceError("invalid plan steps")

    for expected_seq, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping) or set(step) != _STEP_FIELDS:
            raise PlanLatencyEvidenceError("invalid plan step")
        if type(step.get("seq")) is not int or step.get("seq") != expected_seq:
            raise PlanLatencyEvidenceError("invalid plan step sequence")
        _non_empty_text(step.get("action"), reason="invalid plan step")
        _non_empty_text(step.get("tool"), reason="invalid plan step")
        _non_empty_text(step.get("rollback"), reason="invalid plan step")
        if not isinstance(step.get("args"), Mapping):
            raise PlanLatencyEvidenceError("invalid plan step")
        if step.get("risk_level") not in _RISK_LEVELS:
            raise PlanLatencyEvidenceError("invalid plan step")


def _validate_trail_entry_shape(entry: Any, expected_seq: int) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise PlanLatencyEvidenceError("invalid trail entry")
    keys = set(entry)
    if (
        not _TRAIL_REQUIRED_FIELDS.issubset(keys)
        or not keys.issubset(_TRAIL_REQUIRED_FIELDS | _TRAIL_OPTIONAL_FIELDS)
    ):
        raise PlanLatencyEvidenceError("invalid trail entry")
    if type(entry.get("seq")) is not int or entry.get("seq") != expected_seq:
        raise PlanLatencyEvidenceError("invalid trail sequence")
    if entry.get("type") not in _TRAIL_TYPES:
        raise PlanLatencyEvidenceError("invalid trail entry")
    model_used = entry.get("model_used")
    if model_used is not None:
        _non_empty_text(model_used, reason="invalid trail entry")
    tokens = entry.get("tokens")
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise PlanLatencyEvidenceError("invalid trail entry")
    return entry


def _plan_ready_timestamp(
    incident: Mapping[str, Any],
    *,
    created_at: datetime,
) -> datetime:
    trail = incident.get("trail")
    if not isinstance(trail, list) or not 1 <= len(trail) <= MAX_TRAIL_ENTRIES:
        raise PlanLatencyEvidenceError("invalid trail")

    classification: tuple[int, str] | None = None
    plan_ready: tuple[int, str, datetime] | None = None
    previous_timestamp = created_at
    for expected_seq, raw_entry in enumerate(trail, start=1):
        entry = _validate_trail_entry_shape(raw_entry, expected_seq)
        timestamp = _timestamp(entry.get("timestamp"))
        if timestamp < previous_timestamp:
            raise PlanLatencyEvidenceError("non-chronological trail")
        previous_timestamp = timestamp

        content = entry.get("content")
        if not isinstance(content, Mapping):
            continue
        stage = content.get("stage")
        if stage == "classification":
            if (
                entry.get("type") != "thought"
                or classification is not None
                or set(content) != _CLASSIFICATION_FIELDS
            ):
                raise PlanLatencyEvidenceError("invalid classification event")
            _non_empty_text(
                content.get("classification"),
                reason="invalid classification event",
            )
            _non_empty_text(
                content.get("provider"),
                reason="invalid classification event",
            )
            _non_empty_text(
                content.get("model"),
                reason="invalid classification event",
            )
            classification = (
                expected_seq,
                _trace_id(
                    content.get("trace_id"),
                    reason="invalid classification event",
                ),
            )
        elif stage == "plan_ready":
            if (
                entry.get("type") != "thought"
                or plan_ready is not None
                or set(content) != _PLAN_READY_FIELDS
                or content.get("status") != "ready"
            ):
                raise PlanLatencyEvidenceError("invalid plan-ready event")
            plan_ready = (
                expected_seq,
                _trace_id(
                    content.get("trace_id"),
                    reason="invalid plan-ready event",
                ),
                timestamp,
            )

    if classification is None:
        raise PlanLatencyEvidenceError("missing classification event")
    if plan_ready is None:
        raise PlanLatencyEvidenceError("missing plan-ready event")
    classification_seq, classification_trace = classification
    ready_seq, ready_trace, ready_timestamp = plan_ready
    if ready_seq <= classification_seq or ready_trace != classification_trace:
        raise PlanLatencyEvidenceError("mixed triage evidence")
    return ready_timestamp


def _latency_sample(incident: Any) -> tuple[str, float]:
    if not isinstance(incident, Mapping):
        raise PlanLatencyEvidenceError("invalid incident")
    keys = set(incident)
    if (
        not _INCIDENT_REQUIRED_FIELDS.issubset(keys)
        or not keys.issubset(_INCIDENT_REQUIRED_FIELDS | _INCIDENT_OPTIONAL_FIELDS)
    ):
        raise PlanLatencyEvidenceError("invalid incident")
    incident_id = _identifier(incident.get("id"))
    for field, max_chars in _PUBLIC_TEXT_LIMITS.items():
        _non_empty_text(
            incident.get(field),
            reason="invalid incident",
            max_chars=max_chars,
        )
    if incident.get("severity") not in _SEVERITIES:
        raise PlanLatencyEvidenceError("invalid incident")
    if incident.get("state") not in _MEASURABLE_STATES:
        raise PlanLatencyEvidenceError("incident is not plan-ready")
    _trace_id(incident.get("trace_id"), reason="invalid incident")
    memory_match = incident.get("memory_match")
    if memory_match is not None and not isinstance(memory_match, Mapping):
        raise PlanLatencyEvidenceError("invalid incident")
    _validate_plan(incident.get("plan"))

    created_at = _timestamp(incident.get("created_at"))
    ready_at = _plan_ready_timestamp(incident, created_at=created_at)
    latency = (ready_at - created_at).total_seconds()
    if not math.isfinite(latency) or latency < 0:
        raise PlanLatencyEvidenceError("invalid latency")
    return incident_id, latency


def evaluate_evidence(payload: Any) -> PlanLatencyResult:
    """Validate a bounded evidence document and calculate nearest-rank p95."""

    if not isinstance(payload, Mapping) or set(payload) != {"incidents"}:
        raise PlanLatencyEvidenceError("invalid evidence envelope")
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        raise PlanLatencyEvidenceError("invalid evidence envelope")
    if not MIN_SAMPLES <= len(incidents) <= MAX_SAMPLES:
        raise PlanLatencyEvidenceError("invalid sample count")

    identities: set[str] = set()
    latencies: list[float] = []
    for incident in incidents:
        incident_id, latency = _latency_sample(incident)
        if incident_id in identities:
            raise PlanLatencyEvidenceError("duplicate incident")
        identities.add(incident_id)
        latencies.append(latency)

    latencies.sort()
    rank = math.ceil(0.95 * len(latencies))
    return PlanLatencyResult(
        sample_count=len(latencies),
        p95_seconds=latencies[rank - 1],
    )


def _output(
    *,
    ok: bool,
    reason: str,
    sample_count: int,
    p95_seconds: float | None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "p95_seconds": (
                round(p95_seconds, 6) if p95_seconds is not None else None
            ),
            "reason": reason,
            "sample_count": sample_count,
            "target_seconds": TARGET_SECONDS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def run(path: Path) -> int:
    """Read one local evidence file and emit only a fixed safe envelope."""

    try:
        if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            raise PlanLatencyEvidenceError("unreadable evidence")
        raw = path.read_bytes()
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise PlanLatencyEvidenceError("unreadable evidence")
        payload = json.loads(raw.decode("utf-8"))
        result = evaluate_evidence(payload)
    except PlanLatencyEvidenceError:
        print(
            _output(
                ok=False,
                reason="invalid_evidence",
                sample_count=0,
                p95_seconds=None,
            )
        )
        return 1
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        print(
            _output(
                ok=False,
                reason="invalid_evidence",
                sample_count=0,
                p95_seconds=None,
            )
        )
        return 1

    reason = "within_target" if result.ok else "threshold_not_met"
    print(
        _output(
            ok=result.ok,
            reason=reason,
            sample_count=result.sample_count,
            p95_seconds=result.p95_seconds,
        )
    )
    return 0 if result.ok else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify NFR-2 from a local exported incident-view envelope."
    )
    parser.add_argument("--input", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv).input)


if __name__ == "__main__":
    raise SystemExit(main())
