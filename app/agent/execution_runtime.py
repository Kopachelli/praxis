"""Lifecycle runner for an already-approved remediation plan [FR-6, FR-8]."""

from __future__ import annotations

import logging

from app.agent.executor import ExecutionReport, PlanExecutor, StepExecutionResult
from app.agent.memory import IncidentMemoryService
from app.agent.plans import RemediationStep, RiskLevel
from app.agent.runtime import LifecycleJobContext
from app.agent.tools.registry import IncidentToolContext, ToolExecutionMode
from app.demo_target import DEMO_TARGET_NAME
from app.incidents import (
    ApprovalDecision,
    ApprovalRequiredError,
    ExecutionIntent,
    IncidentState,
    IncidentStore,
    InvalidTransitionError,
    OperationOutcome,
)
from app.trail import TrailEntryType


def _restart_current_boot_id(report: "ExecutionReport") -> str | None:
    """Return the verified new boot id from a successful real restart step."""

    for result in report.results:
        if result.tool == "restart_service":
            boot_id = result.output.get("current_boot_id")
            if isinstance(boot_id, str):
                return boot_id
    return None


class ApprovedExecutionRunner:
    """Execute only incidents that already crossed the atomic approval gate."""

    def __init__(
        self,
        store: IncidentStore,
        executor: PlanExecutor,
        logger: logging.Logger,
        memory: IncidentMemoryService | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._logger = logger
        self._memory = memory

    async def run(self, incident_id: str, trace_id: str) -> None:
        await self._run(incident_id, trace_id, lifecycle_context=None)

    async def run_with_lifecycle(
        self,
        incident_id: str,
        trace_id: str,
        lifecycle_context: LifecycleJobContext,
    ) -> None:
        """Run while reporting the real-dispatch boundary to ADR-024."""

        if not isinstance(lifecycle_context, LifecycleJobContext):
            raise TypeError("lifecycle_context must be a LifecycleJobContext")
        await self._run(
            incident_id,
            trace_id,
            lifecycle_context=lifecycle_context,
        )

    async def _run(
        self,
        incident_id: str,
        trace_id: str,
        *,
        lifecycle_context: LifecycleJobContext | None,
    ) -> None:
        incident = self._store.get(incident_id)
        if incident.state is not IncidentState.EXECUTING:
            raise InvalidTransitionError(
                f"Cannot execute an incident while {incident.state.value}"
            )
        approval = self._store.latest_approval(incident_id)
        if approval is None or approval.decision is not ApprovalDecision.APPROVE:
            raise ApprovalRequiredError(
                "Latest immutable decision must approve execution"
            )
        plan = self._store.get_plan(incident_id)
        if plan is None:
            self._store.record_execution_failure(
                incident_id,
                "Approved remediation plan is unavailable",
            )
            return

        try:
            async def record_attempt(step: RemediationStep) -> None:
                # Fail closed if the whole-job deadline already fired: a surviving
                # (cancellation-suppressing) operation must not append a late
                # attempt after its disposition was recorded [ADR-024].
                if lifecycle_context is not None:
                    lifecycle_context.raise_if_revoked()
                content = {
                    "seq": step.seq,
                    "tool": step.tool,
                    "risk_level": step.risk_level.value,
                    "status": "attempted",
                    "dry_run": step.risk_level
                    in {RiskLevel.CAUTION, RiskLevel.DANGEROUS},
                    "trace_id": trace_id,
                }
                if step.tool == "restart_service":
                    content["target"] = DEMO_TARGET_NAME
                self._store.append_trail(
                    incident_id,
                    TrailEntryType.EXECUTION,
                    content,
                )

            async def record_result(result: StepExecutionResult) -> None:
                if lifecycle_context is not None:
                    lifecycle_context.raise_if_revoked()
                content = result.as_trail_content()
                content["trace_id"] = trace_id
                self._store.append_trail(
                    incident_id,
                    TrailEntryType.EXECUTION,
                    content,
                )

            def record_real_intent(step: RemediationStep, baseline_boot_id: str) -> str:
                # ADR-028: durably record intent (with the exact pre-action boot-id
                # baseline) before the real adapter crosses its external boundary,
                # and mark the ADR-024 dispatch boundary. The revoked fence keeps a
                # deadline-fired job from dispatching at all.
                if lifecycle_context is not None:
                    lifecycle_context.raise_if_revoked()
                    lifecycle_context.mark_external_dispatch()
                intent = self._store.record_execution_intent(
                    incident_id,
                    step_seq=step.seq,
                    tool=step.tool,
                    target=DEMO_TARGET_NAME,
                    baseline_boot_id=baseline_boot_id,
                    trace_id=trace_id,
                )
                return intent.operation_id

            report = await self._executor.execute(
                plan,
                context=IncidentToolContext.from_incident(incident),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
                on_step_start=record_attempt,
                on_step_result=record_result,
                on_real_intent=record_real_intent,
            )

            intent = self._store.active_execution_intent(incident_id)
            if report.succeeded:
                # A surviving operation must never resolve an incident whose
                # deadline already elapsed and whose disposition already ran.
                if lifecycle_context is not None:
                    lifecycle_context.raise_if_revoked()
                if intent is not None:
                    # ADR-028: a verified real dispatch. Record the typed outcome
                    # (consuming the intent) before resolving.
                    self._store.record_operation_result(
                        incident_id,
                        operation_id=intent.operation_id,
                        outcome=OperationOutcome.VERIFIED_SUCCEEDED,
                        current_boot_id=_restart_current_boot_id(report),
                        trace_id=trace_id,
                    )
                resolved = self._store.transition(incident_id, IncidentState.RESOLVED)
                if self._memory is not None:
                    try:
                        await self._memory.remember_resolution(resolved, trace_id)
                    except Exception as exc:
                        # Persistence is deliberately non-blocking after the
                        # state-changing remediation has already succeeded.
                        self._logger.warning(
                            "memory_write_unavailable",
                            extra={
                                "incident_id": incident_id,
                                "trace_id": trace_id,
                                "error_type": type(exc).__name__,
                            },
                        )
            else:
                # Once the job is revoked, the lifecycle deadline's fixed
                # disposition owns the incident state; the runner must not add a
                # competing transition [ADR-024].
                if lifecycle_context is not None:
                    lifecycle_context.raise_if_revoked()
                self._dispose_failed_execution(
                    incident_id,
                    intent,
                    trace_id,
                    failure_note="Approved remediation stopped after a failed step",
                )
        except Exception as exc:
            # If the whole-job deadline already fired, its fixed disposition is
            # authoritative: a pre-dispatch expiry already returned the incident
            # to AWAITING_APPROVAL, while a post-dispatch expiry deliberately left
            # it EXECUTING for ADR-028 reconciliation. A surviving cancellation
            # suppressor must not re-transition or re-open it here [ADR-024].
            revoked = lifecycle_context is not None and lifecycle_context.revoked
            if not revoked:
                current = self._store.get(incident_id)
                if current.state is IncidentState.EXECUTING:
                    self._dispose_failed_execution(
                        incident_id,
                        self._store.active_execution_intent(incident_id),
                        trace_id,
                        failure_note="Approved remediation failed before completion",
                    )
            self._logger.error(
                "approved_execution_failed",
                extra={
                    "incident_id": incident_id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise

        self._logger.info(
            "approved_execution_finished",
            extra={"incident_id": incident_id, "trace_id": trace_id},
        )

    def _dispose_failed_execution(
        self,
        incident_id: str,
        intent: ExecutionIntent | None,
        trace_id: str,
        *,
        failure_note: str,
    ) -> None:
        """Fail closed after a failed step: reconcile an uncertain real dispatch,
        otherwise return to approval review [ADR-024, ADR-028]."""

        if intent is not None:
            # ADR-028: a real dispatch was in flight and did not verify as
            # succeeded. Never re-open for another dispatch; require reconciliation.
            self._store.record_reconciliation_required(
                incident_id,
                operation_id=intent.operation_id,
                reason="uncertain_real_dispatch",
                trace_id=trace_id,
            )
        else:
            self._store.record_execution_failure(incident_id, failure_note)
