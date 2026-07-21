"""ADR-029 read-only viewer role: separate token, reads only, no mutations."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.agent.runtime import AgentTaskManager, LifecycleJobKind, LifecycleTaskManager
from app.config import Settings, get_settings
from app.incidents import Approval, IncidentState, IncidentStore, Severity
from app.main import create_app


_LOGGER = logging.getLogger("praxis.test.viewer")
_OPERATOR = "operator-token-viewer-suite-0123456789abcd"
_VIEWER = "viewer-token-viewer-suite-0123456789abcdefg"
_WEBHOOK = "viewer-suite-webhook-signing-secret-0123456789"


class _InertAgent:
    async def run(self, incident_id: str, trace_id: str) -> None:
        del incident_id, trace_id

    async def regenerate(
        self, incident_id: str, trace_id: str, correction: Approval
    ) -> None:
        del incident_id, trace_id, correction


def _settings(**overrides) -> Settings:
    return replace(
        get_settings(),
        operator_token=_OPERATOR,
        viewer_token=_VIEWER,
        webhook_signing_secret=_WEBHOOK,
        dedup_window_seconds=600,
        **overrides,
    )


def _awaiting(store: IncidentStore) -> str:
    from app.agent.plans import RemediationPlan, RiskLevel

    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload={},
        service="checkout-service",
        severity=Severity.HIGH,
        signal="timeout",
        title="Checkout timeout",
        idempotency_key="viewer-suite",
    )
    store.transition(incident.id, IncidentState.TRIAGED)
    store.store_plan(
        incident.id,
        RemediationPlan.model_validate(
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
        ),
        trace_id="plan-trace",
    )
    return incident.id


def _app(store: IncidentStore):
    lifecycle = LifecycleTaskManager(store, _LOGGER)
    agent_tasks = AgentTaskManager(_InertAgent(), _LOGGER, lifecycle=lifecycle)
    execution_tasks = AgentTaskManager(
        _InertAgent(),
        _LOGGER,
        lifecycle=lifecycle,
        job_kind=LifecycleJobKind.APPROVED_EXECUTION,
    )
    return create_app(
        _settings(),
        store,
        agent_task_manager=agent_tasks,
        execution_task_manager=execution_tasks,
        lifecycle_task_manager=lifecycle,
    )


def _op(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_config_rejects_viewer_equal_to_operator(monkeypatch) -> None:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    for name in (
        "APP_ENV",
        "DEPLOYED_ON",
        "MEMORY_BACKEND",
        "PRAXIS_DEMO_TARGET_URL",
        "PRAXIS_DEMO_TARGET_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.setenv("PROVIDER_ORDER", "qwencloud,openrouter")
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", _OPERATOR)
    monkeypatch.setenv("PRAXIS_VIEWER_TOKEN", _OPERATOR)

    with pytest.raises(ValueError, match="PRAXIS_VIEWER_TOKEN must differ"):
        Settings.from_env()


def test_config_accepts_distinct_viewer_token(monkeypatch) -> None:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    for name in ("APP_ENV", "DEPLOYED_ON", "MEMORY_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.setenv("PROVIDER_ORDER", "qwencloud,openrouter")
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", _OPERATOR)
    monkeypatch.setenv("PRAXIS_VIEWER_TOKEN", _VIEWER)

    settings = Settings.from_env()
    assert settings.viewer_token == _VIEWER
    assert settings.operator_token == _OPERATOR


def test_reads_accept_operator_and_viewer_but_reject_others() -> None:
    store = IncidentStore(600)
    incident_id = _awaiting(store)
    with TestClient(_app(store)) as client:
        assert client.get("/incidents", headers=_op(_OPERATOR)).status_code == 200
        assert client.get("/incidents", headers=_op(_VIEWER)).status_code == 200
        assert client.get(f"/incidents/{incident_id}", headers=_op(_VIEWER)).status_code == 200
        assert client.get(
            f"/incidents/{incident_id}/memory-match", headers=_op(_VIEWER)
        ).status_code == 200
        assert client.get("/incidents").status_code == 401
        wrong = client.get("/incidents", headers=_op("wrong-token-" + "x" * 40))
        assert wrong.status_code == 401
        assert wrong.headers.get("WWW-Authenticate") == "Bearer"


def test_session_reports_least_privilege_role() -> None:
    store = IncidentStore(600)
    _awaiting(store)
    with TestClient(_app(store)) as client:
        operator = client.get("/session", headers=_op(_OPERATOR))
        viewer = client.get("/session", headers=_op(_VIEWER))
    assert operator.status_code == 200 and operator.json()["role"] == "operator"
    assert viewer.status_code == 200 and viewer.json()["role"] == "viewer"


def test_viewer_token_cannot_approve_or_mutate() -> None:
    store = IncidentStore(600)
    incident_id = _awaiting(store)
    with TestClient(_app(store)) as client:
        rejected = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
            headers=_op(_VIEWER),
        )
        assert rejected.status_code == 401
        assert rejected.headers.get("WWW-Authenticate") == "Bearer"
        # No approval or state change occurred.
        assert store.approvals_for_incident(incident_id) == []
        assert store.get(incident_id).state is IncidentState.AWAITING_APPROVAL
        # The operator, by contrast, is not rejected at the auth boundary.
        accepted = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve"},
            headers=_op(_OPERATOR),
        )
        assert accepted.status_code != 401


def test_ui_gates_controls_and_detects_viewer_role() -> None:
    from pathlib import Path

    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
    assert "readOnly: false" in html
    assert 'await determineRole();' in html
    assert 'const session = await fetchJson("/session");' in html
    assert 'viewState.readOnly = !(session && session.role === "operator");' in html
    assert (
        'const isAwaitingApproval = state === "AWAITING_APPROVAL" && !viewState.readOnly;'
        in html
    )
    assert "viewState.readOnly ||" in html  # submitDecision guard
