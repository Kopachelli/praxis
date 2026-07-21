"""Process-local incident repository and binding state machine [FR-1..FR-8]."""

from __future__ import annotations

import copy
import hashlib
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Annotated, Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.plans import RemediationPlan, RemediationStep
from app.redaction import (
    MAX_PUBLIC_SERVICE_CHARS,
    MAX_PUBLIC_SIGNAL_CHARS,
    MAX_PUBLIC_SOURCE_CHARS,
    MAX_PUBLIC_TITLE_CHARS,
    redact_text,
)
from app.trail import DecisionTrailEntry, DecisionTrailStore, TrailEntryType


MAX_CORRECTION_NOTE_CHARS = 2_000
MAX_CORRECTION_EDIT_INSTRUCTION_CHARS = 1_000
LIFECYCLE_EXECUTION_TIMEOUT_NOTE = (
    "Approved execution exceeded its lifecycle deadline before external dispatch"
)
_LIFECYCLE_JOB_KINDS = frozenset(
    {
        "initial_triage",
        "correction_regeneration",
        "approved_execution",
    }
)
_LIFECYCLE_TIMEOUT_PHASES = frozenset({"pending", "running"})


class IncidentState(str, Enum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    RESOLVED = "RESOLVED"
    # ADR-028: a real dispatch whose outcome could not be durably verified as
    # succeeded/not-applied lands here and stays. No approval control or
    # execution scheduler is available; it never auto-retries an uncertain write.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class OperationOutcome(str, Enum):
    """ADR-028 typed outcome of one real, state-changing adapter dispatch."""

    VERIFIED_SUCCEEDED = "verified_succeeded"
    VERIFIED_NOT_APPLIED = "verified_not_applied"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class Incident(BaseModel):
    id: str
    source: str
    raw_payload: Any
    service: str
    severity: Severity
    signal: str
    title: str
    idempotency_key: str
    state: IncidentState
    created_at: datetime


class IncidentSummary(BaseModel):
    id: str
    title: str
    service: str
    severity: Severity
    state: IncidentState
    created_at: datetime


CorrectionInstruction = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]


class PlanEdit(BaseModel):
    """One strict, immutable operator instruction for plan regeneration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(strict=True, ge=1)
    instruction: CorrectionInstruction


class Approval(BaseModel):
    """Immutable audit record for an operator decision and any correction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    operator: str
    decision: ApprovalDecision
    note: str | None = None
    edits: tuple[PlanEdit, ...] = ()
    timestamp: datetime


class ExecutionIntent(BaseModel):
    """ADR-028 durable pre-action record of one real state-changing dispatch.

    Persisted before the external boundary is crossed so an uncertain outcome is
    always uniquely identifiable and never silently re-dispatched.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    incident_id: str
    step_seq: int
    tool: str
    target: str
    plan_signature: str
    baseline_boot_id: str
    trace_id: str
    created_at: datetime


class RemediationPlanView(BaseModel):
    status: Literal["proposed"] = "proposed"
    steps: list[RemediationStep]


class IncidentView(BaseModel):
    id: str
    source: str
    service: str
    severity: Severity
    signal: str
    title: str
    state: IncidentState
    created_at: datetime
    memory_match: Any | None = None
    plan: RemediationPlanView | None = None
    trail: list["DecisionTrailEntryView"] = Field(default_factory=list)
    trace_id: str


class DecisionTrailEntryView(BaseModel):
    seq: int
    type: TrailEntryType
    content: Any
    model_used: str | None = None
    tokens: int | None = None
    timestamp: datetime


class IncidentNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass


class ApprovalRequiredError(PermissionError):
    pass


class FailureNoteRequiredError(PermissionError):
    pass


class CorrectionRequiredError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.NEW: frozenset({IncidentState.TRIAGED}),
    IncidentState.TRIAGED: frozenset({IncidentState.AWAITING_APPROVAL}),
    IncidentState.AWAITING_APPROVAL: frozenset(
        {IncidentState.EXECUTING, IncidentState.TRIAGED}
    ),
    IncidentState.EXECUTING: frozenset(
        {
            IncidentState.RESOLVED,
            IncidentState.AWAITING_APPROVAL,
            IncidentState.RECONCILIATION_REQUIRED,
        }
    ),
    IncidentState.RESOLVED: frozenset(),
    # ADR-028: fail-closed terminal in v1. A durable reconciliation workflow out
    # of this state requires ADR-027 and a separately reviewed authenticated path.
    IncidentState.RECONCILIATION_REQUIRED: frozenset(),
}

_OPERATOR_DECISION_TARGETS = {
    ApprovalDecision.APPROVE: IncidentState.EXECUTING,
    ApprovalDecision.REJECT: IncidentState.TRIAGED,
    ApprovalDecision.EDIT: IncidentState.TRIAGED,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_incident_id() -> str:
    return f"inc_{uuid.uuid4().hex}"


_BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _plan_signature(plan: RemediationPlan | None) -> str:
    """Bind an execution intent to the exact validated plan it will run."""

    if plan is None:
        raise InvalidTransitionError(
            "cannot record an operation intent without a stored plan"
        )
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


def _validated_boot_id(value: str) -> str:
    if not isinstance(value, str) or _BOOT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("boot id must be a 32-character lowercase hex string")
    return value


def _redact_correction_text(value: str, *, max_chars: int) -> str:
    """Sanitize one validated correction while retaining its public size bound."""

    redacted = redact_text(value, max_chars=max_chars)
    if redacted is None:
        raise CorrectionRequiredError("correction text must not be empty")
    if len(redacted) > max_chars:
        return f"{redacted[: max_chars - 1]}…"
    return redacted


class IncidentStore:
    """Atomic in-memory incident/dedup repository for the single-instance demo."""

    def __init__(
        self,
        dedup_window_seconds: int,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_incident_id,
        trail_store: DecisionTrailStore | None = None,
    ) -> None:
        if dedup_window_seconds <= 0:
            raise ValueError("dedup_window_seconds must be greater than zero")
        self._dedup_window = timedelta(seconds=dedup_window_seconds)
        self._clock = clock
        self._id_factory = id_factory
        self._trail = trail_store or DecisionTrailStore(clock=clock)
        self._incidents: dict[str, Incident] = {}
        self._idempotency: dict[str, tuple[str, datetime]] = {}
        self._approvals: dict[str, list[Approval]] = {}
        self._plans: dict[str, RemediationPlan | None] = {}
        self._memory_matches: dict[str, dict[str, Any] | None] = {}
        # ADR-028: at most one in-flight real operation intent per incident, plus
        # the set of operation ids whose result was already durably recorded so
        # result persistence is idempotent and never re-dispatches.
        self._operation_intents: dict[str, ExecutionIntent] = {}
        self._completed_operations: set[str] = set()
        self._lock = RLock()

    @property
    def trail_store(self) -> DecisionTrailStore:
        """Expose the single decision trail shared by incident and agent writes."""

        return self._trail

    def create_or_get(
        self,
        *,
        source: str,
        raw_payload: Any,
        service: str,
        severity: Severity,
        signal: str,
        title: str,
        idempotency_key: str,
    ) -> tuple[Incident, bool]:
        """Return `(incident, duplicate)` while atomically enforcing the window."""

        source_value = redact_text(
            source,
            max_chars=MAX_PUBLIC_SOURCE_CHARS,
        ) or "unknown"
        service_value = redact_text(
            service,
            max_chars=MAX_PUBLIC_SERVICE_CHARS,
        ) or "unknown-service"
        signal_value = redact_text(
            signal,
            max_chars=MAX_PUBLIC_SIGNAL_CHARS,
        ) or "generic_alert"
        title_value = redact_text(
            title,
            max_chars=MAX_PUBLIC_TITLE_CHARS,
        ) or f"Alert for {service_value}"

        with self._lock:
            now = self._clock()
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                incident_id, expires_at = existing
                if now < expires_at and incident_id in self._incidents:
                    return self._incidents[incident_id].model_copy(deep=True), True

            incident = Incident(
                id=self._id_factory(),
                source=source_value,
                raw_payload=copy.deepcopy(raw_payload),
                service=service_value,
                severity=severity,
                signal=signal_value,
                title=title_value,
                idempotency_key=idempotency_key,
                state=IncidentState.NEW,
                created_at=now,
            )
            self._incidents[incident.id] = incident
            self._idempotency[idempotency_key] = (
                incident.id,
                now + self._dedup_window,
            )
            self._approvals[incident.id] = []
            self._plans[incident.id] = None
            self._memory_matches[incident.id] = None
            return incident.model_copy(deep=True), False

    def find_retained_duplicate(self, idempotency_key: str) -> Incident | None:
        """Read an unexpired duplicate without reserving or mutating its key."""

        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is None:
                return None
            incident_id, expires_at = existing
            if self._clock() >= expires_at:
                return None
            incident = self._incidents.get(incident_id)
            return incident.model_copy(deep=True) if incident is not None else None

    def get(self, incident_id: str) -> Incident:
        with self._lock:
            return self._get_locked(incident_id).model_copy(deep=True)

    def count(self) -> int:
        with self._lock:
            return len(self._incidents)

    def list_summaries(self) -> list[IncidentSummary]:
        with self._lock:
            ordered = sorted(
                self._incidents.values(),
                key=lambda incident: incident.created_at,
                reverse=True,
            )
            return [
                IncidentSummary(
                    id=incident.id,
                    title=incident.title,
                    service=incident.service,
                    severity=incident.severity,
                    state=incident.state,
                    created_at=incident.created_at,
                )
                for incident in ordered
            ]

    def view(self, incident_id: str, trace_id: str) -> IncidentView:
        with self._lock:
            incident = self._get_locked(incident_id).model_copy(deep=True)
            stored_plan = self._plans[incident_id]
            memory_match = copy.deepcopy(self._memory_matches[incident_id])
            plan = (
                RemediationPlanView(
                    steps=stored_plan.model_copy(deep=True).steps,
                )
                if stored_plan is not None
                else None
            )
            trail = self._trail.list_for_incident(incident.id)

        return IncidentView(
            id=incident.id,
            source=incident.source,
            service=incident.service,
            severity=incident.severity,
            signal=incident.signal,
            title=incident.title,
            state=incident.state,
            created_at=incident.created_at,
            memory_match=memory_match,
            plan=plan,
            trail=[
                DecisionTrailEntryView(
                    seq=entry.seq,
                    type=entry.type,
                    content=entry.content,
                    model_used=entry.model_used,
                    tokens=entry.tokens,
                    timestamp=entry.timestamp,
                )
                for entry in trail
            ],
            trace_id=trace_id,
        )

    def set_memory_match(
        self,
        incident_id: str,
        match: dict[str, Any] | None,
    ) -> None:
        """Store an isolated, public memory-match snapshot for FR-11 reads."""

        if match is not None and not isinstance(match, dict):
            raise TypeError("memory match must be a dict or None")
        with self._lock:
            self._get_locked(incident_id)
            self._memory_matches[incident_id] = copy.deepcopy(match)

    def get_memory_match(self, incident_id: str) -> dict[str, Any] | None:
        """Return an isolated memory-match snapshot for planning and the API."""

        with self._lock:
            self._get_locked(incident_id)
            return copy.deepcopy(self._memory_matches[incident_id])

    def store_plan(
        self,
        incident_id: str,
        plan: RemediationPlan,
        *,
        trace_id: str,
    ) -> Incident:
        """Persist a validated plan and atomically mark its readiness [FR-3]."""

        if not isinstance(plan, RemediationPlan):
            raise TypeError("plan must be a validated RemediationPlan")

        # RemediationPlan is the validation witness produced by the strict parser;
        # raw mappings and other unvalidated values are rejected above.
        validated_plan = plan.model_copy(deep=True)
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")

        with self._lock:
            incident = self._get_locked(incident_id)
            if incident.state is not IncidentState.TRIAGED:
                raise InvalidTransitionError(
                    f"Cannot store plan while {incident.state.value}"
                )

            updated = incident.model_copy(
                update={"state": IncidentState.AWAITING_APPROVAL}
            )
            result = updated.model_copy(deep=True)
            previous_plan = self._plans[incident_id]
            self._plans[incident_id] = validated_plan
            self._incidents[incident_id] = updated
            try:
                self._trail.append(
                    incident_id,
                    TrailEntryType.THOUGHT,
                    {
                        "stage": "plan_ready",
                        "status": "ready",
                        "trace_id": trace_id,
                    },
                )
            except Exception:
                self._plans[incident_id] = previous_plan
                self._incidents[incident_id] = incident
                raise
            return result

    def get_plan(self, incident_id: str) -> RemediationPlan | None:
        """Return an isolated plan snapshot suitable for the gated executor."""

        with self._lock:
            self._get_locked(incident_id)
            plan = self._plans[incident_id]
            return plan.model_copy(deep=True) if plan is not None else None

    def append_trail(
        self,
        incident_id: str,
        entry_type: TrailEntryType,
        content: Any,
        *,
        model_used: str | None = None,
        tokens: int | None = None,
    ) -> DecisionTrailEntry:
        self.get(incident_id)
        return self._trail.append(
            incident_id,
            entry_type,
            content,
            model_used=model_used,
            tokens=tokens,
        )

    def transition(self, incident_id: str, target: IncidentState) -> Incident:
        """Apply a legal transition that needs no plan or operator evidence."""

        with self._lock:
            incident = self._get_locked(incident_id)
            if target not in ALLOWED_TRANSITIONS[incident.state]:
                raise InvalidTransitionError(
                    f"Cannot transition {incident.state.value} to {target.value}"
                )
            if (
                incident.state is IncidentState.TRIAGED
                and target is IncidentState.AWAITING_APPROVAL
            ):
                raise InvalidTransitionError(
                    "Approval review requires store_plan with a validated plan"
                )
            if (
                incident.state is IncidentState.AWAITING_APPROVAL
                and target in _OPERATOR_DECISION_TARGETS.values()
            ):
                raise ApprovalRequiredError(
                    "Operator decisions require record_decision with an Approval record"
                )
            if (
                incident.state is IncidentState.EXECUTING
                and target is IncidentState.AWAITING_APPROVAL
            ):
                raise FailureNoteRequiredError(
                    "Execution failure requires record_execution_failure with a note"
                )
            updated = incident.model_copy(update={"state": target})
            self._incidents[incident_id] = updated
            return updated.model_copy(deep=True)

    def approve_for_execution(
        self,
        incident_id: str,
        *,
        operator: str,
        note: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[Incident, Approval]:
        """Atomically record approval and enter EXECUTING [FR-6, ADR-006]."""

        return self.record_decision(
            incident_id,
            decision=ApprovalDecision.APPROVE,
            operator=operator,
            note=note,
            trace_id=trace_id,
        )

    def record_decision(
        self,
        incident_id: str,
        *,
        decision: ApprovalDecision,
        operator: str,
        note: str | None = None,
        edits: Sequence[PlanEdit] | None = None,
        trace_id: str | None = None,
    ) -> tuple[Incident, Approval]:
        """Atomically audit a decision, transition, and clear corrected plans."""

        if not isinstance(decision, ApprovalDecision):
            raise TypeError("decision must be an ApprovalDecision")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must not be empty")
        operator_value = operator.strip()
        if note is not None and not isinstance(note, str):
            raise TypeError("note must be a string or None")
        note_value = note.strip() if note is not None else None
        edit_values = tuple(edits or ())
        if not all(isinstance(item, PlanEdit) for item in edit_values):
            raise TypeError("edits must contain validated PlanEdit values")
        edit_values = tuple(item.model_copy(deep=True) for item in edit_values)
        if decision in {ApprovalDecision.REJECT, ApprovalDecision.EDIT}:
            if note_value:
                note_value = _redact_correction_text(
                    note_value,
                    max_chars=MAX_CORRECTION_NOTE_CHARS,
                )
            edit_values = tuple(
                PlanEdit(
                    seq=item.seq,
                    instruction=_redact_correction_text(
                        item.instruction,
                        max_chars=MAX_CORRECTION_EDIT_INSTRUCTION_CHARS,
                    ),
                )
                for item in edit_values
            )
        if decision is ApprovalDecision.REJECT and not note_value:
            raise CorrectionRequiredError("reject requires a non-empty note")
        if decision is ApprovalDecision.EDIT and not edit_values:
            raise CorrectionRequiredError("edit requires at least one plan edit")

        with self._lock:
            incident = self._get_locked(incident_id)
            if incident.state is not IncidentState.AWAITING_APPROVAL:
                raise InvalidTransitionError(
                    f"Cannot record {decision.value} while {incident.state.value}"
                )
            if self._plans[incident_id] is None:
                raise InvalidTransitionError(
                    "Cannot record an operator decision without a stored plan"
                )
            target = _OPERATOR_DECISION_TARGETS[decision]
            approval = Approval(
                incident_id=incident_id,
                operator=operator_value,
                decision=decision,
                note=note_value,
                edits=edit_values,
                timestamp=self._clock(),
            )
            self._approvals[incident_id].append(approval)
            updated = incident.model_copy(update={"state": target})
            previous_plan = self._plans[incident_id]
            if decision in {ApprovalDecision.REJECT, ApprovalDecision.EDIT}:
                self._plans[incident_id] = None
            self._incidents[incident_id] = updated
            try:
                trail_content: dict[str, Any] = {
                    "operator": operator_value,
                    "decision": decision.value,
                    "note": note_value,
                    "edits": [item.model_dump(mode="json") for item in edit_values],
                }
                if trace_id:
                    trail_content["trace_id"] = trace_id
                self._trail.append(
                    incident_id,
                    TrailEntryType.APPROVAL,
                    trail_content,
                )
            except Exception:
                self._incidents[incident_id] = incident
                self._plans[incident_id] = previous_plan
                self._approvals[incident_id].pop()
                raise
            return updated.model_copy(deep=True), approval.model_copy(deep=True)

    def approvals_for_incident(self, incident_id: str) -> list[Approval]:
        with self._lock:
            self._get_locked(incident_id)
            return [
                item.model_copy(deep=True) for item in self._approvals[incident_id]
            ]

    def latest_approval(self, incident_id: str) -> Approval | None:
        """Return the latest immutable decision for the execution gate."""

        with self._lock:
            self._get_locked(incident_id)
            approvals = self._approvals[incident_id]
            return approvals[-1].model_copy(deep=True) if approvals else None

    def record_execution_failure(self, incident_id: str, note: str) -> Incident:
        """Record a failure note and atomically return to approval review."""

        failure_note = note.strip()
        if not failure_note:
            raise FailureNoteRequiredError("Execution failure note must not be empty")

        with self._lock:
            incident = self._get_locked(incident_id)
            if incident.state is not IncidentState.EXECUTING:
                raise InvalidTransitionError(
                    f"Cannot record execution failure while {incident.state.value}"
                )
            updated = incident.model_copy(
                update={"state": IncidentState.AWAITING_APPROVAL}
            )
            self._incidents[incident_id] = updated
            try:
                self._trail.append(
                    incident_id,
                    TrailEntryType.EXECUTION,
                    {"status": "failed", "note": failure_note},
                )
            except Exception:
                self._incidents[incident_id] = incident
                raise
            return updated.model_copy(deep=True)

    def record_execution_intent(
        self,
        incident_id: str,
        *,
        step_seq: int,
        tool: str,
        target: str,
        baseline_boot_id: str,
        trace_id: str,
    ) -> ExecutionIntent:
        """ADR-028: record intent durably before a real dispatch crosses its boundary."""

        if not isinstance(step_seq, int) or isinstance(step_seq, bool) or step_seq < 1:
            raise ValueError("step_seq must be a positive integer")
        for name, value in (("tool", tool), ("target", target), ("trace_id", trace_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        baseline = _validated_boot_id(baseline_boot_id)

        with self._lock:
            incident = self._get_locked(incident_id)
            if incident.state is not IncidentState.EXECUTING:
                raise InvalidTransitionError(
                    "execution intent requires the EXECUTING state"
                )
            if self._operation_intents.get(incident_id) is not None:
                raise InvalidTransitionError(
                    "an execution intent is already in flight for this incident"
                )
            signature = _plan_signature(self._plans[incident_id])
            intent = ExecutionIntent(
                operation_id=uuid.uuid4().hex,
                incident_id=incident_id,
                step_seq=step_seq,
                tool=tool,
                target=target,
                plan_signature=signature,
                baseline_boot_id=baseline,
                trace_id=trace_id,
                created_at=self._clock(),
            )
            self._operation_intents[incident_id] = intent
            try:
                self._trail.append(
                    incident_id,
                    TrailEntryType.EXECUTION,
                    {
                        "stage": "operation_intent",
                        "status": "recorded",
                        "operation_id": intent.operation_id,
                        "seq": step_seq,
                        "tool": tool,
                        "target": target,
                        "trace_id": trace_id,
                    },
                )
            except Exception:
                self._operation_intents.pop(incident_id, None)
                raise
            return intent.model_copy(deep=True)

    def active_execution_intent(self, incident_id: str) -> ExecutionIntent | None:
        """Return the in-flight real-operation intent, if any, for recovery checks."""

        with self._lock:
            self._get_locked(incident_id)
            intent = self._operation_intents.get(incident_id)
            return intent.model_copy(deep=True) if intent is not None else None

    def record_operation_result(
        self,
        incident_id: str,
        *,
        operation_id: str,
        outcome: OperationOutcome,
        current_boot_id: str | None,
        trace_id: str,
    ) -> None:
        """ADR-028: idempotently record a typed outcome and consume the intent."""

        if not isinstance(outcome, OperationOutcome):
            raise TypeError("outcome must be an OperationOutcome")
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")

        with self._lock:
            self._get_locked(incident_id)
            if operation_id in self._completed_operations:
                return  # idempotent: never a second result and never a re-dispatch
            intent = self._operation_intents.get(incident_id)
            if intent is None or intent.operation_id != operation_id:
                raise InvalidTransitionError(
                    "no matching in-flight operation intent for this result"
                )
            content: dict[str, Any] = {
                "stage": "operation_result",
                "status": outcome.value,
                "operation_id": operation_id,
                "seq": intent.step_seq,
                "tool": intent.tool,
                "target": intent.target,
                "trace_id": trace_id,
            }
            if outcome is OperationOutcome.VERIFIED_SUCCEEDED:
                content["previous_boot_id"] = intent.baseline_boot_id
                content["current_boot_id"] = _validated_boot_id(current_boot_id)
            self._trail.append(incident_id, TrailEntryType.EXECUTION, content)
            self._operation_intents.pop(incident_id, None)
            self._completed_operations.add(operation_id)

    def record_reconciliation_required(
        self,
        incident_id: str,
        *,
        operation_id: str,
        reason: str,
        trace_id: str,
    ) -> Incident:
        """ADR-028: fail closed to RECONCILIATION_REQUIRED for an uncertain write."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")

        with self._lock:
            incident = self._get_locked(incident_id)
            if incident.state is not IncidentState.EXECUTING:
                raise InvalidTransitionError(
                    f"Cannot require reconciliation while {incident.state.value}"
                )
            updated = incident.model_copy(
                update={"state": IncidentState.RECONCILIATION_REQUIRED}
            )
            self._incidents[incident_id] = updated
            try:
                self._trail.append(
                    incident_id,
                    TrailEntryType.EXECUTION,
                    {
                        "stage": "reconciliation_required",
                        "status": "reconciliation_required",
                        "operation_id": operation_id,
                        "reason": reason,
                        "trace_id": trace_id,
                    },
                )
            except Exception:
                self._incidents[incident_id] = incident
                raise
            return updated.model_copy(deep=True)

    def record_lifecycle_timeout(
        self,
        incident_id: str,
        *,
        job_kind: str,
        phase: str,
        trace_id: str,
        external_dispatch_started: bool,
    ) -> Incident:
        """Append one fixed timeout event and apply ADR-024's disposition."""

        if job_kind not in _LIFECYCLE_JOB_KINDS:
            raise ValueError("job_kind is not lifecycle-owned")
        if phase not in _LIFECYCLE_TIMEOUT_PHASES:
            raise ValueError("phase must be pending or running")
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not isinstance(external_dispatch_started, bool):
            raise TypeError("external_dispatch_started must be a bool")

        with self._lock:
            incident = self._get_locked(incident_id)
            previous_plan = self._plans[incident_id]
            updated = incident
            execution_failed = False
            reconciliation_required = False

            if job_kind == "initial_triage" and incident.state in {
                IncidentState.NEW,
                IncidentState.TRIAGED,
                IncidentState.AWAITING_APPROVAL,
            }:
                updated = incident.model_copy(update={"state": IncidentState.NEW})
                self._plans[incident_id] = None
            elif job_kind == "correction_regeneration" and incident.state in {
                IncidentState.TRIAGED,
                IncidentState.AWAITING_APPROVAL,
            }:
                updated = incident.model_copy(
                    update={"state": IncidentState.TRIAGED}
                )
                self._plans[incident_id] = None
            elif (
                job_kind == "approved_execution"
                and not external_dispatch_started
                and incident.state is IncidentState.EXECUTING
            ):
                updated = incident.model_copy(
                    update={"state": IncidentState.AWAITING_APPROVAL}
                )
                execution_failed = True
            elif (
                job_kind == "approved_execution"
                and external_dispatch_started
                and incident.state is IncidentState.EXECUTING
            ):
                # ADR-028: the whole-job deadline crossed a real dispatch. The
                # outcome is uncertain, so fail closed to reconciliation instead
                # of re-opening the incident for another dispatch.
                updated = incident.model_copy(
                    update={"state": IncidentState.RECONCILIATION_REQUIRED}
                )
                reconciliation_required = True

            self._incidents[incident_id] = updated
            try:
                self._trail.append(
                    incident_id,
                    TrailEntryType.LIFECYCLE,
                    {
                        "stage": "job_timeout",
                        "status": "timed_out",
                        "reason": (
                            "pending_expired"
                            if phase == "pending"
                            else "job_deadline_exceeded"
                        ),
                        "job_kind": job_kind,
                        "trace_id": trace_id,
                    },
                )
                if execution_failed:
                    self._trail.append(
                        incident_id,
                        TrailEntryType.EXECUTION,
                        {
                            "status": "failed",
                            "note": LIFECYCLE_EXECUTION_TIMEOUT_NOTE,
                        },
                    )
                if reconciliation_required:
                    self._trail.append(
                        incident_id,
                        TrailEntryType.EXECUTION,
                        {
                            "stage": "reconciliation_required",
                            "status": "reconciliation_required",
                            "reason": "deadline_crossed_dispatch",
                            "trace_id": trace_id,
                        },
                    )
            except Exception:
                self._incidents[incident_id] = incident
                self._plans[incident_id] = previous_plan
                raise
            return updated.model_copy(deep=True)

    def _get_locked(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise IncidentNotFoundError(incident_id) from exc
