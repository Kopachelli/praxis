"""Approval-gated execution lifecycle tests [FR-6, FR-8]."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.agent.execution_runtime import ApprovedExecutionRunner
from app.agent.executor import PlanExecutor
from app.agent.plans import RemediationPlan, RemediationStep, RiskLevel
from app.agent.runtime import LifecycleJobContext, LifecycleTaskManager
from app.agent.tools.registry import build_tool_registry
from app.incidents import ApprovalDecision, IncidentState, IncidentStore, Severity
from app.trail import TrailEntryType


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _approved_store(
    *,
    store: IncidentStore | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> tuple[IncidentStore, str]:
    active_store = store or IncidentStore(600)
    plan_steps = steps or [
        {
            "seq": 1,
            "action": "Restart isolated target",
            "tool": "restart_service",
            "args": {"service": "checkout-service"},
            "risk_level": RiskLevel.SAFE,
            "rollback": "Target recycles automatically",
        }
    ]
    incident, _ = active_store.create_or_get(
        source="sentry",
        raw_payload={},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="timeout",
        title="Checkout timeout",
        idempotency_key="runtime",
    )
    active_store.transition(incident.id, IncidentState.TRIAGED)
    active_store.store_plan(
        incident.id,
        RemediationPlan.model_validate(
            {"steps": plan_steps},
            context={
                "registered_tools": {step["tool"] for step in plan_steps}
            },
        ),
        trace_id="0" * 32,
    )
    active_store.approve_for_execution(incident.id, operator="demo-operator")
    return active_store, incident.id


class _FailingResultTrailStore(IncidentStore):
    """Inject one result-write failure without weakening approval storage."""

    def append_trail(
        self,
        incident_id: str,
        entry_type: TrailEntryType,
        content: Any,
        *,
        model_used: str | None = None,
        tokens: int | None = None,
    ) -> Any:
        if (
            entry_type is TrailEntryType.EXECUTION
            and isinstance(content, dict)
            and content.get("status") == "succeeded"
        ):
            raise RuntimeError("result trail unavailable")
        return super().append_trail(
            incident_id,
            entry_type,
            content,
            model_used=model_used,
            tokens=tokens,
        )


def test_approved_runner_records_result_and_resolves() -> None:
    store, incident_id = _approved_store()

    async def restart(
        context: Any, target: str, *, record_intent: Any = None
    ) -> dict[str, Any]:
        assert context.incident_id == incident_id
        assert target == "praxis-demo-target"
        if record_intent is not None:
            record_intent("a" * 32)  # ADR-028: intent before the external POST
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "target": "praxis-demo-target",
            "status": "restarted",
            "previous_boot_id": "a" * 32,
            "current_boot_id": "b" * 32,
        }

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    _run(runner.run(incident_id, "trace-approved"))

    assert store.get(incident_id).state is IncidentState.RESOLVED
    execution = [
        entry
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    statuses = [entry.content.get("status") for entry in execution]
    # ADR-028: the step succeeded, and the typed operation result was recorded
    # as the final execution entry with the verified new boot id.
    assert "succeeded" in statuses
    assert execution[-1].content["status"] == "verified_succeeded"
    assert execution[-1].content["current_boot_id"] == "b" * 32
    assert execution[-1].content["previous_boot_id"] == "a" * 32
    assert execution[-1].content["trace_id"] == "trace-approved"
    assert execution[-1].content["operation_id"]


def test_approved_runner_fails_closed_when_real_dispatch_disabled() -> None:
    """PRAXIS-146: an approved real restart fails closed to AWAITING_APPROVAL
    while ADR-028 reconciliation is unimplemented, never dispatching [ADR-024]."""

    store, incident_id = _approved_store()
    calls: list[str] = []

    async def restart(context: Any, target: str) -> dict[str, Any]:
        calls.append(target)
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "target": "praxis-demo-target",
            "status": "restarted",
            "previous_boot_id": "a" * 32,
            "current_boot_id": "b" * 32,
        }

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    _run(runner.run(incident_id, "trace-fail-closed"))

    # The real adapter was never invoked and the fixed fail-closed transition
    # preserves the immutable approval without authorizing another run.
    assert calls == []
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    latest = store.latest_approval(incident_id)
    assert latest is not None and latest.decision is ApprovalDecision.APPROVE
    execution = [
        entry.content
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    assert execution[-1]["status"] == "failed"
    reasons = [
        entry.get("output", {}).get("reason")
        for entry in execution
        if isinstance(entry.get("output"), dict)
    ]
    assert "policy_rejected" in reasons


def test_execution_runner_fences_late_mutation_after_revocation() -> None:
    """PRAXIS-145: once the whole-job deadline revokes the job, a surviving
    operation cannot dispatch or mutate; it fails closed [ADR-024]."""

    store, incident_id = _approved_store()
    calls: list[str] = []

    async def restart(context: Any, target: str) -> dict[str, Any]:
        calls.append(target)
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "target": "praxis-demo-target",
            "status": "restarted",
            "previous_boot_id": "a" * 32,
            "current_boot_id": "b" * 32,
        }

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    context = LifecycleJobContext(
        LifecycleTaskManager(store, logging.getLogger("fence")),
        "token-fence",
    )
    context._revoke()

    with pytest.raises(RuntimeError, match="deadline has expired"):
        _run(runner.run_with_lifecycle(incident_id, "trace-revoked", context))

    # The real adapter never ran, and a revoked runner performs no state change
    # of its own: the lifecycle deadline's fixed disposition is authoritative, so
    # the runner must neither transition nor append a late execution entry.
    assert calls == []
    assert store.get(incident_id).state is IncidentState.EXECUTING
    execution = [
        entry
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    assert execution == []


def test_approved_runner_failure_returns_to_approval_without_leaking_error() -> None:
    store, incident_id = _approved_store()
    secret = "adapter-secret-sentinel"

    async def restart(context: Any, target: str) -> None:
        del context, target
        raise RuntimeError(secret)

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    _run(runner.run(incident_id, "trace-failed"))

    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    rendered = str(store.trail_store.list_for_incident(incident_id))
    assert secret not in rendered
    assert "failed" in rendered


def test_approved_runner_records_result_before_the_next_attempt() -> None:
    store, incident_id = _approved_store(
        steps=[
            {
                "seq": 1,
                "action": "Render the configuration change as a dry run",
                "tool": "update_config",
                "args": {"key": "gateway_timeout", "value": 60},
                "risk_level": RiskLevel.CAUTION,
                "rollback": "Restore the prior timeout",
            },
            {
                "seq": 2,
                "action": "Restart isolated target",
                "tool": "restart_service",
                "args": {"service": "checkout-service"},
                "risk_level": RiskLevel.SAFE,
                "rollback": "Target recycles automatically",
            },
        ]
    )

    async def restart(
        context: Any, target: str, *, record_intent: Any = None
    ) -> dict[str, Any]:
        assert context.incident_id == incident_id
        assert target == "praxis-demo-target"
        if record_intent is not None:
            record_intent("a" * 32)
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "target": "praxis-demo-target",
            "status": "restarted",
            "previous_boot_id": "a" * 32,
            "current_boot_id": "b" * 32,
        }

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    _run(runner.run(incident_id, "trace-ordered"))

    execution = [
        entry.content
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    # ADR-028: the real restart (seq 2) records intent before dispatch and a typed
    # verified result after, around the ordinary attempt/succeeded pair.
    assert [(entry.get("seq"), entry["status"]) for entry in execution] == [
        (1, "attempted"),
        (1, "succeeded"),
        (2, "attempted"),
        (2, "recorded"),
        (2, "succeeded"),
        (2, "verified_succeeded"),
    ]
    assert store.get(incident_id).state is IncidentState.RESOLVED


def test_result_trail_failure_blocks_the_next_state_changing_step() -> None:
    store, incident_id = _approved_store(
        store=_FailingResultTrailStore(600),
        steps=[
            {
                "seq": seq,
                "action": f"Restart isolated target attempt {seq}",
                "tool": "restart_service",
                "args": {"service": "checkout-service"},
                "risk_level": RiskLevel.SAFE,
                "rollback": "Target recycles automatically",
            }
            for seq in (1, 2)
        ],
    )
    restart_calls: list[str] = []

    async def restart(
        context: Any, target: str, *, record_intent: Any = None
    ) -> dict[str, Any]:
        del context, target
        if record_intent is not None:
            record_intent("a" * 32)
        restart_calls.append("restart")
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "target": "praxis-demo-target",
            "status": "restarted",
            "previous_boot_id": "a" * 32,
            "current_boot_id": "b" * 32,
        }

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    with pytest.raises(RuntimeError, match="result trail unavailable"):
        _run(runner.run(incident_id, "trace-trail-failed"))

    # ADR-028: the real restart dispatched (intent recorded) but its result could
    # not be durably stored, so the incident fails closed to RECONCILIATION_REQUIRED
    # rather than re-opening for another dispatch; the second step never runs.
    assert restart_calls == ["restart"]
    assert store.get(incident_id).state is IncidentState.RECONCILIATION_REQUIRED
    execution = [
        entry.content
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    assert execution[0]["seq"] == 1
    assert execution[0]["status"] == "attempted"
    assert execution[-1]["status"] == "reconciliation_required"
    assert all(entry.get("seq") != 2 for entry in execution)


def test_failed_dispatch_after_intent_requires_reconciliation() -> None:
    """ADR-028: once the adapter records intent (reaches the boundary) and then
    fails, the outcome is uncertain — reconcile, never re-open for re-dispatch."""

    store, incident_id = _approved_store()

    async def restart(
        context: Any, target: str, *, record_intent: Any = None
    ) -> dict[str, Any]:
        del context, target
        if record_intent is not None:
            record_intent("a" * 32)  # intent durably recorded; boundary reached
        raise RuntimeError("isolated target disconnected after the restart request")

    runner = ApprovedExecutionRunner(
        store,
        PlanExecutor(
            build_tool_registry(
                restart_handler=restart,
                restart_target="praxis-demo-target",
                real_dispatch_enabled=True,
            )
        ),
        logging.getLogger("test-execution-runtime"),
    )

    _run(runner.run(incident_id, "trace-uncertain"))

    assert store.get(incident_id).state is IncidentState.RECONCILIATION_REQUIRED
    latest = store.latest_approval(incident_id)
    assert latest is not None and latest.decision is ApprovalDecision.APPROVE
    execution = [
        entry.content
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
    ]
    assert any(entry.get("status") == "reconciliation_required" for entry in execution)
