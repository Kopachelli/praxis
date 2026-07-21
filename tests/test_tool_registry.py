"""Contract and safety tests for the registered tool boundary [FR-5, FR-6]."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agent.plans import (
    RemediationPlan,
    RemediationPlanValidationError,
    parse_remediation_plan_json,
)
from app.agent.tools import (
    IncidentToolContext,
    ToolArgumentError,
    ToolExecutionMode,
    ToolPolicyError,
    ToolUnavailableError,
    UnknownToolError,
    build_tool_registry,
)
from app.agent.tools.real import MAX_EVIDENCE_TEXT_CHARS, MAX_LOG_ENTRIES


def _context(**changes: Any) -> IncidentToolContext:
    values: dict[str, Any] = {
        "incident_id": "inc-1",
        "source": "sentry",
        "service": "checkout-service",
        "severity": "high",
        "signal": "upstream_timeout",
        "title": "TimeoutError in checkout-service",
        "raw_payload": {
            "message": "Gateway timed out",
            "secret": "raw-secret-sentinel",
        },
    }
    values.update(changes)
    return IncidentToolContext(**values)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _tool_call(
    name: str,
    arguments: str | dict[str, Any],
    *,
    call_id: str = "call-1",
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _plan_step(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "seq": 1,
        "action": "Restart the checkout worker pool",
        "tool": "restart_service",
        "args": {"service": "checkout-service"},
        "risk_level": "safe",
        "rollback": "Restart the previous worker revision",
    }
    values.update(changes)
    return values


def _registry_plan(
    registry: Any,
    *steps: dict[str, Any],
) -> RemediationPlan:
    return parse_remediation_plan_json(
        json.dumps({"steps": list(steps)}),
        registered_tools=registry.names,
    )


def test_planning_schemas_are_openai_compatible_and_read_only() -> None:
    registry = build_tool_registry()

    schemas = registry.planning_schemas()

    assert [schema["type"] for schema in schemas] == ["function", "function"]
    assert [schema["function"]["name"] for schema in schemas] == [
        "fetch_logs",
        "service_status",
    ]
    for schema in schemas:
        parameters = schema["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["required"] == ["service"]
        assert parameters["additionalProperties"] is False
        assert parameters["properties"]["service"]["type"] == "string"
    assert not {
        "restart_service",
        "update_config",
        "scale_service",
        "rollback_deploy",
    } & {schema["function"]["name"] for schema in schemas}


def test_full_registry_matches_api_contract_and_is_returned_by_copy() -> None:
    registry = build_tool_registry()

    schemas = registry.registered_schemas()
    assert [schema["function"]["name"] for schema in schemas] == [
        "fetch_logs",
        "service_status",
        "restart_service",
        "update_config",
        "scale_service",
        "rollback_deploy",
    ]
    schemas[0]["function"]["name"] = "mutated"
    assert registry.registered_schemas()[0]["function"]["name"] == "fetch_logs"


@pytest.mark.parametrize(
    ("tool", "args", "risk", "error_type", "field"),
    [
        (
            "restart_service",
            {"service": 42},
            "safe",
            "string_type",
            "service",
        ),
        (
            "scale_service",
            {"service": "checkout-service", "replicas": "2"},
            "caution",
            "int_type",
            "replicas",
        ),
        (
            "update_config",
            {"key": "timeout", "value": 60, "provider-token-sentinel": True},
            "caution",
            "extra_forbidden",
            "<untrusted-field>",
        ),
    ],
)
def test_plan_validation_uses_exact_strict_tool_argument_model(
    tool: str,
    args: dict[str, Any],
    risk: str,
    error_type: str,
    field: str,
) -> None:
    registry = build_tool_registry()
    plan = _registry_plan(
        registry,
        _plan_step(tool=tool, args=args, risk_level=risk),
    )

    with pytest.raises(RemediationPlanValidationError) as raised:
        registry.validate_remediation_plan(plan)

    assert raised.value.diagnostics[0] == {
        "type": error_type,
        "loc": ("steps", 0, "args", field),
        "msg": raised.value.diagnostics[0]["msg"],
    }
    assert "provider-token-sentinel" not in str(raised.value)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("tool", "args", "registered_risk", "downgraded_risk"),
    [
        (
            "update_config",
            {"key": "timeout", "value": 60},
            "caution",
            "safe",
        ),
        (
            "scale_service",
            {"service": "checkout-service", "replicas": 2},
            "caution",
            "safe",
        ),
        (
            "rollback_deploy",
            {"service": "checkout-service", "version": "v1"},
            "dangerous",
            "caution",
        ),
    ],
)
def test_plan_validation_rejects_downgraded_write_risk(
    tool: str,
    args: dict[str, Any],
    registered_risk: str,
    downgraded_risk: str,
) -> None:
    registry = build_tool_registry()
    plan = _registry_plan(
        registry,
        _plan_step(tool=tool, args=args, risk_level=downgraded_risk),
    )

    with pytest.raises(RemediationPlanValidationError) as raised:
        registry.validate_remediation_plan(plan)

    assert raised.value.diagnostics == (
        {
            "type": "risk_policy",
            "loc": ("steps", 0, "risk_level"),
            "msg": (
                f"Write tool {tool} must use its registered risk level "
                f"{registered_risk}"
            ),
        },
    )


def test_plan_validation_requires_safe_read_risk_and_a_write_step() -> None:
    registry = build_tool_registry()
    risky_read = _registry_plan(
        registry,
        _plan_step(
            tool="fetch_logs",
            args={"service": "checkout-service"},
            risk_level="caution",
        ),
        _plan_step(seq=2),
    )
    read_only = _registry_plan(
        registry,
        _plan_step(
            tool="fetch_logs",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
        _plan_step(
            seq=2,
            tool="service_status",
            args={"service": "checkout-service"},
            risk_level="safe",
        ),
    )

    with pytest.raises(RemediationPlanValidationError) as risky_error:
        registry.validate_remediation_plan(risky_read)
    assert risky_error.value.diagnostics == (
        {
            "type": "risk_policy",
            "loc": ("steps", 0, "risk_level"),
            "msg": "Read tool fetch_logs must use risk level safe",
        },
    )

    with pytest.raises(RemediationPlanValidationError) as read_only_error:
        registry.validate_remediation_plan(read_only)
    assert read_only_error.value.diagnostics == (
        {
            "type": "missing_write_step",
            "loc": ("steps",),
            "msg": "Plan must include at least one registered write remediation step",
        },
    )


@pytest.mark.parametrize(
    "steps",
    [
        [_plan_step()],
        [
            _plan_step(
                tool="fetch_logs",
                args={"service": "checkout-service"},
                risk_level="safe",
            ),
            _plan_step(
                seq=2,
                tool="rollback_deploy",
                args={"service": "checkout-service", "version": "v1"},
                risk_level="dangerous",
            ),
        ],
    ],
)
def test_plan_validation_accepts_write_only_and_mixed_plans(
    steps: list[dict[str, Any]],
) -> None:
    registry = build_tool_registry()
    plan = _registry_plan(registry, *steps)

    assert registry.validate_remediation_plan(plan) is plan


def test_execute_all_supplied_calls_in_order_and_render_tool_messages() -> None:
    registry = build_tool_registry()
    calls = [
        _tool_call(
            "fetch_logs",
            json.dumps({"service": "checkout-service"}),
            call_id="call-logs",
        ),
        _tool_call(
            "service_status",
            {"service": "checkout-service"},
            call_id="call-status",
        ),
    ]

    results = _run(registry.execute_tool_calls(calls, context=_context()))

    assert [result.call_id for result in results] == ["call-logs", "call-status"]
    assert [result.name for result in results] == ["fetch_logs", "service_status"]
    assert [result.source for result in results] == [
        "incident_context",
        "incident_context",
    ]
    assert results[0].output["entries"] == [{"message": "Gateway timed out"}]
    assert results[1].output["status"] == "degraded"
    message = results[0].as_tool_message()
    assert message["role"] == "tool"
    assert message["tool_call_id"] == "call-logs"
    assert json.loads(message["content"])["source"] == "incident_context"
    json.dumps([result.model_dump(mode="json") for result in results])


def test_qwencloud_live_tool_call_shape_accepts_indexed_calls_in_order() -> None:
    registry = build_tool_registry()
    calls = [
        {
            "function": {
                "arguments": json.dumps({"service": "checkout-service"}),
                "name": "fetch_logs",
            },
            "id": "call-logs",
            "index": 0,
            "type": "function",
        },
        {
            "function": {
                "arguments": json.dumps({"service": "checkout-service"}),
                "name": "service_status",
            },
            "id": "call-status",
            "index": 1,
            "type": "function",
        },
    ]

    results = _run(registry.execute_tool_calls(calls, context=_context()))

    assert [result.call_id for result in results] == ["call-logs", "call-status"]
    assert [result.name for result in results] == ["fetch_logs", "service_status"]
    assert [result.source for result in results] == [
        "incident_context",
        "incident_context",
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        {"index": True},
        {"index": -1},
        {"index": "0"},
        {"index": 0.0},
        {"ordinal": 0},
        {"index": 0, "unexpected": True},
    ],
)
def test_tool_call_index_and_envelope_metadata_remain_strict(
    metadata: dict[str, Any],
) -> None:
    registry = build_tool_registry()
    call = _tool_call(
        "fetch_logs",
        json.dumps({"service": "checkout-service"}),
    )
    call.update(metadata)

    with pytest.raises(ToolArgumentError, match="malformed tool-call envelope"):
        _run(registry.execute_tool_calls([call], context=_context()))


def test_unknown_tool_is_rejected_without_execution() -> None:
    registry = build_tool_registry()

    with pytest.raises(UnknownToolError, match="unknown registered tool"):
        _run(
            registry.execute(
                "shell_exec",
                {"service": "checkout-service"},
                context=_context(),
            )
        )


@pytest.mark.parametrize(
    "arguments",
    [
        "not-json",
        "[]",
        "null",
        ["checkout-service"],
        {"service": "checkout-service", "unexpected": True},
        {"service": 123},
        {"service": ""},
    ],
)
def test_malformed_nonobject_and_extra_arguments_are_rejected(arguments: Any) -> None:
    registry = build_tool_registry()

    with pytest.raises(ToolArgumentError):
        _run(
            registry.execute(
                "fetch_logs",
                arguments,
                context=_context(),
            )
        )


@pytest.mark.parametrize(
    "tool_name",
    ["restart_service", "update_config", "scale_service", "rollback_deploy"],
)
def test_planning_can_never_execute_a_state_changing_tool(tool_name: str) -> None:
    registry = build_tool_registry()
    arguments: dict[str, Any] = {
        "restart_service": {"service": "checkout-service"},
        "update_config": {"key": "timeout", "value": 60},
        "scale_service": {"service": "checkout-service", "replicas": 2},
        "rollback_deploy": {
            "service": "checkout-service",
            "version": "v1",
        },
    }[tool_name]

    with pytest.raises(ToolPolicyError, match="blocked during planning"):
        _run(registry.execute(tool_name, arguments, context=_context()))


def test_restart_remains_unavailable_even_in_approved_execution_mode() -> None:
    registry = build_tool_registry()

    with pytest.raises(ToolUnavailableError, match="not executable"):
        _run(
            registry.execute(
                "restart_service",
                {"service": "checkout-service"},
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
            )
        )


def test_batch_preflight_blocks_write_before_any_read_executes() -> None:
    registry = build_tool_registry()
    raw_payload = {"message": "safe evidence"}
    context = _context(raw_payload=raw_payload)
    calls = [
        _tool_call("fetch_logs", {"service": "checkout-service"}),
        _tool_call("restart_service", {"service": "checkout-service"}),
    ]

    with pytest.raises(ToolPolicyError):
        _run(registry.execute_tool_calls(calls, context=context))

    assert raw_payload == {"message": "safe evidence"}


def test_batch_preflights_context_policy_before_any_read_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions: list[str] = []

    async def counted_fetch_logs(context: Any, *, service: str) -> dict[str, Any]:
        del context
        executions.append(service)
        return {"source": "incident_context"}

    monkeypatch.setattr(
        "app.agent.tools.registry.fetch_logs",
        counted_fetch_logs,
    )
    registry = build_tool_registry()
    calls = [
        _tool_call("fetch_logs", {"service": "checkout-service"}, call_id="call-1"),
        _tool_call("service_status", {"service": "other-service"}, call_id="call-2"),
    ]

    with pytest.raises(ToolPolicyError, match="outside the incident context"):
        _run(registry.execute_tool_calls(calls, context=_context()))

    assert executions == []


def test_fetch_logs_is_bounded_and_does_not_return_unallowlisted_raw_secrets() -> None:
    raw_payload = {
        "logs": [
            {
                "timestamp": f"2026-07-20T00:00:{index:02d}Z",
                "level": "error",
                "message": f"failure {index}",
                "secret": "nested-secret-sentinel",
            }
            for index in range(MAX_LOG_ENTRIES + 5)
        ],
        "authorization": "Bearer raw-secret-sentinel",
        "secret": "raw-secret-sentinel",
        "arbitrary": {"private_key": "nested-secret-sentinel"},
    }
    registry = build_tool_registry()

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=_context(raw_payload=raw_payload),
        )
    )
    rendered = json.dumps(result.model_dump(mode="json"))

    assert result.output["count"] == MAX_LOG_ENTRIES
    assert len(result.output["entries"]) == MAX_LOG_ENTRIES
    assert result.output["truncated"] is True
    assert "raw-secret-sentinel" not in rendered
    assert "nested-secret-sentinel" not in rendered
    assert "authorization" not in rendered.lower()
    assert "private_key" not in rendered


def test_inline_secrets_are_redacted_from_allowlisted_message_fields() -> None:
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "message": (
                "request failed Authorization: Bearer hidden-token "
                "api_key=hidden-key password=hidden-password"
            )
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    rendered = json.dumps(result.output)

    assert "hidden-token" not in rendered
    assert "hidden-key" not in rendered
    assert "hidden-password" not in rendered
    assert "[REDACTED]" in rendered


def test_nested_tool_evidence_redacts_key_aliases_and_full_authorization_payloads() -> None:
    secrets = (
        "tool-secret-key-sentinel",
        "tool-signing-key-sentinel",
        "dG9vbC11c2VyOnRvb2wtcGFzcw==",
        "tool-digest-response-sentinel",
        "tool-aws-signature-sentinel",
    )
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "extra": {
                "logs": [
                    {
                        "message": (
                            f"SECRET_KEY={secrets[0]}; "
                            f"webhookSigningKey={secrets[1]}; "
                            f"Authorization: Basic {secrets[2]}; basic-tail"
                        )
                    },
                    {
                        "message": (
                            "Authorization: Digest username=\"operator\", "
                            f"response=\"{secrets[3]}\"; digest-tail"
                        )
                    },
                    {
                        "message": (
                            "Authorization: AWS4-HMAC-SHA256 Credential=AKIA/route, "
                            f"SignedHeaders=host, Signature={secrets[4]}; aws-tail"
                        )
                    },
                ]
            }
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    rendered = json.dumps(result.model_dump(mode="json"))

    for secret in secrets:
        assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 5
    assert "basic-tail" not in rendered
    assert "digest-tail" not in rendered
    assert "aws-tail" not in rendered


def test_quoted_json_secrets_are_fully_redacted_without_losing_safe_text() -> None:
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "message": (
                'request failed {"api_key": "json-api-secret with spaces", '
                '"secret":"json-secret,with;punctuation", '
                '"password": "json-password\\\"suffix", '
                '"private_key": "-----BEGIN PRIVATE KEY-----\\nkey-material\\n-----END PRIVATE KEY-----", '
                '"correlation_id": "safe-correlation-42"} '
                "api_key=legacy-secret safe-tail "
                + ("x" * MAX_EVIDENCE_TEXT_CHARS)
            )
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    message = result.output["entries"][0]["message"]

    for secret_fragment in (
        "json-api-secret",
        "json-secret",
        "json-password",
        "key-material",
        "legacy-secret",
    ):
        assert secret_fragment not in message
    assert message.count("[REDACTED]") == 5
    assert '"correlation_id": "safe-correlation-42"' in message
    assert "safe-tail" in message
    assert len(message) == MAX_EVIDENCE_TEXT_CHARS + 1
    assert message.endswith("…")


def test_escaped_json_envelope_secrets_are_fully_redacted() -> None:
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "message": (
                r'prefix {\"message\":\"safe evidence\", '
                r'\"api_key\":\"ESCAPED_API with spaces\", '
                r'\"secret\":\"ESCAPED_SECRET,with;punctuation\", '
                r'\"password\":\"ESCAPED_PASSWORD\", '
                r'\"private_\u006bey\":\"ESCAPED_PRIVATE_KEY material\"} suffix'
            )
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    message = result.output["entries"][0]["message"]

    for secret_fragment in (
        "ESCAPED_API",
        "ESCAPED_SECRET",
        "ESCAPED_PASSWORD",
        "ESCAPED_PRIVATE_KEY",
    ):
        assert secret_fragment not in message
    assert message.count("[REDACTED]") == 4
    assert r'\"message\":\"safe evidence\"' in message
    assert message.startswith("prefix ")
    assert message.endswith(" suffix")


@pytest.mark.parametrize("key", ["apikey", "accesskey", "privatekey"])
def test_escaped_json_separatorless_sensitive_aliases_are_redacted(key: str) -> None:
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "message": (
                rf'prefix {{\"message\":\"safe evidence\",'
                rf'\"{key}\":\"SEPARATORLESS_SECRET with spaces\"}} suffix'
            )
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    message = result.output["entries"][0]["message"]

    assert "SEPARATORLESS_SECRET" not in message
    assert r'\"message\":\"safe evidence\"' in message
    assert message.startswith("prefix ")
    assert message.endswith(" suffix")


@pytest.mark.parametrize(
    ("encoded_key", "secret"),
    [
        (r"api_\u006bey", "UNICODE_API_SECRET"),
        (r"secr\u0065t", "UNICODE_GENERIC_SECRET"),
        (r"passw\u006frd", "UNICODE_PASSWORD_SECRET"),
        (r"private_\u006bey", "UNICODE_PRIVATE_KEY_SECRET"),
    ],
)
def test_unicode_escaped_sensitive_keys_are_redacted(
    encoded_key: str,
    secret: str,
) -> None:
    registry = build_tool_registry()
    context = _context(
        raw_payload={
            "message": (
                f'prefix {{"message":"safe evidence","{encoded_key}":'
                f'"{secret} with spaces"}} suffix'
            )
        }
    )

    result = _run(
        registry.execute(
            "fetch_logs",
            {"service": "checkout-service"},
            context=context,
        )
    )
    message = result.output["entries"][0]["message"]

    assert secret not in message
    assert '"message":"safe evidence"' in message
    assert message.startswith("prefix ")
    assert message.endswith(" suffix")


def test_tool_cannot_read_a_service_outside_injected_incident_context() -> None:
    registry = build_tool_registry()

    with pytest.raises(ToolPolicyError, match="outside the incident context"):
        _run(
            registry.execute(
                "service_status",
                {"service": "other-service"},
                context=_context(),
            )
        )


@pytest.mark.parametrize(
    "tool_calls",
    [
        "not-a-list",
        [{"id": "call-1", "type": "function", "function": {"name": "fetch_logs"}}],
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "fetch_logs",
                    "arguments": {"service": "checkout-service"},
                    "extra": True,
                },
            }
        ],
    ],
)
def test_malformed_tool_call_envelopes_are_rejected(tool_calls: Any) -> None:
    registry = build_tool_registry()

    with pytest.raises(ToolArgumentError):
        _run(registry.execute_tool_calls(tool_calls, context=_context()))
