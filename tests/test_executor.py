"""Sequential approval-gate and execution-result tests [FR-6, FR-8, FR-9]."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agent.executor import (
    ExecutionApprovalRequiredError,
    PlanExecutor,
    StepExecutionStatus,
)
from app.agent.plans import RemediationPlan, parse_remediation_plan_json
from app.agent.tools.registry import (
    IncidentToolContext,
    ToolExecutionMode,
    build_tool_registry,
)

_OLD_BOOT_ID = "a" * 32
_NEW_BOOT_ID = "b" * 32


def _restart_proof() -> dict[str, Any]:
    return {
        "source": "alibaba_function_compute",
        "dry_run": False,
        "target": "praxis-demo-target",
        "status": "restarted",
        "previous_boot_id": _OLD_BOOT_ID,
        "current_boot_id": _NEW_BOOT_ID,
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _context() -> IncidentToolContext:
    return IncidentToolContext(
        incident_id="inc-1",
        source="sentry",
        service="checkout-service",
        severity="high",
        signal="upstream_timeout",
        title="TimeoutError in checkout-service",
        raw_payload={"message": "gateway timed out"},
    )


def _step(
    seq: int,
    *,
    tool: str,
    args: dict[str, Any],
    risk_level: str,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "action": f"Execute remediation step {seq}",
        "tool": tool,
        "args": args,
        "risk_level": risk_level,
        "rollback": "Restore the prior isolated demo state",
    }


def _plan(registry: Any, *steps: dict[str, Any]) -> RemediationPlan:
    plan = parse_remediation_plan_json(
        json.dumps({"steps": list(steps)}),
        registered_tools=registry.names,
    )
    return registry.validate_remediation_plan(plan)


def test_executor_rejects_the_whole_plan_without_approved_mode() -> None:
    calls: list[tuple[str, str]] = []

    async def restart(context: IncidentToolContext, target: str) -> dict[str, Any]:
        calls.append((context.incident_id, target))
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )
    plan = _plan(
        registry,
        _step(
            1,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
    )

    with pytest.raises(ExecutionApprovalRequiredError):
        _run(PlanExecutor(registry).execute(plan, context=_context()))

    assert calls == []


def test_executor_runs_approved_steps_in_exact_sequence() -> None:
    calls: list[tuple[str, str]] = []
    audit_events: list[tuple[str, int]] = []

    async def restart(
        context: IncidentToolContext,
        target: str,
    ) -> dict[str, Any]:
        calls.append((context.incident_id, target))
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )

    def record_attempt(step: Any) -> None:
        audit_events.append(("attempted", step.seq))

    async def record_result(result: Any) -> None:
        audit_events.append((result.status.value, result.seq))

    plan = _plan(
        registry,
        _step(
            1,
            tool="update_config",
            args={"key": "gateway_timeout", "value": 60},
            risk_level="caution",
        ),
        _step(
            2,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
        _step(
            3,
            tool="rollback_deploy",
            args={"service": "checkout-service", "version": "v1"},
            risk_level="dangerous",
        ),
    )

    report = _run(
        PlanExecutor(registry).execute(
            plan,
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
            on_step_start=record_attempt,
            on_step_result=record_result,
        )
    )

    assert report.succeeded is True
    assert report.failed_seq is None
    assert [result.seq for result in report.results] == [1, 2, 3]
    assert [result.tool for result in report.results] == [
        "update_config",
        "restart_service",
        "rollback_deploy",
    ]
    assert [result.dry_run for result in report.results] == [True, False, True]
    assert all(
        result.status is StepExecutionStatus.SUCCEEDED
        for result in report.results
    )
    assert calls == [("inc-1", "praxis-demo-target")]
    assert audit_events == [
        ("attempted", 1),
        ("succeeded", 1),
        ("attempted", 2),
        ("succeeded", 2),
        ("attempted", 3),
        ("succeeded", 3),
    ]


def test_executor_stops_at_first_failure_and_returns_trail_safe_reason() -> None:
    audit_events: list[tuple[str, int]] = []
    registry = build_tool_registry()
    plan = _plan(
        registry,
        _step(
            1,
            tool="update_config",
            args={"key": "timeout", "value": 60},
            risk_level="caution",
        ),
        _step(
            2,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
        _step(
            3,
            tool="scale_service",
            args={"service": "checkout-service", "replicas": 2},
            risk_level="caution",
        ),
    )

    report = _run(
        PlanExecutor(registry).execute(
            plan,
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
            on_step_start=lambda step: audit_events.append(
                ("attempted", step.seq)
            ),
            on_step_result=lambda result: audit_events.append(
                (result.status.value, result.seq)
            ),
        )
    )

    assert report.succeeded is False
    assert report.failed_seq == 2
    assert [result.seq for result in report.results] == [1, 2]
    assert report.results[1].status is StepExecutionStatus.FAILED
    assert report.results[1].output == {
        "status": "failed",
        "reason": "tool_unavailable",
    }
    assert report.results[1].as_trail_content()["status"] == "failed"
    assert audit_events == [
        ("attempted", 1),
        ("succeeded", 1),
        ("attempted", 2),
        ("failed", 2),
    ]


def test_result_callback_failure_blocks_the_next_state_changing_step() -> None:
    calls: list[str] = []

    async def restart(
        context: IncidentToolContext,
        target: str,
    ) -> dict[str, Any]:
        del context, target
        calls.append("restart")
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )
    plan = _plan(
        registry,
        _step(
            1,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
        _step(
            2,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
    )

    def fail_result_recording(result: Any) -> None:
        assert result.seq == 1
        raise RuntimeError("result audit unavailable")

    with pytest.raises(RuntimeError, match="result audit unavailable"):
        _run(
            PlanExecutor(registry).execute(
                plan,
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
                on_step_result=fail_result_recording,
            )
        )

    assert calls == ["restart"]


def test_attempt_callback_failure_dispatches_no_tool_or_fabricated_result() -> None:
    calls: list[str] = []
    result_events: list[int] = []

    async def restart(
        context: IncidentToolContext,
        target: str,
    ) -> dict[str, Any]:
        del context, target
        calls.append("restart")
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )
    plan = _plan(
        registry,
        _step(
            1,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
    )

    def fail_attempt_recording(step: Any) -> None:
        assert step.seq == 1
        raise RuntimeError("attempt audit unavailable")

    with pytest.raises(RuntimeError, match="attempt audit unavailable"):
        _run(
            PlanExecutor(registry).execute(
                plan,
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
                on_step_start=fail_attempt_recording,
                on_step_result=lambda result: result_events.append(result.seq),
            )
        )

    assert calls == []
    assert result_events == []


def test_injected_handler_exception_text_never_enters_report() -> None:
    secret = "provider-secret-sentinel"

    async def failing_restart(
        context: IncidentToolContext,
        target: str,
    ) -> None:
        del context, target
        raise RuntimeError(f"cloud error includes {secret}")

    registry = build_tool_registry(
        restart_handler=failing_restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )
    plan = _plan(
        registry,
        _step(
            1,
            tool="restart_service",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
    )

    report = _run(
        PlanExecutor(registry).execute(
            plan,
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
        )
    )
    rendered = report.model_dump_json()

    assert report.succeeded is False
    assert report.results[0].output["reason"] == "tool_execution_failed"
    assert secret not in rendered
