"""Sequential, approval-gated remediation-plan executor [FR-6, FR-8, FR-9]."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.plans import RemediationPlan, RemediationStep, RiskLevel
from app.agent.tools.registry import (
    IncidentToolContext,
    ToolArgumentError,
    ToolExecutionMode,
    ToolPolicyError,
    ToolRegistry,
    ToolRegistryError,
    ToolUnavailableError,
    UnknownToolError,
)


class ExecutionApprovalRequiredError(PermissionError):
    """Execution was attempted without the explicit approved-execution mode."""


class StepExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StepExecutionResult(BaseModel):
    """One secret-safe result that can be appended directly to the trail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(strict=True, ge=1)
    tool: str
    risk_level: RiskLevel
    status: StepExecutionStatus
    dry_run: bool
    source: str
    output: dict[str, Any]

    def as_trail_content(self) -> dict[str, Any]:
        """Render the exact JSON-safe content for an execution trail entry."""

        return self.model_dump(mode="json")


class ExecutionReport(BaseModel):
    """Ordered results for an approved plan, ending at the first failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    succeeded: bool
    results: tuple[StepExecutionResult, ...]
    failed_seq: int | None = Field(default=None, strict=True, ge=1)


StepStartCallback = Callable[[RemediationStep], Awaitable[None] | None]
StepResultCallback = Callable[[StepExecutionResult], Awaitable[None] | None]
ExternalDispatchCallback = Callable[[RemediationStep], None]
# ADR-028: (step, baseline_boot_id) -> operation id; records durable intent
# before the real adapter crosses its external boundary.
RealIntentCallback = Callable[[RemediationStep, str], object]


class PlanExecutor:
    """Execute a validated plan serially through the fixed tool registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        self._registry = registry

    async def execute(
        self,
        plan: RemediationPlan,
        *,
        context: IncidentToolContext,
        mode: ToolExecutionMode = ToolExecutionMode.PLANNING,
        on_step_start: StepStartCallback | None = None,
        on_step_result: StepResultCallback | None = None,
        on_external_dispatch: ExternalDispatchCallback | None = None,
        on_real_intent: RealIntentCallback | None = None,
    ) -> ExecutionReport:
        """Run plan steps in supplied sequence only after explicit approval.

        The incident state machine owns the Approval record and may pass
        ``APPROVED_EXECUTION`` only after entering EXECUTING. The secure default
        is PLANNING, which rejects the entire plan before even read tools run.
        """

        if mode is not ToolExecutionMode.APPROVED_EXECUTION:
            raise ExecutionApprovalRequiredError(
                "approved execution mode is required before running a plan"
            )
        if not isinstance(context, IncidentToolContext):
            raise TypeError("context must be an IncidentToolContext")
        if on_step_start is not None and not callable(on_step_start):
            raise TypeError("on_step_start must be callable")
        if on_step_result is not None and not callable(on_step_result):
            raise TypeError("on_step_result must be callable")
        if on_external_dispatch is not None and not callable(on_external_dispatch):
            raise TypeError("on_external_dispatch must be callable")
        if on_real_intent is not None and not callable(on_real_intent):
            raise TypeError("on_real_intent must be callable")

        validated_plan = self._registry.validate_remediation_plan(plan)
        results: list[StepExecutionResult] = []

        for step in validated_plan.steps:
            if on_step_start is not None:
                # Audit admission is a precondition, not a tool outcome. A
                # callback failure must propagate without dispatching the tool
                # or fabricating a failed execution result.
                await _invoke_step_callback(
                    on_step_start,
                    step.model_copy(deep=True),
                    name="on_step_start",
                )
            try:
                tool_result = await self._registry.execute(
                    step.tool,
                    step.args,
                    context=context,
                    mode=ToolExecutionMode.APPROVED_EXECUTION,
                    on_external_dispatch=(
                        lambda current=step.model_copy(deep=True): (
                            on_external_dispatch(current)
                        )
                        if on_external_dispatch is not None
                        else None
                    ),
                    record_intent=(
                        (
                            lambda baseline, current=step.model_copy(deep=True): (
                                on_real_intent(current, baseline)
                            )
                        )
                        if on_real_intent is not None
                        else None
                    ),
                )
                dry_run = tool_result.output.get("dry_run", False)
                if not isinstance(dry_run, bool):
                    raise ToolPolicyError("tool returned an invalid dry-run label")
                if step.risk_level in {RiskLevel.CAUTION, RiskLevel.DANGEROUS}:
                    if dry_run is not True:
                        raise ToolPolicyError("risky tool did not use a dry-run adapter")
                result = StepExecutionResult(
                    seq=step.seq,
                    tool=step.tool,
                    risk_level=step.risk_level,
                    status=StepExecutionStatus.SUCCEEDED,
                    dry_run=dry_run,
                    source=tool_result.source,
                    output=tool_result.output,
                )
            except Exception as error:
                result = _failed_result(step, error)
            results.append(result)
            if on_step_result is not None:
                # This callback is deliberately outside the tool-error handler.
                # If the validated result cannot be durably recorded, abort the
                # complete run before a later state-changing step can begin.
                await _invoke_step_callback(
                    on_step_result,
                    result.model_copy(deep=True),
                    name="on_step_result",
                )
            if result.status is StepExecutionStatus.FAILED:
                return ExecutionReport(
                    incident_id=context.incident_id,
                    succeeded=False,
                    results=tuple(results),
                    failed_seq=step.seq,
                )

        return ExecutionReport(
            incident_id=context.incident_id,
            succeeded=True,
            results=tuple(results),
        )


async def _invoke_step_callback(
    callback: Callable[[Any], Awaitable[None] | None],
    value: Any,
    *,
    name: str,
) -> None:
    """Invoke a synchronous or asynchronous audit callback and fail closed."""

    outcome = callback(value)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if outcome is not None:
        raise TypeError(f"{name} must return None or an awaitable resolving to None")


def _failed_result(
    step: RemediationStep,
    error: Exception,
) -> StepExecutionResult:
    reason = _safe_failure_reason(error)
    dry_run = step.risk_level in {RiskLevel.CAUTION, RiskLevel.DANGEROUS}
    return StepExecutionResult(
        seq=step.seq,
        tool=step.tool,
        risk_level=step.risk_level,
        status=StepExecutionStatus.FAILED,
        dry_run=dry_run,
        source="executor",
        output={"status": "failed", "reason": reason},
    )


def _safe_failure_reason(error: Exception) -> str:
    """Map failures to fixed labels; never persist exception text or repr."""

    if isinstance(error, ToolPolicyError):
        return "policy_rejected"
    if isinstance(error, ToolUnavailableError):
        return "tool_unavailable"
    if isinstance(error, ToolArgumentError):
        return "invalid_arguments"
    if isinstance(error, UnknownToolError):
        return "unknown_tool"
    if isinstance(error, ToolRegistryError):
        return "tool_execution_failed"
    return "tool_execution_failed"
