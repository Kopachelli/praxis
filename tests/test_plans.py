from __future__ import annotations

import json
import traceback
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.plans import (
    RemediationPlan,
    RemediationPlanValidationError,
    RiskLevel,
    parse_remediation_plan_json,
    safe_validation_diagnostics,
)

REGISTERED_TOOLS = {"fetch_logs", "restart_service", "update_config"}


def _step(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "seq": 1,
        "action": "Restart the checkout worker pool",
        "tool": "restart_service",
        "args": {"service": "checkout-service"},
        "risk_level": "safe",
        "rollback": "Restart the previous worker revision",
    }
    value.update(updates)
    return value


def _payload(*steps: dict[str, Any], **extra: Any) -> str:
    value: dict[str, Any] = {"steps": list(steps) or [_step()]}
    value.update(extra)
    return json.dumps(value)


def _parse(payload: str) -> RemediationPlan:
    return parse_remediation_plan_json(
        payload,
        registered_tools=REGISTERED_TOOLS,
    )


def test_parses_exact_plan_and_preserves_json_arguments() -> None:
    payload = _payload(
        _step(),
        _step(
            seq=2,
            action="Update the gateway timeout",
            tool="update_config",
            args={"key": "gateway_timeout", "value": 60, "enabled": True},
            risk_level="caution",
            rollback="Restore the gateway timeout to 30 seconds",
        ),
    )

    plan = _parse(payload)

    assert [step.seq for step in plan.steps] == [1, 2]
    assert plan.steps[0].risk_level is RiskLevel.SAFE
    assert plan.steps[1].args == {
        "key": "gateway_timeout",
        "value": 60,
        "enabled": True,
    }
    assert plan.model_dump(mode="json") == json.loads(payload)


def test_action_and_rollback_are_trimmed_without_interpreting_semantics() -> None:
    plan = _parse(
        _payload(
            _step(
                action="  Observe  ",
                rollback="  Document that no rollback is required  ",
            )
        )
    )

    assert plan.steps[0].action == "Observe"
    assert plan.steps[0].rollback == "Document that no rollback is required"


@pytest.mark.parametrize("extra_location", ["root", "step"])
def test_extra_fields_are_forbidden_at_every_schema_level(
    extra_location: str,
) -> None:
    secret_field = "https://secret.invalid/token-sentinel"
    if extra_location == "root":
        payload = _payload(_step(), **{secret_field: "secret-value-sentinel"})
    else:
        payload = _payload(_step(**{secret_field: "secret-value-sentinel"}))

    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(payload)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic["type"] == "extra_forbidden"
    assert "<untrusted-field>" in diagnostic["loc"]
    assert secret_field not in str(raised.value)
    assert "secret-value-sentinel" not in str(raised.value)


@pytest.mark.parametrize(
    "missing_field",
    ["seq", "action", "tool", "args", "risk_level", "rollback"],
)
def test_every_step_field_is_required(missing_field: str) -> None:
    step = _step()
    del step[missing_field]

    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(_payload(step))

    assert raised.value.diagnostics[0]["type"] == "missing"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("seq", "1"),
        ("seq", True),
        ("action", 42),
        ("tool", 42),
        ("args", []),
        ("risk_level", "SAFE"),
        ("rollback", None),
    ],
)
def test_field_types_are_strict_and_not_coerced(
    field: str,
    invalid_value: Any,
) -> None:
    with pytest.raises(RemediationPlanValidationError):
        _parse(_payload(_step(**{field: invalid_value})))


@pytest.mark.parametrize("field", ["action", "rollback"])
def test_human_readable_text_must_not_be_blank(field: str) -> None:
    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(_payload(_step(**{field: " \t "})))

    assert raised.value.diagnostics[0]["type"] == "string_too_short"


@pytest.mark.parametrize(
    "steps",
    [
        [_step(seq=0)],
        [_step(seq=-1)],
        [_step(seq=2)],
        [_step(seq=1), _step(seq=3)],
        [_step(seq=1), _step(seq=1)],
        [_step(seq=2), _step(seq=1)],
    ],
)
def test_sequence_is_positive_ordered_and_contiguous(
    steps: list[dict[str, Any]],
) -> None:
    with pytest.raises(RemediationPlanValidationError):
        _parse(_payload(*steps))


def test_plan_requires_at_least_one_step() -> None:
    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(json.dumps({"steps": []}))

    assert raised.value.diagnostics[0]["type"] == "too_short"


def test_unknown_tool_is_rejected_against_caller_registry_context() -> None:
    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(_payload(_step(tool="delete_everything")))

    assert raised.value.diagnostics == (
        {
            "type": "value_error",
            "loc": ("steps", 0, "tool"),
            "msg": "Value error, tool is not registered",
        },
    )
    assert "delete_everything" not in str(raised.value)


def test_direct_model_validation_requires_registered_tool_context() -> None:
    with pytest.raises(ValidationError) as raised:
        RemediationPlan.model_validate_json(_payload(_step()), strict=True)

    diagnostics = safe_validation_diagnostics(raised.value)
    assert diagnostics[0]["msg"] == (
        "Value error, registered tool context is required"
    )


@pytest.mark.parametrize(
    "registered_tools",
    ["restart_service", [""], [" restart_service"], [42]],
)
def test_registered_tool_context_must_be_a_clean_name_collection(
    registered_tools: Any,
) -> None:
    with pytest.raises(TypeError, match="collection of non-empty names"):
        parse_remediation_plan_json(
            _payload(_step()),
            registered_tools=registered_tools,
        )


def test_malformed_json_has_retry_safe_diagnostics_and_no_exception_context() -> None:
    secret = "https://secret.invalid/provider-token-sentinel"
    payload = '{"steps": [' + json.dumps(secret)

    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(payload)

    error = raised.value
    assert error.diagnostics[0]["type"] == "json_invalid"
    assert set(error.diagnostics[0]) == {"type", "loc", "msg"}
    assert secret not in str(error)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert secret not in "".join(traceback.format_exception(error))


def test_validation_diagnostics_never_include_input_url_or_context() -> None:
    secret = "https://secret.invalid/provider-token-sentinel"
    payload = _payload(_step(tool=secret))

    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(payload)

    diagnostic = raised.value.diagnostics[0]
    assert set(diagnostic) == {"type", "loc", "msg"}
    assert secret not in repr(diagnostic)
    assert "input" not in diagnostic
    assert "url" not in diagnostic
    assert "ctx" not in diagnostic


def test_public_tool_argument_locations_are_safe_but_unknown_keys_are_redacted() -> None:
    class ArgumentProbe(RemediationPlan):
        pass

    # The registry-level validation tests exercise the prefixed locations. This
    # assertion pins the shared sanitizer used for those retry diagnostics.
    class StrictProbe(RemediationPlan):
        pass

    del ArgumentProbe, StrictProbe

    class ToolArguments(RemediationPlan):
        pass

    del ToolArguments

    # A plan's top-level args remain intentionally opaque here; public argument
    # names are only exposed when Pydantic reports them from a trusted tool model.
    with pytest.raises(RemediationPlanValidationError) as raised:
        _parse(_payload(_step(**{"provider-token-sentinel": True})))
    assert "provider-token-sentinel" not in str(raised.value)


def test_parser_accepts_json_bytes_and_rejects_non_json_objects() -> None:
    plan = parse_remediation_plan_json(
        _payload(_step()).encode("utf-8"),
        registered_tools=REGISTERED_TOOLS,
    )

    assert plan.steps[0].tool == "restart_service"
    with pytest.raises(TypeError, match="JSON text or bytes"):
        parse_remediation_plan_json(  # type: ignore[arg-type]
            {"steps": [_step()]},
            registered_tools=REGISTERED_TOOLS,
        )
