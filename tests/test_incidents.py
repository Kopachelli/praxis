from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import count
import json

import pytest

from app.agent.plans import RemediationPlan, parse_remediation_plan_json
from app.incidents import (
    ApprovalDecision,
    ApprovalRequiredError,
    CorrectionRequiredError,
    FailureNoteRequiredError,
    IncidentState,
    IncidentStore,
    InvalidTransitionError,
    OperationOutcome,
    PlanEdit,
    Severity,
)
from app.trail import DecisionTrailStore, TrailEntryType


TRACE_ID = "0" * 32


def _create(store: IncidentStore, key: str = "demo-key"):
    return store.create_or_get(
        source="sentry",
        raw_payload={"message": "timeout"},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="upstream_timeout",
        title="TimeoutError in checkout-service",
        idempotency_key=key,
    )


def _plan(action: str = "Restart checkout-service worker pool") -> RemediationPlan:
    return parse_remediation_plan_json(
        json.dumps(
            {
                "steps": [
                    {
                        "seq": 1,
                        "action": action,
                        "tool": "restart_service",
                        "args": {"service": "checkout-service"},
                        "risk_level": "safe",
                        "rollback": "Service auto-recovers; no rollback needed",
                    }
                ]
            }
        ),
        registered_tools={"restart_service"},
    )


def _executing(store: IncidentStore) -> str:
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)
    store.approve_for_execution(incident.id, operator="demo-operator")
    return incident.id


def test_execution_intent_requires_executing_and_is_single() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_intent")
    incident_id = _executing(store)

    intent = store.record_execution_intent(
        incident_id,
        step_seq=1,
        tool="restart_service",
        target="praxis-demo-target",
        baseline_boot_id="a" * 32,
        trace_id=TRACE_ID,
    )
    assert intent.operation_id
    assert store.active_execution_intent(incident_id).operation_id == intent.operation_id

    # Only one real operation may be in flight per incident.
    with pytest.raises(InvalidTransitionError):
        store.record_execution_intent(
            incident_id,
            step_seq=1,
            tool="restart_service",
            target="praxis-demo-target",
            baseline_boot_id="a" * 32,
            trace_id=TRACE_ID,
        )

    # A non-EXECUTING incident cannot record intent.
    other = IncidentStore(600, id_factory=lambda: "inc_intent_new")
    new_incident, _ = _create(other)
    with pytest.raises(InvalidTransitionError):
        other.record_execution_intent(
            new_incident.id,
            step_seq=1,
            tool="restart_service",
            target="praxis-demo-target",
            baseline_boot_id="a" * 32,
            trace_id=TRACE_ID,
        )

    # A malformed boot id is rejected before any incident state is touched.
    with pytest.raises(ValueError):
        store.record_execution_intent(
            incident_id,
            step_seq=1,
            tool="restart_service",
            target="praxis-demo-target",
            baseline_boot_id="not-a-boot-id",
            trace_id=TRACE_ID,
        )


def test_operation_result_is_idempotent_and_consumes_intent() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_result")
    incident_id = _executing(store)
    intent = store.record_execution_intent(
        incident_id,
        step_seq=1,
        tool="restart_service",
        target="praxis-demo-target",
        baseline_boot_id="a" * 32,
        trace_id=TRACE_ID,
    )

    store.record_operation_result(
        incident_id,
        operation_id=intent.operation_id,
        outcome=OperationOutcome.VERIFIED_SUCCEEDED,
        current_boot_id="b" * 32,
        trace_id=TRACE_ID,
    )
    assert store.active_execution_intent(incident_id) is None

    # Idempotent: a retried result write neither raises nor writes a second entry.
    store.record_operation_result(
        incident_id,
        operation_id=intent.operation_id,
        outcome=OperationOutcome.VERIFIED_SUCCEEDED,
        current_boot_id="b" * 32,
        trace_id=TRACE_ID,
    )
    results = [
        entry
        for entry in store.trail_store.list_for_incident(incident_id)
        if entry.type is TrailEntryType.EXECUTION
        and entry.content.get("stage") == "operation_result"
    ]
    assert len(results) == 1
    assert results[0].content["current_boot_id"] == "b" * 32


def test_reconciliation_required_is_terminal() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_reconcile")
    incident_id = _executing(store)
    intent = store.record_execution_intent(
        incident_id,
        step_seq=1,
        tool="restart_service",
        target="praxis-demo-target",
        baseline_boot_id="a" * 32,
        trace_id=TRACE_ID,
    )

    updated = store.record_reconciliation_required(
        incident_id,
        operation_id=intent.operation_id,
        reason="uncertain_real_dispatch",
        trace_id=TRACE_ID,
    )
    assert updated.state is IncidentState.RECONCILIATION_REQUIRED

    # No transition leaves the fail-closed terminal state in v1.
    for target in (
        IncidentState.AWAITING_APPROVAL,
        IncidentState.EXECUTING,
        IncidentState.RESOLVED,
    ):
        with pytest.raises(InvalidTransitionError):
            store.transition(incident_id, target)


def test_dedup_window_uses_original_creation_time_and_expires_at_boundary() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    current = [now]
    ids = iter(["inc_first", "inc_second"])
    store = IncidentStore(
        600,
        clock=lambda: current[0],
        id_factory=lambda: next(ids),
    )

    first, first_duplicate = _create(store)
    current[0] = now + timedelta(seconds=599)
    repeated, repeated_duplicate = _create(store)

    assert first_duplicate is False
    assert repeated_duplicate is True
    assert repeated.id == first.id
    assert store.count() == 1

    current[0] = now + timedelta(seconds=600)
    after_window, after_window_duplicate = _create(store)

    assert after_window_duplicate is False
    assert after_window.id == "inc_second"
    assert store.count() == 2


def test_concurrent_same_key_creates_exactly_one_incident() -> None:
    sequence = count(1)
    store = IncidentStore(600, id_factory=lambda: f"inc_{next(sequence)}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: _create(store), range(64)))

    assert sum(not duplicate for _, duplicate in results) == 1
    assert {incident.id for incident, _ in results} == {"inc_1"}
    assert store.count() == 1


def test_store_plan_atomically_enters_approval_and_exposes_exact_safe_view() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_plan")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    plan = _plan()

    updated = store.store_plan(incident.id, plan, trace_id=TRACE_ID)
    plan.steps[0].args["service"] = "mutated-input"

    assert updated.state is IncidentState.AWAITING_APPROVAL
    view = store.view(incident.id, "trace-plan")
    assert view.state is IncidentState.AWAITING_APPROVAL
    assert view.plan is not None
    assert view.plan.model_dump(mode="json") == {
        "status": "proposed",
        "steps": [
            {
                "seq": 1,
                "action": "Restart checkout-service worker pool",
                "tool": "restart_service",
                "args": {"service": "checkout-service"},
                "risk_level": "safe",
                "rollback": "Service auto-recovers; no rollback needed",
            }
        ],
    }
    assert "raw_payload" not in view.model_dump()

    view.plan.steps[0].args["service"] = "mutated-view"
    assert (
        store.view(incident.id, "trace-plan-again").plan.steps[0].args["service"]
        == "checkout-service"
    )


def test_store_plan_rejects_unvalidated_values_without_mutation() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_invalid_plan")
    incident, _ = _create(store)

    with pytest.raises(TypeError, match="validated RemediationPlan"):
        store.store_plan(
            incident.id,
            {"steps": []},  # type: ignore[arg-type]
            trace_id=TRACE_ID,
        )
    assert store.get(incident.id).state is IncidentState.NEW
    assert store.view(incident.id, "trace-new").plan is None

    store.transition(incident.id, IncidentState.TRIAGED)
    with pytest.raises(TypeError, match="validated RemediationPlan"):
        store.store_plan(
            incident.id,
            object(),  # type: ignore[arg-type]
            trace_id=TRACE_ID,
        )

    assert store.get(incident.id).state is IncidentState.TRIAGED
    assert store.view(incident.id, "trace-unvalidated").plan is None


@pytest.mark.parametrize("trace_id", [None, "", "   "])
def test_store_plan_requires_a_non_empty_trace_id(trace_id: object) -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_plan_trace")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)

    with pytest.raises(ValueError, match="trace_id must be a non-empty string"):
        store.store_plan(
            incident.id,
            _plan(),
            trace_id=trace_id,  # type: ignore[arg-type]
        )

    assert store.get(incident.id).state is IncidentState.TRIAGED
    assert store.get_plan(incident.id) is None
    assert store.view(incident.id, "trace-invalid").trail == []


def test_store_plan_state_guards_preserve_existing_plan() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_guarded_plan")
    incident, _ = _create(store)

    with pytest.raises(InvalidTransitionError, match="while NEW"):
        store.store_plan(
            incident.id,
            _plan("new-state plan"),
            trace_id=TRACE_ID,
        )

    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(
        incident.id,
        _plan("accepted plan"),
        trace_id=TRACE_ID,
    )
    expected = store.view(incident.id, "trace-awaiting").plan

    with pytest.raises(InvalidTransitionError, match="while AWAITING_APPROVAL"):
        store.store_plan(
            incident.id,
            _plan("replacement plan"),
            trace_id=TRACE_ID,
        )
    assert store.view(incident.id, "trace-still-awaiting").plan == expected

    store.approve_for_execution(incident.id, operator="demo-operator")
    with pytest.raises(InvalidTransitionError, match="while EXECUTING"):
        store.store_plan(
            incident.id,
            _plan("executing plan"),
            trace_id=TRACE_ID,
        )
    assert store.view(incident.id, "trace-executing").plan == expected

    store.transition(incident.id, IncidentState.RESOLVED)
    with pytest.raises(InvalidTransitionError, match="while RESOLVED"):
        store.store_plan(
            incident.id,
            _plan("resolved plan"),
            trace_id=TRACE_ID,
        )
    assert store.view(incident.id, "trace-resolved").plan == expected

    corrected_store = IncidentStore(600, id_factory=lambda: "inc_corrected_plan")
    corrected_incident, _ = _create(corrected_store)
    corrected_store.transition(corrected_incident.id, IncidentState.TRIAGED)
    corrected_store.store_plan(
        corrected_incident.id,
        _plan("rejected original"),
        trace_id=TRACE_ID,
    )
    corrected, _ = corrected_store.record_decision(
        corrected_incident.id,
        decision=ApprovalDecision.REJECT,
        operator="demo-operator",
        note="Use a bounded restart instead",
    )
    assert corrected.state is IncidentState.TRIAGED
    assert corrected_store.get_plan(corrected_incident.id) is None
    replacement = corrected_store.store_plan(
        corrected_incident.id,
        _plan("corrected replacement"),
        trace_id=TRACE_ID,
    )
    assert replacement.state is IncidentState.AWAITING_APPROVAL


def test_concurrent_plan_writes_have_exactly_one_atomic_winner() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_concurrent_plan")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)

    def attempt(index: int) -> str | None:
        action = f"candidate plan {index}"
        try:
            store.store_plan(incident.id, _plan(action), trace_id=TRACE_ID)
        except InvalidTransitionError:
            return None
        return action

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(64)))

    winners = [result for result in results if result is not None]
    view = store.view(incident.id, "trace-concurrent-plan")
    assert len(winners) == 1
    assert view.state is IncidentState.AWAITING_APPROVAL
    assert view.plan is not None
    assert view.plan.steps[0].action == winners[0]


def test_state_machine_rejects_skips_and_requires_atomic_approval() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_state")
    incident, _ = _create(store)

    with pytest.raises(InvalidTransitionError):
        store.transition(incident.id, IncidentState.AWAITING_APPROVAL)

    store.transition(incident.id, IncidentState.TRIAGED)
    with pytest.raises(InvalidTransitionError, match="store_plan"):
        store.transition(incident.id, IncidentState.AWAITING_APPROVAL)
    assert store.get_plan(incident.id) is None
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    with pytest.raises(ApprovalRequiredError):
        store.transition(incident.id, IncidentState.EXECUTING)

    executing, approval = store.approve_for_execution(
        incident.id,
        operator="demo-operator",
        note="approved for isolated target",
    )

    assert executing.state is IncidentState.EXECUTING
    assert approval.incident_id == incident.id
    assert len(store.approvals_for_incident(incident.id)) == 1


def test_state_machine_supports_all_documented_nonapproval_transitions() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_transitions")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)
    edited, edit_approval = store.record_decision(
        incident.id,
        decision=ApprovalDecision.EDIT,
        operator="demo-operator",
        edits=(PlanEdit(seq=1, instruction="Use a bounded restart"),),
    )
    assert edited.state is IncidentState.TRIAGED
    assert edit_approval.decision is ApprovalDecision.EDIT
    assert store.get_plan(incident.id) is None
    store.store_plan(
        incident.id,
        _plan("regenerated plan"),
        trace_id=TRACE_ID,
    )
    executing, _ = store.approve_for_execution(
        incident.id,
        operator="demo-operator",
    )
    retry = store.record_execution_failure(
        executing.id,
        "demo restart returned a transient error",
    )
    assert retry.state is IncidentState.AWAITING_APPROVAL
    rejected, reject_approval = store.record_decision(
        executing.id,
        decision=ApprovalDecision.REJECT,
        operator="demo-operator",
        note="Try a different remediation",
    )

    assert rejected.state is IncidentState.TRIAGED
    assert reject_approval.decision is ApprovalDecision.REJECT
    assert store.get_plan(incident.id) is None
    assert len(store.approvals_for_incident(incident.id)) == 3


def test_execution_failure_requires_note_and_cannot_use_generic_transition() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_failure_note")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)
    executing, _ = store.approve_for_execution(
        incident.id,
        operator="demo-operator",
    )

    with pytest.raises(FailureNoteRequiredError):
        store.transition(executing.id, IncidentState.AWAITING_APPROVAL)
    with pytest.raises(FailureNoteRequiredError):
        store.record_execution_failure(executing.id, "   ")

    retry = store.record_execution_failure(executing.id, "tool returned HTTP 503")

    assert retry.state is IncidentState.AWAITING_APPROVAL
    assert retry.id == incident.id
    failure_entry = store.view(incident.id, "trace-failure").trail[-1]
    assert failure_entry.type is TrailEntryType.EXECUTION
    assert failure_entry.content == {
        "status": "failed",
        "note": "tool returned HTTP 503",
    }


def test_generic_transition_cannot_bypass_any_operator_decision() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_decision_gate")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    for target in (
        IncidentState.EXECUTING,
        IncidentState.TRIAGED,
    ):
        with pytest.raises(ApprovalRequiredError):
            store.transition(incident.id, target)


def test_corrections_require_strict_input_and_clear_plan_atomically() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc-correction")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    with pytest.raises(CorrectionRequiredError, match="non-empty note"):
        store.record_decision(
            incident.id,
            decision=ApprovalDecision.REJECT,
            operator="demo-operator",
            note="   ",
        )
    with pytest.raises(CorrectionRequiredError, match="at least one plan edit"):
        store.record_decision(
            incident.id,
            decision=ApprovalDecision.EDIT,
            operator="demo-operator",
        )

    assert store.get(incident.id).state is IncidentState.AWAITING_APPROVAL
    assert store.get_plan(incident.id) is not None
    assert store.approvals_for_incident(incident.id) == []

    updated, approval = store.record_decision(
        incident.id,
        decision=ApprovalDecision.EDIT,
        operator=" demo-operator ",
        note=" tighten limits ",
        edits=(PlanEdit(seq=1, instruction=" use a 45s bound "),),
    )

    assert updated.state is IncidentState.TRIAGED
    assert store.get_plan(incident.id) is None
    assert approval.operator == "demo-operator"
    assert approval.note == "tighten limits"
    assert approval.edits[0].instruction == "use a 45s bound"
    assert store.view(incident.id, "trace-correction").trail[-1].content == {
        "operator": "demo-operator",
        "decision": "edit",
        "note": "tighten limits",
        "edits": [{"seq": 1, "instruction": "use a 45s bound"}],
    }


def test_operator_decision_fails_closed_if_awaiting_plan_is_missing() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc-orphaned-approval")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    # Simulate a corrupted or legacy orphaned approval state that public APIs
    # can no longer create, and verify the decision boundary still fails closed.
    store._plans[incident.id] = None

    with pytest.raises(InvalidTransitionError, match="without a stored plan"):
        store.approve_for_execution(
            incident.id,
            operator="demo-operator",
        )

    assert store.get(incident.id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident.id) == []


def test_get_plan_returns_an_isolated_executor_snapshot() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc-plan-snapshot")
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    first = store.get_plan(incident.id)
    assert first is not None
    first.steps[0].args["service"] = "mutated-copy"

    second = store.get_plan(incident.id)
    assert second is not None
    assert second.steps[0].args == {"service": "checkout-service"}


def test_trail_entries_are_ordered_and_returned_in_incident_view() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_trail")
    incident, _ = _create(store)

    store.append_trail(incident.id, TrailEntryType.THOUGHT, "classified")
    store.append_trail(
        incident.id,
        TrailEntryType.TOOL_CALL,
        {"tool": "service_status", "args": {"service": incident.service}},
    )

    view = store.view(incident.id, "trace-demo")

    assert [entry.seq for entry in view.trail] == [1, 2]
    assert all(entry.timestamp.tzinfo is not None for entry in view.trail)
    assert view.trace_id == "trace-demo"


def test_trail_content_is_immutable_from_input_and_returned_entry() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc_immutable_trail")
    incident, _ = _create(store)
    content = {"evidence": ["first"]}

    returned = store.append_trail(
        incident.id,
        TrailEntryType.THOUGHT,
        content,
    )
    content["evidence"].append("mutated-input")
    returned.content["evidence"].append("mutated-return")

    stored = store.view(incident.id, "trace-immutable").trail[0]
    assert stored.content == {"evidence": ["first"]}
    assert stored.model_dump(exclude_none=True) == {
        "seq": 1,
        "type": TrailEntryType.THOUGHT,
        "content": {"evidence": ["first"]},
        "timestamp": stored.timestamp,
    }


def test_failed_approval_trail_write_rolls_back_state_and_record() -> None:
    class FailingTrailStore(DecisionTrailStore):
        def append(self, *args, **kwargs):
            if args[1] is TrailEntryType.APPROVAL:
                raise RuntimeError("simulated trail failure")
            return super().append(*args, **kwargs)

    trail_store = FailingTrailStore()
    store = IncidentStore(
        600,
        id_factory=lambda: "inc_atomic_decision",
        trail_store=trail_store,
    )
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)

    with pytest.raises(RuntimeError, match="simulated trail failure"):
        store.record_decision(
            incident.id,
            decision=ApprovalDecision.APPROVE,
            operator="demo-operator",
        )

    assert store.get(incident.id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident.id) == []
    assert [
        entry.type for entry in store.view(incident.id, "trace-rollback").trail
    ] == [TrailEntryType.THOUGHT]


def test_failed_plan_ready_trail_write_rolls_back_plan_and_state() -> None:
    class FailingTrailStore(DecisionTrailStore):
        def append(self, *args, **kwargs):
            raise RuntimeError("simulated plan-ready trail failure")

    store = IncidentStore(
        600,
        id_factory=lambda: "inc_atomic_plan_ready",
        trail_store=FailingTrailStore(),
    )
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)

    with pytest.raises(RuntimeError, match="simulated plan-ready trail failure"):
        store.store_plan(
            incident.id,
            _plan(),
            trace_id=TRACE_ID,
        )

    assert store.get(incident.id).state is IncidentState.TRIAGED
    assert store.get_plan(incident.id) is None
    assert store.view(incident.id, "trace-plan-ready-rollback").trail == []


def test_failed_execution_trail_write_rolls_back_state() -> None:
    class FailingTrailStore(DecisionTrailStore):
        def append(self, *args, **kwargs):
            if args[1] is TrailEntryType.EXECUTION:
                raise RuntimeError("simulated execution trail failure")
            return super().append(*args, **kwargs)

    store = IncidentStore(
        600,
        id_factory=lambda: "inc_failure_rollback",
        trail_store=FailingTrailStore(),
    )
    incident, _ = _create(store)
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id=TRACE_ID)
    executing, _ = store.approve_for_execution(
        incident.id,
        operator="demo-operator",
    )

    with pytest.raises(RuntimeError, match="simulated execution trail failure"):
        store.record_execution_failure(executing.id, "tool returned HTTP 503")

    assert store.get(incident.id).state is IncidentState.EXECUTING
    assert [
        entry.type for entry in store.view(incident.id, "trace-rollback").trail
    ] == [TrailEntryType.THOUGHT, TrailEntryType.APPROVAL]
