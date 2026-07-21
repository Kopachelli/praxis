from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.client import ChatCompletion, ModelRole
from app.agent.runtime import AgentTaskManager, IncidentAgent
from app.agent.triage import TriageAgent
from app.config import get_settings
from app.incidents import IncidentState, IncidentStore
from app.trail import TrailEntryType


_SECRET = "agent-webhook-integration-secret"
_PAYLOAD = {
    "source": "sentry",
    "service": "checkout-service",
    "level": "error",
    "title": "checkout upstream timeout",
}


class _BlockingTransitionAgent:
    def __init__(self, store: IncidentStore) -> None:
        self._store = store
        self.started = threading.Event()
        self.release = threading.Event()
        self.transitioned = threading.Event()
        self.calls: list[tuple[str, str]] = []

    async def run(self, incident_id: str, trace_id: str) -> None:
        self.calls.append((incident_id, trace_id))
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.005)
        self._store.transition(incident_id, IncidentState.TRIAGED)
        self.transitioned.set()


class _ScheduleSpy(AgentTaskManager):
    def __init__(self, agent: IncidentAgent) -> None:
        super().__init__(agent, logging.getLogger("praxis.test.webhook-agent"))
        self.schedule_calls: list[tuple[str, str]] = []

    def schedule(self, incident_id: str, trace_id: str) -> bool:
        self.schedule_calls.append((incident_id, trace_id))
        return super().schedule(incident_id, trace_id)


class _BlockingScriptedQwenClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, object]] = []
        self._responses = [
            _completion(
                "upstream dependency timeout",
                model="qwen-flash",
                total_tokens=9,
            ),
            _completion(
                json.dumps(
                    {
                        "steps": [
                            {
                                "seq": 1,
                                "action": "Restart the checkout worker pool",
                                "tool": "restart_service",
                                "args": {"service": "checkout-service"},
                                "risk_level": "safe",
                                "rollback": "Restart the prior worker revision",
                            }
                        ]
                    }
                ),
                model="qwen3.7-max",
                reasoning=(
                    "Repeated upstream timeouts indicate the checkout worker "
                    "pool is unhealthy."
                ),
                total_tokens=21,
            ),
        ]

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if len(self.calls) == 1:
            self.started.set()
            while not self.release.is_set():
                await asyncio.sleep(0.005)
        if not self._responses:
            raise AssertionError("unexpected scripted Qwen call")
        return self._responses.pop(0)


def _completion(
    content: str,
    *,
    model: str,
    total_tokens: int,
    reasoning: str | None = None,
) -> ChatCompletion:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return ChatCompletion.from_response(
        "qwencloud",
        model,
        {
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"total_tokens": total_tokens},
        },
    )


def _settings():
    return replace(get_settings(), webhook_signing_secret=_SECRET)


def _signed_request() -> tuple[bytes, dict[str, str]]:
    body = json.dumps(_PAYLOAD, separators=(",", ":"), sort_keys=True).encode()
    digest = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Praxis-Signature": f"sha256={digest}",
        "X-Idempotency-Key": "agent-integration-alert",
    }


def test_new_webhook_acks_under_500ms_and_duplicate_schedules_no_agent() -> None:
    store = IncidentStore(600)
    agent = _BlockingTransitionAgent(store)
    manager = _ScheduleSpy(agent)
    application = main_module.create_app(
        _settings(),
        store,
        agent_task_manager=manager,
    )
    body, headers = _signed_request()

    with TestClient(application) as client:
        started_at = time.perf_counter()
        first = client.post("/webhook", content=body, headers=headers)
        elapsed = time.perf_counter() - started_at

        assert first.status_code == 202
        assert elapsed < 0.5
        assert agent.started.wait(timeout=1)
        incident_id = first.json()["incident_id"]
        assert store.get(incident_id).state is IncidentState.NEW

        duplicate = client.post("/webhook", content=body, headers=headers)

        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert manager.schedule_calls == [(incident_id, first.json()["trace_id"])]
        assert agent.calls == [(incident_id, first.json()["trace_id"])]

        agent.release.set()
        assert agent.transitioned.wait(timeout=1)
        assert store.get(incident_id).state is IncidentState.TRIAGED

    assert manager.is_active(incident_id) is False


def test_signed_webhook_reaches_real_triage_approval_checkpoint_without_network() -> None:
    store = IncidentStore(600)
    qwen = _BlockingScriptedQwenClient()
    triage = TriageAgent(
        store,
        qwen,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test.integrated-triage"),
    )
    manager = _ScheduleSpy(triage)
    application = main_module.create_app(
        _settings(),
        store,
        agent_task_manager=manager,
    )
    body, headers = _signed_request()

    with TestClient(application) as client:
        started_at = time.perf_counter()
        first = client.post("/webhook", content=body, headers=headers)
        elapsed = time.perf_counter() - started_at

        assert first.status_code == 202
        assert elapsed < 0.5
        assert qwen.started.wait(timeout=1)
        incident_id = first.json()["incident_id"]
        trace_id = first.json()["trace_id"]
        assert store.get(incident_id).state is IncidentState.NEW

        duplicate = client.post("/webhook", content=body, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert manager.schedule_calls == [(incident_id, trace_id)]
        assert len(qwen.calls) == 1

        qwen.release.set()
        deadline = time.monotonic() + 1
        while (
            store.get(incident_id).state is not IncidentState.AWAITING_APPROVAL
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        view = store.view(incident_id, "read-trace")
        assert view.state is IncidentState.AWAITING_APPROVAL
        assert view.plan is not None
        assert view.plan.status == "proposed"
        assert [step.model_dump(mode="json") for step in view.plan.steps] == [
            {
                "seq": 1,
                "action": "Restart the checkout worker pool",
                "tool": "restart_service",
                "args": {"service": "checkout-service"},
                "risk_level": "safe",
                "rollback": "Restart the prior worker revision",
            }
        ]

        assert [entry.type for entry in view.trail] == [
            TrailEntryType.THOUGHT,
            TrailEntryType.THOUGHT,
            TrailEntryType.THOUGHT,
        ]
        classification, root_cause, plan_ready = view.trail
        assert classification.content == {
            "stage": "classification",
            "classification": "upstream dependency timeout",
            "provider": "qwencloud",
            "model": "qwen-flash",
            "trace_id": trace_id,
        }
        assert classification.model_used == "qwen-flash"
        assert classification.tokens == 9
        assert root_cause.content == {
            "stage": "root_cause_reasoning",
            "hypothesis": (
                "Repeated upstream timeouts indicate the checkout worker "
                "pool is unhealthy."
            ),
            "provider": "qwencloud",
            "model": "qwen3.7-max",
            "trace_id": trace_id,
        }
        assert root_cause.model_used == "qwen3.7-max"
        assert root_cause.tokens == 21
        assert plan_ready.content == {
            "stage": "plan_ready",
            "status": "ready",
            "trace_id": trace_id,
        }
        assert plan_ready.timestamp >= view.created_at
        assert [call["role"] for call in qwen.calls] == [
            ModelRole.FAST,
            ModelRole.PRIMARY,
        ]
        assert [call["thinking"] for call in qwen.calls] == [False, True]
        assert [call["trace_id"] for call in qwen.calls] == [trace_id, trace_id]
        assert len(manager.schedule_calls) == 1

    assert manager.is_active(incident_id) is False


def test_default_runtime_shares_trail_and_lifespan_closes_qwen_client(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _CapturingQwenClient:
        def __init__(self, settings, *, trail) -> None:
            captured["settings"] = settings
            captured["trail"] = trail
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(main_module, "QwenClient", _CapturingQwenClient)
    store = IncidentStore(600)
    application = main_module.create_app(_settings(), store)
    client_instance = application.state.qwen_client

    assert captured["trail"] is store.trail_store
    assert client_instance.closed is False

    with TestClient(application) as client:
        assert client.get("/healthz").status_code == 200

    assert client_instance.closed is True
