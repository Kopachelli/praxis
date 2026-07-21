"""JSON-safe request-validation boundary tests [FR-6, NFR-3, NFR-5]."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.agent.plans import parse_remediation_plan_json
from app.agent.runtime import AgentTaskManager
from app.config import Settings
from app.incidents import Approval, IncidentState, IncidentStore, Severity
from app.main import create_app

OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"


class _InertAgent:
    async def run(self, incident_id: str, trace_id: str) -> None:
        return None

    async def regenerate(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        app_env="dev",
        app_version="test",
        deployed_on="local",
        port=8000,
        provider_order=("qwencloud", "openrouter"),
        primary_model="qwen3.7-max",
        fast_model="qwen-flash",
        qwen_base_url=(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ),
        qwencloud_models=("qwen3.7-max", "qwen3-max", "qwen-plus"),
        openrouter_models=(
            "qwen/qwen3.7-max",
            "qwen/qwen3-max",
            "qwen/qwen-plus",
        ),
        dashscope_api_key="",
        openrouter_api_key="",
        fc_function_name="",
        fc_instance_id="",
        fc_region="",
        operator_token=OPERATOR_TOKEN,
    )


def _awaiting_client() -> tuple[TestClient, IncidentStore, str]:
    store = IncidentStore(600, id_factory=lambda: "inc-validation")
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload={"message": "timeout"},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="upstream_timeout",
        title="Checkout timeout",
        idempotency_key="validation-key",
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
    agent_tasks = AgentTaskManager(
        _InertAgent(), logging.getLogger("praxis.test.validation.agent")
    )
    execution_tasks = AgentTaskManager(
        _InertAgent(), logging.getLogger("praxis.test.validation.execution")
    )
    return (
        TestClient(
            create_app(
                _settings(),
                store,
                agent_task_manager=agent_tasks,
                execution_task_manager=execution_tasks,
            ),
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        ),
        store,
        incident.id,
    )


@pytest.mark.parametrize(
    ("body", "sentinel"),
    [
        (
            b'{"decision":"reject","note":NaN,'
            b'"operator":"NONFINITE-INPUT-SENTINEL"}',
            b"NONFINITE-INPUT-SENTINEL",
        ),
        (
            b'{"decision":"approve",'
            b'"note":"\\ud800LONE-SURROGATE-SENTINEL"}',
            b"LONE-SURROGATE-SENTINEL",
        ),
        (
            b'{"decision":"edit","edits":[{"seq":'
            + (b"9" * 5_000)
            + b',"instruction":"PATHOLOGICAL-INTEGER-SENTINEL"}]}',
            b"PATHOLOGICAL-INTEGER-SENTINEL",
        ),
    ],
)
def test_unsafe_validation_inputs_return_redacted_422_without_mutation(
    body: bytes,
    sentinel: bytes,
) -> None:
    client, store, incident_id = _awaiting_client()

    with client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]
    assert response.json()["detail"]
    assert sentinel not in response.content
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.get_plan(incident_id) is not None
    assert store.approvals_for_incident(incident_id) == []


def test_validation_diagnostics_retain_locations_types_and_messages_only() -> None:
    client, store, incident_id = _awaiting_client()

    with client:
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "decision": "edit",
                "edits": [
                    {"seq": 0, "instruction": "change", "unexpected": "secret"}
                ],
                "operator": "attacker",
            },
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["trace_id"] == response.headers["X-Trace-Id"]
    diagnostics = payload["detail"]
    assert diagnostics
    assert all(set(item) == {"loc", "type", "msg"} for item in diagnostics)
    assert all(isinstance(item["msg"], str) and item["msg"] for item in diagnostics)
    by_location = {tuple(item["loc"]): item["type"] for item in diagnostics}
    assert by_location[("body", "edits", 0, "seq")] == "greater_than_equal"
    assert by_location[("body", "edits", 0, "unexpected")] == "extra_forbidden"
    assert by_location[("body", "operator")] == "extra_forbidden"
    assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
    assert store.approvals_for_incident(incident_id) == []
