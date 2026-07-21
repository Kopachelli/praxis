from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from app.agent.client import ChatCompletion, ModelRole
from app.agent.plans import parse_remediation_plan_json
from app.agent.tools import build_tool_registry
from app.agent.triage import (
    AgentRunError,
    MAX_PLAN_REPROMPTS,
    MAX_TOOL_ROUNDS,
    PlanGenerationError,
    ToolRoundLimitError,
    TriageAgent,
)
from app.incidents import (
    ApprovalDecision,
    IncidentState,
    IncidentStore,
    InvalidTransitionError,
    PlanEdit,
    Severity,
)
from app.trail import TrailEntryType


def _completion(
    content: str | None,
    *,
    model: str,
    provider: str = "qwencloud",
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ChatCompletion:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ChatCompletion.from_response(
        provider,
        model,
        {
            "choices": [
                {
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
        },
    )


def _plan_json(action: str = "Restart the checkout worker pool") -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "seq": 1,
                    "action": action,
                    "tool": "restart_service",
                    "args": {"service": "checkout-service"},
                    "risk_level": "safe",
                    "rollback": "Restart the prior worker revision",
                }
            ]
        }
    )


def _tool_call(name: str = "service_status", call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"service": "checkout-service"}),
        },
    }


class ScriptedClient:
    def __init__(self, completions: list[ChatCompletion]) -> None:
        self._completions = list(completions)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if not self._completions:
            raise AssertionError("unexpected Qwen call")
        return self._completions.pop(0)


def _store(raw_payload: Any | None = None) -> tuple[IncidentStore, str]:
    store = IncidentStore(600, id_factory=lambda: "inc-triage")
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload=raw_payload or {
            "message": "Gateway timed out",
            "secret": "raw-secret-sentinel",
        },
        service="checkout-service",
        severity=Severity.HIGH,
        signal="upstream_timeout",
        title="TimeoutError in checkout-service",
        idempotency_key="triage-key",
    )
    return store, incident.id


def _run(agent: TriageAgent, incident_id: str) -> None:
    asyncio.run(agent.run(incident_id, "trace-triage"))


def test_thinking_tool_loop_reaches_validated_approval_checkpoint() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("upstream dependency timeout", model="qwen-flash"),
            _completion(
                None,
                model="qwen3.7-max",
                reasoning="The payment dependency is timing out repeatedly.",
                tool_calls=[_tool_call()],
            ),
            _completion(
                _plan_json(),
                model="qwen3.7-max",
            ),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        registry=build_tool_registry(),
        logger=logging.getLogger("praxis.test"),
    )

    _run(agent, incident_id)

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.AWAITING_APPROVAL
    assert view.plan is not None
    assert view.plan.steps[0].tool == "restart_service"
    assert [entry.type for entry in view.trail] == [
        TrailEntryType.THOUGHT,
        TrailEntryType.THOUGHT,
        TrailEntryType.TOOL_CALL,
        TrailEntryType.TOOL_RESULT,
        TrailEntryType.THOUGHT,
        TrailEntryType.THOUGHT,
    ]
    assert view.trail[1].content["hypothesis"].startswith("The payment")
    assert view.trail[2].content["args"] == {"service": "checkout-service"}
    assert view.trail[3].content["source"] == "incident_context"
    assert view.trail[-1].content == {
        "stage": "plan_ready",
        "status": "ready",
        "trace_id": "trace-triage",
    }
    assert view.trail[-1].timestamp >= view.created_at
    assert all(entry.content["trace_id"] == "trace-triage" for entry in view.trail)
    assert [call["role"] for call in client.calls] == [
        ModelRole.FAST,
        ModelRole.PRIMARY,
        ModelRole.PRIMARY,
    ]
    assert [call["thinking"] for call in client.calls] == [False, True, True]
    assert [call["trace_id"] for call in client.calls] == ["trace-triage"] * 3
    assert "tools" not in client.calls[0] or client.calls[0].get("tools") is None
    assert client.calls[1]["tools"]
    assert client.calls[2]["messages"][-1]["role"] == "tool"
    rendered_prompts = json.dumps(client.calls)
    assert "raw-secret-sentinel" not in rendered_prompts


def test_indexed_untyped_tool_call_replay_is_canonical_across_providers() -> None:
    store, incident_id = _store()
    indexed_call = _tool_call()
    indexed_call.pop("type")
    indexed_call["index"] = 0
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion(
                None,
                model="qwen3.7-max",
                reasoning="Service status should confirm the timeout hypothesis.",
                tool_calls=[indexed_call],
            ),
            _completion(
                _plan_json(),
                model="qwen3.7-max",
                provider="openrouter",
            ),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        registry=build_tool_registry(),
        logger=logging.getLogger("praxis.test"),
    )

    _run(agent, incident_id)

    assistant_replay = client.calls[2]["messages"][-2]
    assert assistant_replay == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
                {
                    "id": indexed_call["id"],
                    "type": "function",
                    "function": indexed_call["function"],
                }
        ],
    }
    assert "index" not in assistant_replay["tool_calls"][0]
    assert indexed_call["index"] == 0
    assert "type" not in indexed_call
    assert store.view(incident_id, "trace").state is IncidentState.AWAITING_APPROVAL


def test_invalid_plan_is_reprompted_with_redacted_diagnostics() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion('{"steps":[]}', model="qwen3.7-max", reasoning="first"),
            _completion(_plan_json("Retry safely"), model="qwen3.7-max", reasoning="second"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    _run(agent, incident_id)

    validation = next(
        entry
        for entry in store.view(incident_id, "trace").trail
        if entry.content.get("stage") == "plan_validation"
    )
    assert validation.content["status"] == "retry"
    retry_prompt = client.calls[-1]["messages"][-1]["content"]
    assert "Return corrected plan JSON only" in retry_prompt
    assert "input" not in retry_prompt


def test_three_invalid_primary_plans_use_fast_fallback_template() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion(
                "not-json",
                model="qwen3.7-max",
                reasoning="Primary analysis supports a bounded service restart.",
            ),
            _completion("not-json", model="qwen3.7-max"),
            _completion("not-json", model="qwen3.7-max"),
            _completion(_plan_json("Fallback plan"), model="qwen-flash"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    _run(agent, incident_id)

    assert [call["role"] for call in client.calls] == [
        ModelRole.FAST,
        ModelRole.PRIMARY,
        ModelRole.PRIMARY,
        ModelRole.PRIMARY,
        ModelRole.FAST,
    ]
    assert client.calls[-1]["thinking"] is False
    assert [call["trace_id"] for call in client.calls] == ["trace-triage"] * 5
    assert store.view(incident_id, "trace").state is IncidentState.AWAITING_APPROVAL


def test_valid_plan_without_primary_reasoning_stays_triaged_without_plan() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion(_plan_json(), model="qwen3.7-max"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(PlanGenerationError, match="reasoning was unavailable"):
        _run(agent, incident_id)

    view = store.view(incident_id, "trace")
    assert view.state is IncidentState.TRIAGED
    assert view.plan is None
    unavailable = next(
        entry
        for entry in view.trail
        if entry.content.get("stage") == "root_cause_reasoning"
    )
    assert unavailable.content == {
        "stage": "root_cause_reasoning",
        "status": "unavailable",
        "provider": "qwencloud",
        "model": "qwen3.7-max",
        "trace_id": "trace-triage",
    }
    assert "hypothesis" not in unavailable.content


def test_fast_fallback_plan_cannot_replace_missing_primary_reasoning() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion("not-json", model="qwen3.7-max"),
            _completion("not-json", model="qwen3.7-max"),
            _completion("not-json", model="qwen3.7-max"),
            _completion(_plan_json("Fallback plan"), model="qwen-flash"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(PlanGenerationError, match="reasoning was unavailable"):
        _run(agent, incident_id)

    view = store.view(incident_id, "trace")
    assert view.state is IncidentState.TRIAGED
    assert view.plan is None
    reasoning_entries = [
        entry
        for entry in view.trail
        if entry.content.get("stage") == "root_cause_reasoning"
    ]
    assert len(reasoning_entries) == 3
    assert all(entry.content["status"] == "unavailable" for entry in reasoning_entries)
    assert all("hypothesis" not in entry.content for entry in reasoning_entries)
    assert client.calls[-1]["role"] is ModelRole.FAST
    assert client.calls[-1]["thinking"] is False


def test_invalid_fast_fallback_leaves_incident_triaged() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion("bad", model="qwen3.7-max"),
            _completion("bad", model="qwen3.7-max"),
            _completion("bad", model="qwen3.7-max"),
            _completion("still-bad", model="qwen-flash"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(PlanGenerationError):
        _run(agent, incident_id)

    assert store.get(incident_id).state is IncidentState.TRIAGED
    assert store.view(incident_id, "trace").plan is None


def test_tool_round_cap_is_enforced_before_seventh_execution() -> None:
    store, incident_id = _store()
    completions = [_completion("timeout", model="qwen-flash")]
    completions.extend(
        _completion(
            None,
            model="qwen3.7-max",
            tool_calls=[_tool_call(call_id=f"call-{index}")],
        )
        for index in range(1, 8)
    )
    client = ScriptedClient(completions)
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(ToolRoundLimitError):
        _run(agent, incident_id)

    tool_results = [
        entry
        for entry in store.view(incident_id, "trace").trail
        if entry.type is TrailEntryType.TOOL_RESULT
    ]
    assert len(tool_results) == 6


def test_state_changing_tool_call_is_blocked_during_planning() -> None:
    store, incident_id = _store()
    client = ScriptedClient(
        [
            _completion("timeout", model="qwen-flash"),
            _completion(
                None,
                model="qwen3.7-max",
                tool_calls=[_tool_call(name="restart_service")],
            ),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(AgentRunError, match="invalid planning tool"):
        _run(agent, incident_id)

    trail = store.view(incident_id, "trace").trail
    rejected = trail[-2]
    assert rejected.type is TrailEntryType.TOOL_CALL
    assert rejected.content["status"] == "rejected"
    assert "args" not in rejected.content
    assert trail[-1].content == {
        "stage": "initial_triage",
        "status": "failed",
        "reason": "triage_failed",
        "trace_id": "trace-triage",
    }
    assert store.get(incident_id).state is IncidentState.TRIAGED


def test_automatic_triage_rejects_non_new_incident() -> None:
    store, incident_id = _store()
    store.transition(incident_id, IncidentState.TRIAGED)
    agent = TriageAgent(
        store,
        ScriptedClient([]),  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(InvalidTransitionError, match="while TRIAGED"):
        _run(agent, incident_id)


def test_initial_triage_failure_is_fixed_secret_safe_event_and_stays_new() -> None:
    store, incident_id = _store()
    secret = "provider-response-secret-sentinel"

    class FailingClient:
        async def chat(self, messages, **kwargs):
            del messages, kwargs
            raise RuntimeError(secret)

    agent = TriageAgent(
        store,
        FailingClient(),  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(RuntimeError, match=secret):
        asyncio.run(agent.run(incident_id, "trace-initial-failure"))

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.NEW
    assert view.trail[-1].type is TrailEntryType.THOUGHT
    assert view.trail[-1].content == {
        "stage": "initial_triage",
        "status": "failed",
        "reason": "triage_failed",
        "trace_id": "trace-initial-failure",
    }
    assert secret not in json.dumps(view.model_dump(mode="json"))


@pytest.mark.parametrize("content", [None, "", " \t\n"])
def test_empty_fast_classification_fails_without_qwen_attribution(
    content: str | None,
) -> None:
    store, incident_id = _store()
    client = ScriptedClient([_completion(content, model="qwen-flash")])
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        registry=build_tool_registry(),
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(AgentRunError, match="classification response was empty"):
        _run(agent, incident_id)

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.NEW
    assert view.plan is None
    assert len(view.trail) == 1
    assert view.trail[0].type is TrailEntryType.THOUGHT
    assert view.trail[0].content == {
        "stage": "initial_triage",
        "status": "failed",
        "reason": "triage_failed",
        "trace_id": "trace-triage",
    }
    assert view.trail[0].model_used is None


def test_initial_triage_planning_failure_preserves_triaged_state() -> None:
    store, incident_id = _store()
    secret = "planning-response-secret-sentinel"

    class FailingPlanningClient:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            self.calls += 1
            if self.calls == 1:
                return _completion("timeout", model="qwen-flash")
            raise RuntimeError(secret)

    agent = TriageAgent(
        store,
        FailingPlanningClient(),  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(RuntimeError, match=secret):
        asyncio.run(agent.run(incident_id, "trace-planning-failure"))

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.TRIAGED
    assert view.trail[-1].content == {
        "stage": "initial_triage",
        "status": "failed",
        "reason": "triage_failed",
        "trace_id": "trace-planning-failure",
    }
    assert secret not in json.dumps(view.model_dump(mode="json"))


def test_rejected_plan_is_regenerated_with_untrusted_correction_context() -> None:
    store, incident_id = _store()
    store.transition(incident_id, IncidentState.TRIAGED)
    store.store_plan(
        incident_id,
        parse_remediation_plan_json(
            _plan_json("Original plan"),
            registered_tools={"restart_service"},
        ),
        trace_id="0" * 32,
    )
    correction_secret = "operator-secret-key-sentinel"
    digest_secret = "operator-digest-response-sentinel"
    unknown_authorization_secret = "operator-custom-authorization-sentinel"
    authorization_tail_secret = "operator-authorization-tail-sentinel"
    quoted_authorization_secret = "operator-quoted-authorization-sentinel"
    quoted_authorization_tail = "operator-quoted-tail-sentinel"
    folded_authorization_secret = "operator-folded-authorization-sentinel"
    folded_authorization_tail = "operator-folded-tail-sentinel"
    unterminated_secret = "operator-unterminated-key-sentinel"
    _, correction = store.record_decision(
        incident_id,
        decision=ApprovalDecision.REJECT,
        operator="demo-operator",
        note=(
            "Inspect logs before choosing a bounded restart. "
            f"SECRET_KEY={correction_secret}\n"
            "Authorization: Digest username=\"operator\", "
            f"response=\"{digest_secret}\"\n"
            "Authorization: Token "
            f"{unknown_authorization_secret}; opaque={authorization_tail_secret}\n"
            f'Authorization: "{quoted_authorization_secret}" '
            f"{quoted_authorization_tail}\n"
            f"Authorization: Token {folded_authorization_secret};\r\n"
            f"\topaque={folded_authorization_tail}\r\n"
            f'api_key="{unterminated_secret}'
        ),
    )
    client = ScriptedClient(
        [
            _completion(
                _plan_json("Regenerated plan"),
                model="qwen3.7-max",
                reasoning="The correction calls for more evidence before restart.",
            )
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    asyncio.run(agent.regenerate(incident_id, "trace-regenerate", correction))

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.AWAITING_APPROVAL
    assert view.plan is not None
    assert view.plan.steps[0].action == "Regenerated plan"
    assert len(client.calls) == 1
    assert client.calls[0]["role"] is ModelRole.PRIMARY
    assert client.calls[0]["thinking"] is True
    context = json.loads(client.calls[0]["messages"][1]["content"])
    operator_correction = context["operator_correction"]
    assert operator_correction["trust"] == "untrusted_operator_input"
    assert operator_correction["decision"] == "reject"
    assert operator_correction["edits"] == []
    assert operator_correction["note"].count("[REDACTED]") == 6
    rendered_calls = json.dumps(client.calls)
    rendered_view = json.dumps(view.model_dump(mode="json"))
    for secret in (
        correction_secret,
        digest_secret,
        unknown_authorization_secret,
        authorization_tail_secret,
        quoted_authorization_secret,
        quoted_authorization_tail,
        folded_authorization_secret,
        folded_authorization_tail,
        unterminated_secret,
    ):
        assert secret not in rendered_calls
        assert secret not in rendered_view
    assert "raw-secret-sentinel" not in json.dumps(client.calls)


def test_regeneration_failure_stays_triaged_with_secret_safe_trace() -> None:
    store, incident_id = _store()
    store.transition(incident_id, IncidentState.TRIAGED)
    store.store_plan(
        incident_id,
        parse_remediation_plan_json(
            _plan_json("Original plan"),
            registered_tools={"restart_service"},
        ),
        trace_id="0" * 32,
    )
    _, correction = store.record_decision(
        incident_id,
        decision=ApprovalDecision.EDIT,
        operator="demo-operator",
        edits=(PlanEdit(seq=1, instruction="Use a 45s limit"),),
    )
    secret = "provider-body-secret-sentinel"

    class FailingClient:
        async def chat(self, messages, **kwargs):
            del messages, kwargs
            raise RuntimeError(secret)

    agent = TriageAgent(
        store,
        FailingClient(),  # type: ignore[arg-type]
        logger=logging.getLogger("praxis.test"),
    )

    with pytest.raises(RuntimeError, match=secret):
        asyncio.run(agent.regenerate(incident_id, "trace-regenerate", correction))

    view = store.view(incident_id, "trace-view")
    assert view.state is IncidentState.TRIAGED
    assert view.plan is None
    assert view.trail[-1].content == {
        "stage": "plan_regeneration",
        "status": "failed",
        "reason": "generation_failed",
        "trace_id": "trace-regenerate",
    }
    assert secret not in json.dumps(view.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"max_tool_rounds": MAX_TOOL_ROUNDS + 1},
            "max_tool_rounds must be an integer from 1 to 6",
        ),
        (
            {"max_plan_reprompts": MAX_PLAN_REPROMPTS + 1},
            "max_plan_reprompts must be an integer from 0 to 2",
        ),
    ],
)
def test_binding_retry_caps_cannot_be_raised(
    overrides: dict[str, int],
    message: str,
) -> None:
    store, _ = _store()

    with pytest.raises(ValueError, match=message):
        TriageAgent(
            store,
            ScriptedClient([]),  # type: ignore[arg-type]
            logger=logging.getLogger("praxis.test"),
            **overrides,  # type: ignore[arg-type]
        )
