"""Application-level single-operator boundary tests [ADR-025]."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.operator_auth as operator_auth_module
from app.agent.plans import parse_remediation_plan_json
from app.config import get_settings
from app.incidents import Approval, IncidentState, IncidentStore, Severity
from app.main import create_app


OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
WRONG_OPERATOR_TOKEN = "wrong-operator-token-0123456789abcdef"
WEBHOOK_SECRET = "operator-auth-webhook-secret"


class _RecordingTasks:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str]] = []
        self.regenerations: list[tuple[str, str, Approval]] = []

    def schedule(self, incident_id: str, trace_id: str) -> bool:
        self.scheduled.append((incident_id, trace_id))
        return True

    def schedule_regeneration(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> bool:
        self.regenerations.append((incident_id, trace_id, correction))
        return True

    async def shutdown(self) -> None:
        return None


def _awaiting_store() -> tuple[IncidentStore, str]:
    incident_ids = iter(("inc-auth-existing", "inc-auth-webhook"))
    store = IncidentStore(600, id_factory=lambda: next(incident_ids))
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload={"message": "timeout"},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="upstream_timeout",
        title="Checkout timeout",
        idempotency_key="operator-auth-existing",
    )
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(
        incident.id,
        parse_remediation_plan_json(
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
        ),
        trace_id="0" * 32,
    )
    return store, incident.id


def _application():
    store, incident_id = _awaiting_store()
    agent_tasks = _RecordingTasks()
    execution_tasks = _RecordingTasks()
    settings = replace(
        get_settings(),
        operator_token=OPERATOR_TOKEN,
        webhook_signing_secret=WEBHOOK_SECRET,
    )
    application = create_app(
        settings,
        store,
        agent_task_manager=agent_tasks,  # type: ignore[arg-type]
        execution_task_manager=execution_tasks,  # type: ignore[arg-type]
    )
    return application, store, incident_id, agent_tasks, execution_tasks


def _assert_fixed_challenge(response) -> None:
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "detail": "Operator authentication required",
        "trace_id": response.headers["X-Trace-Id"],
    }


def _signed_webhook_headers(body: bytes) -> dict[str, str]:
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Praxis-Signature": f"sha256={digest}",
        "X-Idempotency-Key": "operator-auth-public-webhook",
    }


def test_public_routes_and_signed_webhook_do_not_require_operator_token() -> None:
    application, store, _, agent_tasks, _ = _application()
    body = json.dumps(
        {
            "source": "sentry",
            "service": "public-webhook-service",
            "title": "Public signed webhook",
            "severity": "high",
        },
        separators=(",", ":"),
    ).encode()

    with TestClient(application) as client:
        root = client.get("/")
        health = client.get("/healthz")
        webhook = client.post(
            "/webhook",
            content=body,
            headers=_signed_webhook_headers(body),
        )

    assert root.status_code == 200
    assert health.status_code == 200
    assert webhook.status_code == 202
    assert store.count() == 2
    assert len(agent_tasks.scheduled) == 1


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/incidents", None),
        ("GET", "/incidents/inc-auth-existing", None),
        ("GET", "/incidents/inc-auth-existing/memory-match", None),
        (
            "POST",
            "/incidents/inc-auth-existing/approve",
            {"decision": "approve"},
        ),
    ],
)
def test_every_operator_surface_rejects_missing_credentials_with_fixed_401(
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    application, store, incident_id, agent_tasks, execution_tasks = _application()

    with TestClient(application) as client:
        response = client.request(method, path, json=body)

    _assert_fixed_challenge(response)
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
    assert agent_tasks.regenerations == []
    assert execution_tasks.scheduled == []


@pytest.mark.parametrize(
    "authorization",
    (
        "Basic dXNlcjpwYXNz",
        "Bearer too-short",
        "Bearer wrong token with spaces 0123456789abcdef",
        f"Bearer {WRONG_OPERATOR_TOKEN}",
    ),
)
def test_invalid_credentials_are_indistinguishable_and_never_mutate(
    authorization: str,
) -> None:
    application, store, incident_id, agent_tasks, execution_tasks = _application()

    with TestClient(application) as client:
        existing = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
            headers={"Authorization": authorization},
        )
        missing = client.post(
            "/incidents/inc-auth-missing/approve",
            json={"decision": "approve"},
            headers={"Authorization": authorization},
        )

    _assert_fixed_challenge(existing)
    _assert_fixed_challenge(missing)
    assert existing.json()["detail"] == missing.json()["detail"]
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
    assert agent_tasks.regenerations == []
    assert execution_tasks.scheduled == []


def test_authentication_precedes_body_validation_and_approval_size_limit() -> None:
    application, store, incident_id, _, execution_tasks = _application()

    with TestClient(application) as client:
        malformed = client.post(
            f"/incidents/{incident_id}/approve",
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        oversized = client.post(
            f"/incidents/{incident_id}/approve",
            content=b"{" + (b" " * 20_000),
            headers={"Content-Type": "application/json"},
        )

    _assert_fixed_challenge(malformed)
    _assert_fixed_challenge(oversized)
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
    assert execution_tasks.scheduled == []


def test_authorized_operator_can_read_and_cross_the_explicit_approval_gate() -> None:
    application, store, incident_id, _, execution_tasks = _application()
    headers = {"Authorization": f"bearer {OPERATOR_TOKEN}"}

    with TestClient(application) as client:
        incident_list = client.get("/incidents", headers=headers)
        detail = client.get(f"/incidents/{incident_id}", headers=headers)
        memory = client.get(
            f"/incidents/{incident_id}/memory-match",
            headers=headers,
        )
        approval = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
            headers=headers,
        )

    assert incident_list.status_code == 200
    assert detail.status_code == 200
    assert memory.status_code == 200
    assert approval.status_code == 200
    assert store.get(incident_id).state is IncidentState.EXECUTING
    assert len(store.approvals_for_incident(incident_id)) == 1
    assert len(execution_tasks.scheduled) == 1


def test_constant_time_compare_runs_only_after_both_tokens_pass_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def record_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(
        operator_auth_module.hmac,
        "compare_digest",
        record_compare,
    )

    assert not operator_auth_module.operator_token_matches(
        OPERATOR_TOKEN,
        "too-short",
    )
    assert calls == []
    assert not operator_auth_module.operator_token_matches(
        OPERATOR_TOKEN,
        WRONG_OPERATOR_TOKEN,
    )
    assert len(calls) == 1
    assert operator_auth_module.operator_token_matches(
        OPERATOR_TOKEN,
        OPERATOR_TOKEN,
    )
    assert len(calls) == 2
