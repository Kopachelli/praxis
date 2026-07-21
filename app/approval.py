"""Human approval and plan-correction endpoint [FR-6..FR-8, ADR-014]."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.agent.runtime import (
    AgentTaskManager,
    LifecycleAdmissionStatus,
    LIFECYCLE_CAPACITY_ERROR_DETAIL,
    LifecycleJobKind,
)
from app.incidents import (
    ApprovalDecision,
    CorrectionRequiredError,
    IncidentNotFoundError,
    IncidentState,
    IncidentStore,
    IncidentView,
    InvalidTransitionError,
    MAX_CORRECTION_EDIT_INSTRUCTION_CHARS,
    MAX_CORRECTION_NOTE_CHARS,
    PlanEdit,
)
from app.operator_auth import OperatorAuthDependency
from app.trail import TrailEntryType

DEMO_OPERATOR = "demo-operator"
_SCHEDULING_FAILURE_NOTE = "Approved execution could not be scheduled"
MAX_APPROVAL_NOTE_CHARS = MAX_CORRECTION_NOTE_CHARS
MAX_APPROVAL_EDITS = 20
MAX_APPROVAL_EDIT_INSTRUCTION_CHARS = MAX_CORRECTION_EDIT_INSTRUCTION_CHARS

OptionalNote = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        max_length=MAX_APPROVAL_NOTE_CHARS,
    ),
]


class ApprovalRequest(BaseModel):
    """Strict public request; the operator identity is deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    note: OptionalNote | None = None
    edits: tuple[PlanEdit, ...] = Field(default=(), max_length=MAX_APPROVAL_EDITS)

    @field_validator("edits")
    @classmethod
    def bound_edit_instructions(
        cls,
        edits: tuple[PlanEdit, ...],
    ) -> tuple[PlanEdit, ...]:
        if any(
            len(edit.instruction) > MAX_APPROVAL_EDIT_INSTRUCTION_CHARS
            for edit in edits
        ):
            raise ValueError(
                "edit instruction must not exceed "
                f"{MAX_APPROVAL_EDIT_INSTRUCTION_CHARS} characters"
            )
        return edits

    @model_validator(mode="after")
    def require_decision_correction(self) -> "ApprovalRequest":
        supplied_note = "note" in self.model_fields_set
        supplied_edits = "edits" in self.model_fields_set
        if self.decision is ApprovalDecision.APPROVE:
            if supplied_note or supplied_edits:
                raise ValueError("approve does not accept note or edits")
        elif self.decision is ApprovalDecision.REJECT:
            if not self.note:
                raise ValueError("reject requires a non-empty note")
            if supplied_edits:
                raise ValueError("reject does not accept edits")
        elif self.decision is ApprovalDecision.EDIT and not self.edits:
            raise ValueError("edit requires at least one plan edit")
        return self


class ExecutionTaskScheduler(Protocol):
    """Narrow integration boundary for the approval-gated executor runtime."""

    def schedule(self, incident_id: str, trace_id: str) -> bool: ...


def build_approval_router(
    store: IncidentStore,
    logger: logging.Logger,
    task_manager: AgentTaskManager,
    execution_scheduler: ExecutionTaskScheduler,
    operator_auth: OperatorAuthDependency,
) -> APIRouter:
    """Build the HITL router with injected lifecycle-owned schedulers."""

    router = APIRouter(dependencies=[Depends(operator_auth)])

    @router.post(
        "/incidents/{incident_id}/approve",
        response_model=IncidentView,
        response_model_exclude_none=True,
        tags=["incidents"],
    )
    async def decide_plan(
        incident_id: str,
        body: ApprovalRequest,
        request: Request,
    ) -> Any:
        trace_id = request.state.trace_id
        try:
            current = store.get(incident_id)
        except IncidentNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": "Incident not found", "trace_id": trace_id},
            )
        if current.state is not IncidentState.AWAITING_APPROVAL:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Incident is not awaiting approval",
                    "trace_id": trace_id,
                },
            )

        request.state.incident_id = incident_id
        job_kind = (
            LifecycleJobKind.APPROVED_EXECUTION
            if body.decision is ApprovalDecision.APPROVE
            else LifecycleJobKind.CORRECTION_REGENERATION
        )
        task_lifecycle = getattr(task_manager, "lifecycle", None)
        execution_lifecycle = getattr(execution_scheduler, "lifecycle", None)
        lifecycle = (
            task_lifecycle
            if task_lifecycle is not None
            and task_lifecycle is execution_lifecycle
            else None
        )
        lease = None
        if lifecycle is not None:
            admission = lifecycle.acquire(incident_id, job_kind, trace_id)
            if admission.status is not LifecycleAdmissionStatus.ADMITTED:
                logger.warning(
                    "lifecycle_admission_rejected",
                    extra={
                        "incident_id": incident_id,
                        "trace_id": trace_id,
                        "job_kind": job_kind.value,
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
                _, approval = store.record_decision(
                    incident_id,
                    decision=body.decision,
                    operator=DEMO_OPERATOR,
                    note=body.note,
                    edits=body.edits,
                    trace_id=trace_id,
                )
            except IncidentNotFoundError:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "Incident not found", "trace_id": trace_id},
                )
            except InvalidTransitionError:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Incident is not awaiting approval",
                        "trace_id": trace_id,
                    },
                )
            except CorrectionRequiredError as exc:
                # Normally rejected by ApprovalRequest; retain a store-level guard
                # for callers that invoke the repository directly.
                return JSONResponse(
                    status_code=422,
                    content={"detail": str(exc), "trace_id": trace_id},
                )

            if lease is not None:
                lease.bind(incident_id)
            response_snapshot = store.view(incident_id, trace_id)

            if body.decision is ApprovalDecision.APPROVE:
                try:
                    scheduled = execution_scheduler.schedule(incident_id, trace_id)
                except Exception as exc:
                    return _execution_schedule_failed(
                        store,
                        logger,
                        incident_id,
                        trace_id,
                        exc,
                    )
                if not scheduled:
                    return _execution_schedule_failed(
                        store,
                        logger,
                        incident_id,
                        trace_id,
                        RuntimeError("scheduler declined work"),
                    )
            else:
                try:
                    scheduled = task_manager.schedule_regeneration(
                        incident_id,
                        trace_id,
                        approval,
                    )
                except Exception as exc:
                    return _regeneration_schedule_failed(
                        store,
                        logger,
                        incident_id,
                        trace_id,
                        exc,
                    )
                if not scheduled:
                    return _regeneration_schedule_failed(
                        store,
                        logger,
                        incident_id,
                        trace_id,
                        RuntimeError("scheduler declined work"),
                    )

            logger.info(
                "operator_decision_recorded",
                extra={"incident_id": incident_id, "trace_id": trace_id},
            )
            return response_snapshot
        finally:
            if lease is not None:
                lease.release()

    return router


def _regeneration_schedule_failed(
    store: IncidentStore,
    logger: logging.Logger,
    incident_id: str,
    trace_id: str,
    error: Exception,
) -> JSONResponse:
    """Fail closed if corrected-plan regeneration cannot enter its runtime."""

    store.append_trail(
        incident_id,
        TrailEntryType.THOUGHT,
        {
            "stage": "plan_regeneration",
            "status": "failed",
            "reason": "scheduling_failed",
            "trace_id": trace_id,
        },
    )
    logger.error(
        "agent_regeneration_not_scheduled",
        extra={
            "incident_id": incident_id,
            "trace_id": trace_id,
            "error_type": type(error).__name__,
        },
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Plan regeneration could not be scheduled",
            "trace_id": trace_id,
        },
    )


def _execution_schedule_failed(
    store: IncidentStore,
    logger: logging.Logger,
    incident_id: str,
    trace_id: str,
    error: Exception,
) -> JSONResponse:
    """Fail closed if approved execution cannot enter its owned task runtime."""

    store.record_execution_failure(incident_id, _SCHEDULING_FAILURE_NOTE)
    logger.error(
        "approved_execution_not_scheduled",
        extra={
            "incident_id": incident_id,
            "trace_id": trace_id,
            "error_type": type(error).__name__,
        },
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Approved execution could not be scheduled",
            "trace_id": trace_id,
        },
    )
