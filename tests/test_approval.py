"""Approval endpoint and correction-cycle tests [FR-6..FR-8, ADR-014]."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.approval import MAX_APPROVAL_NOTE_CHARS, build_approval_router
from app.incidents import Approval, IncidentState, IncidentStore, Severity
from app.agent.plans import parse_remediation_plan_json
from app.operator_auth import build_operator_auth_dependency
from app.trail import TrailEntryType

OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"


def _plan():
    return parse_remediation_plan_json(
        """{
          "steps": [{
            "seq": 1,
            "action": "Restart checkout workers",
            "tool": "restart_service",
            "args": {"service": "checkout-service"},
            "risk_level": "safe",
            "rollback": "Restart the prior healthy revision"
          }]
        }""",
        registered_tools={"restart_service"},
    )


def _awaiting_store() -> tuple[IncidentStore, str]:
    store = IncidentStore(600, id_factory=lambda: "inc-approval")
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload={"message": "timeout"},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="upstream_timeout",
        title="Checkout timeout",
        idempotency_key="approval-key",
    )
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(incident.id, _plan(), trace_id="0" * 32)
    return store, incident.id


class _RegenerationScheduler:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.calls: list[tuple[str, str, Approval]] = []

    def schedule_regeneration(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> bool:
        self.calls.append(
            (incident_id, trace_id, correction.model_copy(deep=True))
        )
        return self._result


class _ExecutionScheduler:
    def __init__(self, store: IncidentStore, *, result: bool = True) -> None:
        self._store = store
        self._result = result
        self.calls: list[tuple[str, str, IncidentState]] = []

    def schedule(self, incident_id: str, trace_id: str) -> bool:
        self.calls.append(
            (incident_id, trace_id, self._store.get(incident_id).state)
        )
        return self._result


def _client(
    store: IncidentStore,
    regeneration: _RegenerationScheduler,
    execution: _ExecutionScheduler,
) -> TestClient:
    application = FastAPI()
    test_logger = logging.getLogger("praxis.test.approval")

    @application.middleware("http")
    async def attach_trace(request: Request, call_next):
        request.state.trace_id = "trace-approval"
        request.state.incident_id = "-"
        return await call_next(request)

    application.include_router(
        build_approval_router(
            store,
            test_logger,
            regeneration,  # type: ignore[arg-type]
            execution,
            build_operator_auth_dependency(OPERATOR_TOKEN, test_logger),
        )
    )
    return TestClient(
        application,
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    )


def test_approve_records_server_operator_before_scheduling_execution() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "EXECUTING"
    assert execution.calls == [
        (incident_id, "trace-approval", IncidentState.EXECUTING)
    ]
    assert regeneration.calls == []
    approval = store.approvals_for_incident(incident_id)[0]
    assert approval.operator == "demo-operator"
    assert approval.decision.value == "approve"
    assert store.view(incident_id, "trace").trail[-1].type is TrailEntryType.APPROVAL


def test_client_cannot_spoof_server_owned_operator() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve", "operator": "attacker"},
        )

    assert response.status_code == 422
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
    assert execution.calls == []


def test_reject_clears_plan_and_schedules_qwen_regeneration() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "reject", "note": "Do not restart; inspect logs"},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "TRIAGED"
    assert "plan" not in response.json()
    assert store.get_plan(incident_id) is None
    assert execution.calls == []
    assert len(regeneration.calls) == 1
    correction = regeneration.calls[0][2]
    assert correction.operator == "demo-operator"
    assert correction.note == "Do not restart; inspect logs"


def test_regeneration_scheduler_refusal_returns_503_with_failure_trail() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler(result=False)
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "reject", "note": "Generate a safer plan"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Plan regeneration could not be scheduled",
        "trace_id": "trace-approval",
    }
    assert store.get(incident_id).state is IncidentState.TRIAGED
    assert store.get_plan(incident_id) is None
    assert len(store.approvals_for_incident(incident_id)) == 1
    assert execution.calls == []
    trail = store.view(incident_id, "trace").trail
    assert [entry.type for entry in trail] == [
        TrailEntryType.THOUGHT,
        TrailEntryType.APPROVAL,
        TrailEntryType.THOUGHT,
    ]
    assert trail[-1].content == {
        "stage": "plan_regeneration",
        "status": "failed",
        "reason": "scheduling_failed",
        "trace_id": "trace-approval",
    }


def test_reject_accepts_the_documented_note_length_boundary() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)
    note = "x" * MAX_APPROVAL_NOTE_CHARS

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "reject", "note": note},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "TRIAGED"
    assert execution.calls == []
    assert len(regeneration.calls) == 1
    assert regeneration.calls[0][2].note == note


def test_edit_correction_is_redacted_before_audit_and_regeneration_queue() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)
    secrets = (
        "operator-secret-key-sentinel",
        "operator-digest-response-sentinel",
        "operator-signing-key-sentinel",
        "b3BlcmF0b3I6cGFzc3dvcmQ=",
    )

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "decision": "edit",
                "note": (
                    f"SECRET_KEY={secrets[0]}\n"
                    "Authorization: Digest username=\"operator\", "
                    f"response=\"{secrets[1]}\""
                ),
                "edits": [
                    {
                        "seq": 1,
                        "instruction": (
                            f"webhookSigningKey={secrets[2]}; "
                            f"Authorization: Basic {secrets[3]}"
                        ),
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == "TRIAGED"
    assert execution.calls == []
    assert len(regeneration.calls) == 1
    persisted = store.approvals_for_incident(incident_id)[0]
    queued = regeneration.calls[0][2]
    trail = store.view(incident_id, "trace-redacted-correction").trail[-1]
    rendered = json.dumps(
        {
            "persisted": persisted.model_dump(mode="json"),
            "queued": queued.model_dump(mode="json"),
            "trail": trail.model_dump(mode="json"),
        }
    )
    for secret in secrets:
        assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 6
    assert persisted == queued
    assert persisted.note is not None
    assert len(persisted.note) <= MAX_APPROVAL_NOTE_CHARS


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "approve", "note": "not applicable"},
        {"decision": "approve", "note": None},
        {"decision": "approve", "edits": []},
        {
            "decision": "approve",
            "edits": [{"seq": 1, "instruction": "not applicable"}],
        },
        {"decision": "reject"},
        {"decision": "reject", "note": "   "},
        {"decision": "reject", "note": "reason", "edits": []},
        {"decision": "reject", "note": "x" * 2_001},
        {"decision": "edit", "edits": []},
        {
            "decision": "edit",
            "edits": [
                {"seq": seq, "instruction": "change"}
                for seq in range(1, 22)
            ],
        },
        {
            "decision": "edit",
            "edits": [{"seq": 1, "instruction": "x" * 1_001}],
        },
        {"decision": "edit", "edits": [{"seq": 0, "instruction": "change"}]},
        {"decision": "edit", "edits": [{"seq": True, "instruction": "change"}]},
        {"decision": "edit", "edits": [{"seq": 1, "instruction": "   "}]},
        {
            "decision": "edit",
            "edits": [{"seq": 1, "instruction": "change", "extra": "no"}],
        },
        {"decision": "unknown"},
    ],
)
def test_invalid_correction_bodies_return_422_without_mutation(
    body: dict[str, Any],
) -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(f"/incidents/{incident_id}/approve", json=body)

    assert response.status_code == 422
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.get_plan(incident_id) is not None
    assert store.approvals_for_incident(incident_id) == []
    assert regeneration.calls == []
    assert execution.calls == []


def test_edit_records_strict_instructions_and_never_executes() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "decision": "edit",
                "note": "Keep the rollback explicit",
                "edits": [{"seq": 1, "instruction": " use a 45s timeout "}],
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == "TRIAGED"
    assert execution.calls == []
    correction = regeneration.calls[0][2]
    assert correction.note == "Keep the rollback explicit"
    assert correction.edits[0].model_dump() == {
        "seq": 1,
        "instruction": "use a 45s timeout",
    }


def test_unknown_and_nonawaiting_incidents_return_404_and_409() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store)

    with _client(store, regeneration, execution) as client:
        missing = client.post(
            "/incidents/missing/approve",
            json={"decision": "approve"},
        )
        store.record_decision(
            incident_id,
            decision=store.approvals_for_incident(incident_id)[0].decision,
            operator="demo-operator",
        ) if store.approvals_for_incident(incident_id) else store.approve_for_execution(
            incident_id,
            operator="demo-operator",
        )
        conflict = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
        )

    assert missing.status_code == 404
    assert conflict.status_code == 409


def test_execution_scheduler_failure_returns_to_approval_without_running() -> None:
    store, incident_id = _awaiting_store()
    regeneration = _RegenerationScheduler()
    execution = _ExecutionScheduler(store, result=False)

    with _client(store, regeneration, execution) as client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
        )

    assert response.status_code == 503
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert [entry.type for entry in store.view(incident_id, "trace").trail] == [
        TrailEntryType.THOUGHT,
        TrailEntryType.APPROVAL,
        TrailEntryType.EXECUTION,
    ]
