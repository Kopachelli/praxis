"""ADR-024 process admission, FIFO, expiry, and fail-closed tests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from app.agent.execution_runtime import ApprovedExecutionRunner
from app.agent.executor import PlanExecutor
from app.agent.plans import RemediationPlan, RiskLevel
from app.agent.tools.registry import build_tool_registry
from app.agent.runtime import (
    AgentTaskManager,
    LifecycleJobContext,
    LifecycleJobKind,
    LifecycleTaskManager,
    LIFECYCLE_CAPACITY_ERROR_DETAIL,
    REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY,
)
from app.config import get_settings
from app.incidents import (
    Approval,
    ApprovalDecision,
    IncidentState,
    IncidentStore,
    LIFECYCLE_EXECUTION_TIMEOUT_NOTE,
    Severity,
)
from app.trail import TrailEntryType
from app.main import create_app


_LOGGER = logging.getLogger("praxis.test.lifecycle")
_WEBHOOK_SECRET = "lifecycle-webhook-signing-secret"
_OPERATOR_TOKEN = "lifecycle-operator-token-0123456789abcdef"


def _incident(store: IncidentStore, key: str) -> str:
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload={},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="timeout",
        title="Checkout timeout",
        idempotency_key=key,
    )
    return incident.id


def _plan() -> RemediationPlan:
    return RemediationPlan.model_validate(
        {
            "steps": [
                {
                    "seq": 1,
                    "action": "Restart isolated target",
                    "tool": "restart_service",
                    "args": {"service": "checkout-service"},
                    "risk_level": RiskLevel.SAFE,
                    "rollback": "Target recycles automatically",
                }
            ]
        },
        context={"registered_tools": {"restart_service"}},
    )


def _awaiting(store: IncidentStore, key: str) -> str:
    incident_id = _incident(store, key)
    store.transition(incident_id, IncidentState.TRIAGED)
    store.store_plan(incident_id, _plan(), trace_id="plan-trace")
    return incident_id


async def _wait_inactive(manager: AgentTaskManager, incident_id: str) -> None:
    for _ in range(200):
        if not manager.is_active(incident_id):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("lifecycle job did not become inactive")


def test_one_running_three_pending_fifo_and_per_incident_coalescing() -> None:
    class OrderedAgent:
        def __init__(self, incident_ids: list[str]) -> None:
            self.calls: list[str] = []
            self.started = {item: asyncio.Event() for item in incident_ids}
            self.release = {item: asyncio.Event() for item in incident_ids}

        async def run(self, incident_id: str, trace_id: str) -> None:
            del trace_id
            self.calls.append(incident_id)
            self.started[incident_id].set()
            await self.release[incident_id].wait()

    async def exercise() -> None:
        store = IncidentStore(600)
        incident_ids = [_incident(store, f"fifo-{index}") for index in range(5)]
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=2,
            job_timeout_seconds=2,
        )
        agent = OrderedAgent(incident_ids)
        manager = AgentTaskManager(agent, _LOGGER, lifecycle=lifecycle)

        assert manager.schedule(incident_ids[0], "trace-0") is True
        await asyncio.wait_for(agent.started[incident_ids[0]].wait(), timeout=1)
        for index in range(1, 4):
            assert manager.schedule(incident_ids[index], f"trace-{index}") is True
        assert lifecycle.running_count == 1
        assert lifecycle.pending_count == 3
        assert manager.schedule(incident_ids[0], "trace-coalesced") is False
        assert manager.schedule(incident_ids[4], "trace-full") is False

        for index in range(4):
            agent.release[incident_ids[index]].set()
            if index + 1 < 4:
                await asyncio.wait_for(
                    agent.started[incident_ids[index + 1]].wait(),
                    timeout=1,
                )
        await _wait_inactive(manager, incident_ids[3])
        assert agent.calls == incident_ids[:4]
        await manager.shutdown()

    asyncio.run(exercise())


def test_pending_job_expires_without_running_and_keeps_new_state() -> None:
    class FirstBlocks:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, incident_id: str, trace_id: str) -> None:
            del trace_id
            self.calls.append(incident_id)
            self.started.set()
            await self.release.wait()

    async def exercise() -> None:
        store = IncidentStore(600)
        first = _incident(store, "pending-first")
        expired = _incident(store, "pending-expired")
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=0.05,
            job_timeout_seconds=1,
        )
        agent = FirstBlocks()
        manager = AgentTaskManager(agent, _LOGGER, lifecycle=lifecycle)

        assert manager.schedule(first, "trace-first") is True
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        assert manager.schedule(expired, "trace-expired") is True
        await _wait_inactive(manager, expired)

        assert agent.calls == [first]
        assert store.get(expired).state is IncidentState.NEW
        lifecycle_entries = [
            entry
            for entry in store.trail_store.list_for_incident(expired)
            if entry.type is TrailEntryType.LIFECYCLE
        ]
        assert len(lifecycle_entries) == 1
        assert lifecycle_entries[0].content == {
            "stage": "job_timeout",
            "status": "timed_out",
            "reason": "pending_expired",
            "job_kind": "initial_triage",
            "trace_id": "trace-expired",
        }

        agent.release.set()
        await _wait_inactive(manager, first)
        await manager.shutdown()

    asyncio.run(exercise())


def test_running_initial_and_correction_deadlines_restore_fail_closed_states() -> None:
    class PartialAgent:
        def __init__(self, store: IncidentStore) -> None:
            self.store = store

        async def run(self, incident_id: str, trace_id: str) -> None:
            del trace_id
            self.store.transition(incident_id, IncidentState.TRIAGED)
            await asyncio.Event().wait()

        async def regenerate(
            self,
            incident_id: str,
            trace_id: str,
            correction: Approval,
        ) -> None:
            del incident_id, trace_id, correction
            await asyncio.Event().wait()

    async def exercise() -> None:
        store = IncidentStore(600)
        initial = _incident(store, "deadline-initial")
        corrected = _awaiting(store, "deadline-correction")
        _, correction = store.record_decision(
            corrected,
            decision=ApprovalDecision.REJECT,
            operator="demo-operator",
            note="Generate a safer plan",
            trace_id="correction-decision",
        )
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=1,
            job_timeout_seconds=0.05,
        )
        manager = AgentTaskManager(
            PartialAgent(store),
            _LOGGER,
            lifecycle=lifecycle,
        )

        assert manager.schedule(initial, "trace-initial-timeout") is True
        await _wait_inactive(manager, initial)
        assert store.get(initial).state is IncidentState.NEW

        assert manager.schedule_regeneration(
            corrected,
            "trace-correction-timeout",
            correction,
        ) is True
        await _wait_inactive(manager, corrected)
        assert store.get(corrected).state is IncidentState.TRIAGED
        assert store.get_plan(corrected) is None
        assert store.approvals_for_incident(corrected) == [correction]
        for incident_id in (initial, corrected):
            lifecycle_entries = [
                entry
                for entry in store.trail_store.list_for_incident(incident_id)
                if entry.type is TrailEntryType.LIFECYCLE
            ]
            assert len(lifecycle_entries) == 1
        await manager.shutdown()

    asyncio.run(exercise())


def test_predispatch_execution_timeout_requires_a_fresh_approval() -> None:
    class BlockingExecution:
        async def run(self, incident_id: str, trace_id: str) -> None:
            del incident_id, trace_id
            await asyncio.Event().wait()

    async def exercise() -> None:
        store = IncidentStore(600)
        incident_id = _awaiting(store, "deadline-execution")
        store.record_decision(
            incident_id,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
            trace_id="first-approval",
        )
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=1,
            job_timeout_seconds=0.05,
        )
        manager = AgentTaskManager(
            BlockingExecution(),
            _LOGGER,
            lifecycle=lifecycle,
            job_kind=LifecycleJobKind.APPROVED_EXECUTION,
        )

        assert manager.schedule(incident_id, "trace-execution-timeout") is True
        await _wait_inactive(manager, incident_id)
        assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
        assert len(store.approvals_for_incident(incident_id)) == 1
        execution = [
            entry
            for entry in store.trail_store.list_for_incident(incident_id)
            if entry.type is TrailEntryType.EXECUTION
        ]
        assert execution[-1].content == {
            "status": "failed",
            "note": LIFECYCLE_EXECUTION_TIMEOUT_NOTE,
        }

        store.record_decision(
            incident_id,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
            trace_id="fresh-approval",
        )
        assert store.get(incident_id).state is IncidentState.EXECUTING
        assert len(store.approvals_for_incident(incident_id)) == 2
        await manager.shutdown()

    asyncio.run(exercise())


def test_postdispatch_timeout_fails_closed_to_reconciliation() -> None:
    class DispatchedExecution:
        async def run_with_lifecycle(
            self,
            incident_id: str,
            trace_id: str,
            context: LifecycleJobContext,
        ) -> None:
            del incident_id, trace_id
            context.mark_external_dispatch()
            await asyncio.Event().wait()

    async def exercise() -> None:
        store = IncidentStore(600)
        incident_id = _awaiting(store, "deadline-postdispatch")
        store.record_decision(
            incident_id,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
            trace_id="approval",
        )
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=1,
            job_timeout_seconds=0.05,
        )
        manager = AgentTaskManager(
            DispatchedExecution(),  # type: ignore[arg-type]
            _LOGGER,
            lifecycle=lifecycle,
            job_kind=LifecycleJobKind.APPROVED_EXECUTION,
        )

        assert manager.schedule(incident_id, "trace-postdispatch") is True
        await _wait_inactive(manager, incident_id)
        # ADR-028: a deadline that crosses a real dispatch is uncertain, so the
        # incident fails closed to RECONCILIATION_REQUIRED (never re-opened).
        assert store.get(incident_id).state is IncidentState.RECONCILIATION_REQUIRED
        assert REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY is True
        assert lifecycle.real_dispatch_timeout_reconciliation_ready is True
        entries = store.trail_store.list_for_incident(incident_id)
        lifecycle_entries = [
            entry for entry in entries if entry.type is TrailEntryType.LIFECYCLE
        ]
        assert len(lifecycle_entries) == 1
        # The pre-dispatch execution-failure note is never used post-dispatch;
        # a reconciliation-required event is recorded instead.
        assert not any(
            entry.type is TrailEntryType.EXECUTION
            and entry.content.get("note") == LIFECYCLE_EXECUTION_TIMEOUT_NOTE
            for entry in entries
        )
        assert any(
            entry.type is TrailEntryType.EXECUTION
            and entry.content.get("status") == "reconciliation_required"
            for entry in entries
        )
        await manager.shutdown()

    asyncio.run(exercise())


def test_postdispatch_deadline_keeps_execution_blocked_despite_suppressor() -> None:
    """PRAXIS-145 + ADR-028: when the whole-job deadline crosses a real dispatch,
    the incident fails closed to RECONCILIATION_REQUIRED (uncertain outcome). A
    cancellation-suppressing adapter must not re-open it for re-approval."""

    class _Observable:
        def __init__(self, runner: ApprovedExecutionRunner) -> None:
            self._runner = runner
            self.finished = asyncio.Event()

        async def run_with_lifecycle(
            self,
            incident_id: str,
            trace_id: str,
            context: LifecycleJobContext,
        ) -> None:
            try:
                await self._runner.run_with_lifecycle(
                    incident_id, trace_id, context
                )
            finally:
                self.finished.set()

    async def exercise() -> None:
        store = IncidentStore(600)
        incident_id = _awaiting(store, "postdispatch-suppress")
        store.record_decision(
            incident_id,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
            trace_id="approval",
        )
        assert store.get(incident_id).state is IncidentState.EXECUTING

        dispatched = asyncio.Event()

        async def suppressing_restart(
            context: Any, target: str, *, record_intent: Any = None
        ) -> dict[str, Any]:
            del context, target
            if record_intent is not None:
                # ADR-028 intent + ADR-024 dispatch mark, before the boundary.
                record_intent("a" * 32)
            dispatched.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Adversarially survive the deadline and return a success proof.
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
                    restart_handler=suppressing_restart,
                    restart_target="praxis-demo-target",
                    real_dispatch_enabled=True,
                )
            ),
            _LOGGER,
        )
        observable = _Observable(runner)
        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=1,
            job_timeout_seconds=0.05,
        )
        manager = AgentTaskManager(
            observable,
            _LOGGER,
            lifecycle=lifecycle,
            job_kind=LifecycleJobKind.APPROVED_EXECUTION,
        )

        assert manager.schedule(incident_id, "trace-postdispatch-suppress") is True
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        await asyncio.wait_for(observable.finished.wait(), timeout=2)

        # The uncertain post-dispatch outcome fails closed to reconciliation; the
        # immutable approval is retained but does not authorize another run.
        assert (
            store.get(incident_id).state is IncidentState.RECONCILIATION_REQUIRED
        )
        assert len(store.approvals_for_incident(incident_id)) == 1
        entries = store.trail_store.list_for_incident(incident_id)
        lifecycle_entries = [
            entry for entry in entries if entry.type is TrailEntryType.LIFECYCLE
        ]
        assert len(lifecycle_entries) == 1
        failures = [
            entry
            for entry in entries
            if entry.type is TrailEntryType.EXECUTION
            and isinstance(entry.content, dict)
            and entry.content.get("status") == "failed"
        ]
        assert failures == []
        assert any(
            entry.type is TrailEntryType.EXECUTION
            and isinstance(entry.content, dict)
            and entry.content.get("status") == "reconciliation_required"
            for entry in entries
        )
        await manager.shutdown()

    asyncio.run(exercise())


def test_running_deadline_fences_and_releases_a_cancellation_suppressor() -> None:
    """PRAXIS-145: a job that catches CancelledError past the whole-job deadline
    loses admission, cannot block the FIFO, and cannot dispatch [ADR-024]."""

    async def exercise() -> None:
        store = IncidentStore(600)
        zombie = _awaiting(store, "zombie-execution")
        store.record_decision(
            zombie,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
            trace_id="zombie-approval",
        )
        assert store.get(zombie).state is IncidentState.EXECUTING
        follower = _incident(store, "follower")

        lifecycle = LifecycleTaskManager(
            store,
            _LOGGER,
            pending_timeout_seconds=1,
            job_timeout_seconds=0.05,
        )

        started = asyncio.Event()
        released = asyncio.Event()
        follower_ran = asyncio.Event()
        observed: dict[str, Any] = {}

        async def suppressor(context: LifecycleJobContext) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Adversarially refuse to stop at the deadline, then try to
                # cross the real boundary anyway before finally cooperating.
                observed["revoked"] = context.revoked
                try:
                    context.mark_external_dispatch()
                    observed["dispatch"] = "allowed"
                except RuntimeError:
                    observed["dispatch"] = "blocked"
                released.set()
                return

        async def follower_op(context: LifecycleJobContext) -> None:
            del context
            follower_ran.set()

        assert lifecycle.submit(
            zombie,
            LifecycleJobKind.APPROVED_EXECUTION,
            "trace-zombie",
            suppressor,
        ) is True
        await asyncio.wait_for(started.wait(), timeout=1)
        assert lifecycle.submit(
            follower,
            LifecycleJobKind.INITIAL_TRIAGE,
            "trace-follower",
            follower_op,
        ) is True

        # The FIFO advances to the follower even while the zombie op winds down,
        # and the disposition/dispatch fence has already fired.
        await asyncio.wait_for(follower_ran.wait(), timeout=1)
        await asyncio.wait_for(released.wait(), timeout=1)

        assert observed["revoked"] is True
        assert observed["dispatch"] == "blocked"
        assert lifecycle.is_active(zombie) is False
        assert store.get(zombie).state is IncidentState.AWAITING_APPROVAL
        lifecycle_entries = [
            entry
            for entry in store.trail_store.list_for_incident(zombie)
            if entry.type is TrailEntryType.LIFECYCLE
        ]
        assert len(lifecycle_entries) == 1
        assert lifecycle_entries[0].content["reason"] == "job_deadline_exceeded"
        await lifecycle.shutdown()

    asyncio.run(exercise())


class _InertAgent:
    async def run(self, incident_id: str, trace_id: str) -> None:
        del incident_id, trace_id

    async def regenerate(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> None:
        del incident_id, trace_id, correction


def _full_lifecycle(store: IncidentStore) -> LifecycleTaskManager:
    lifecycle = LifecycleTaskManager(store, _LOGGER)
    for index in range(4):
        admission = lifecycle.acquire(
            f"held-{index}",
            LifecycleJobKind.INITIAL_TRIAGE,
            f"held-trace-{index}",
        )
        assert admission.admitted
    assert lifecycle.outstanding_count == 4
    return lifecycle


def _settings():
    return replace(
        get_settings(),
        webhook_signing_secret=_WEBHOOK_SECRET,
        operator_token=_OPERATOR_TOKEN,
        dedup_window_seconds=600,
    )


def _signed_headers(body: bytes, key: str) -> dict[str, str]:
    digest = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Praxis-Signature": f"sha256={digest}",
        "X-Idempotency-Key": key,
    }


def test_full_webhook_queue_mutates_nothing_but_retained_duplicate_bypasses() -> None:
    store = IncidentStore(600)
    duplicate_id = _incident(store, "retained-key")
    lifecycle = _full_lifecycle(store)
    agent_tasks = AgentTaskManager(_InertAgent(), _LOGGER, lifecycle=lifecycle)
    app = create_app(
        _settings(),
        store,
        agent_task_manager=agent_tasks,
        lifecycle_task_manager=lifecycle,
    )
    body = json.dumps(
        {
            "source": "sentry",
            "service": "checkout-service",
            "title": "Checkout timeout",
        },
        separators=(",", ":"),
    ).encode()

    with TestClient(app) as client:
        rejected = client.post(
            "/webhook",
            content=body,
            headers=_signed_headers(body, "new-full-key"),
        )
        duplicate = client.post(
            "/webhook",
            content=body,
            headers=_signed_headers(body, "retained-key"),
        )

    assert rejected.status_code == 503
    assert rejected.json() == {
        "detail": LIFECYCLE_CAPACITY_ERROR_DETAIL,
        "trace_id": rejected.headers["X-Trace-Id"],
    }
    assert store.count() == 1
    assert store.find_retained_duplicate("new-full-key") is None
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["incident_id"] == duplicate_id


def test_full_approval_queue_records_no_approval_or_state_change() -> None:
    store = IncidentStore(600)
    incident_id = _awaiting(store, "full-approval")
    before_trail = store.trail_store.list_for_incident(incident_id)
    lifecycle = _full_lifecycle(store)
    agent_tasks = AgentTaskManager(_InertAgent(), _LOGGER, lifecycle=lifecycle)
    execution_tasks = AgentTaskManager(
        _InertAgent(),
        _LOGGER,
        lifecycle=lifecycle,
        job_kind=LifecycleJobKind.APPROVED_EXECUTION,
    )
    app = create_app(
        _settings(),
        store,
        agent_task_manager=agent_tasks,
        execution_task_manager=execution_tasks,
        lifecycle_task_manager=lifecycle,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": LIFECYCLE_CAPACITY_ERROR_DETAIL,
        "trace_id": response.headers["X-Trace-Id"],
    }
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
    assert store.get_plan(incident_id) == _plan()
    assert store.trail_store.list_for_incident(incident_id) == before_trail
