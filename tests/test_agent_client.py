from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

import httpx
import pytest

from app.agent.client import (
    ChatCompletion,
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    ModelRole,
    QwenCallError,
    QwenClient,
    QwenConfigurationError,
    QwenExhaustedError,
)
from app.config import (
    QWEN_CLOUD_BASE_URL,
    RUNTIME_OPENROUTER_MODELS,
    RUNTIME_QWENCLOUD_MODELS,
    Settings,
)
from app.trail import DecisionTrailStore, TrailEntryType


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "dev",
        "app_version": "test",
        "deployed_on": "local",
        "port": 8000,
        "provider_order": ("qwencloud", "openrouter"),
        "primary_model": "qwen3.7-max",
        "fast_model": "qwen-flash",
        "qwen_base_url": QWEN_CLOUD_BASE_URL,
        "qwencloud_models": RUNTIME_QWENCLOUD_MODELS,
        "openrouter_models": RUNTIME_OPENROUTER_MODELS,
        "dashscope_api_key": "dashscope-secret-sentinel",
        "openrouter_api_key": "openrouter-secret-sentinel",
        "fc_function_name": "",
        "fc_instance_id": "",
        "fc_region": "",
        "openrouter_fast_model": "qwen/qwen3.6-flash",
    }
    values.update(updates)
    return Settings(**values)


def _completion(
    content: Any = "ok",
    *,
    message_extra: dict[str, Any] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message = {"role": "assistant", "content": content}
    message.update(message_extra or {})
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


Action = tuple[int, Any] | str


class ScriptedTransport:
    def __init__(self, actions: list[Action]) -> None:
        self.actions = list(actions)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.actions:
            raise AssertionError("unexpected provider request")
        action = self.actions.pop(0)
        if action == "timeout":
            raise httpx.ReadTimeout("provider-body-secret-sentinel", request=request)
        if action == "connect_error":
            raise httpx.ConnectError("provider-body-secret-sentinel", request=request)
        if action == "invalid_json":
            return httpx.Response(
                200,
                content=b"provider-body-secret-sentinel",
                headers={"Content-Type": "application/json"},
            )
        if action == "recursive_json":
            return httpx.Response(
                200,
                content=("[" * 1_100 + "0" + "]" * 1_100).encode("ascii"),
                headers={"Content-Type": "application/json"},
            )
        if action == "recursive_response":
            nested_content = "[" * 500 + "0" + "]" * 500
            return httpx.Response(
                200,
                content=(
                    '{"choices":[{"message":{"role":"assistant","content":'
                    + nested_content
                    + '},"finish_reason":"stop"}]}'
                ).encode("ascii"),
                headers={"Content-Type": "application/json"},
            )
        status_code, payload = action
        return httpx.Response(
            status_code,
            content=json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii"),
            headers={"Content-Type": "application/json"},
        )


def _execute(
    transport: ScriptedTransport,
    *,
    settings: Settings | None = None,
    trail: DecisionTrailStore | None = None,
    role: ModelRole = ModelRole.PRIMARY,
    thinking: bool = False,
    tools: list[dict[str, Any]] | None = None,
    attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    logger: logging.Logger | None = None,
) -> ChatCompletion:
    async def run() -> ChatCompletion:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as http_client:
            client = QwenClient(
                settings or _settings(),
                trail=trail,
                attempt_timeout_seconds=attempt_timeout_seconds,
                call_timeout_seconds=call_timeout_seconds,
                http_client=http_client,
                logger=logger,
            )
            return await client.chat(
                [{"role": "user", "content": "triage"}],
                role=role,
                thinking=thinking,
                tools=tools,
                incident_id="inc-1",
                trace_id="trace-1",
            )

    return asyncio.run(run())


def _request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _terminal_content(
    provider: str,
    model: str,
    *,
    outcome: str,
    reason: str,
) -> dict[str, str]:
    return {
        "provider": provider,
        "model": model,
        "outcome": outcome,
        "reason": reason,
        "trace_id": "trace-1",
    }


def test_single_attempt_success_records_only_allowlisted_terminal_outcome() -> None:
    transport = ScriptedTransport(
        [(200, _completion("model-output-secret-sentinel"))]
    )
    trail = DecisionTrailStore()

    result = _execute(transport, trail=trail)

    assert result.provider == "qwencloud"
    assert result.model == "qwen3.7-max"
    entries = trail.list_for_incident("inc-1")
    assert len(entries) == 1
    assert entries[0].type is TrailEntryType.QWEN_ATTEMPT
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="success",
        reason="success",
    )
    assert entries[0].model_used == "qwen3.7-max"
    assert entries[0].tokens is None
    rendered = str(entries[0].model_dump(mode="json"))
    assert "model-output-secret-sentinel" not in rendered
    assert "dashscope-secret-sentinel" not in rendered
    assert "openrouter-secret-sentinel" not in rendered


def test_primary_route_exhausts_qwen_cloud_before_openrouter() -> None:
    settings = _settings(
        qwencloud_models=("qwen3.7-max", "qwen3-max", "qwen-plus"),
        openrouter_models=(
            "qwen/qwen3.7-max",
            "qwen/qwen3-max",
            "qwen/qwen-plus",
        ),
    )
    transport = ScriptedTransport(
        [(503, {}), (503, {}), (503, {}), (200, _completion())]
    )
    trail = DecisionTrailStore()

    result = _execute(transport, settings=settings, trail=trail)

    assert result.provider == "openrouter"
    assert result.model == "qwen/qwen3.7-max"
    assert [_request_json(request)["model"] for request in transport.requests] == [
        "qwen3.7-max",
        "qwen3-max",
        "qwen-plus",
        "qwen/qwen3.7-max",
    ]
    assert [request.url.host for request in transport.requests] == [
        "dashscope-intl.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "openrouter.ai",
    ]
    assert [request.headers["authorization"] for request in transport.requests] == [
        "Bearer dashscope-secret-sentinel",
        "Bearer dashscope-secret-sentinel",
        "Bearer dashscope-secret-sentinel",
        "Bearer openrouter-secret-sentinel",
    ]
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [
        TrailEntryType.FALLBACK,
        TrailEntryType.FALLBACK,
        TrailEntryType.FALLBACK,
        TrailEntryType.QWEN_ATTEMPT,
    ]
    assert [entry.content for entry in entries] == [
        {
            "from": "qwencloud/qwen3.7-max",
            "to": "qwencloud/qwen3-max",
            "reason": "http_503",
        },
        {
            "from": "qwencloud/qwen3-max",
            "to": "qwencloud/qwen-plus",
            "reason": "http_503",
        },
        {
            "from": "qwencloud/qwen-plus",
            "to": "openrouter/qwen/qwen3.7-max",
            "reason": "http_503",
        },
        _terminal_content(
            "openrouter",
            "qwen/qwen3.7-max",
            outcome="success",
            reason="success",
        ),
    ]


@pytest.mark.parametrize("status_code", [401, 402, 403, 404, 408, 429, 500, 599])
def test_only_documented_http_failures_enter_fallback(status_code: int) -> None:
    transport = ScriptedTransport(
        [
            (status_code, {"secret": "provider-body-secret-sentinel"}),
            (status_code, {}),
            (status_code, {}),
            (200, _completion()),
        ]
    )
    trail = DecisionTrailStore()

    result = _execute(transport, trail=trail)

    assert result.provider == "openrouter"
    assert len(transport.requests) == 4
    entries = trail.list_for_incident("inc-1")
    assert len(entries) == 4
    assert entries[-2].type is TrailEntryType.FALLBACK
    assert entries[-2].content == {
        "from": "qwencloud/qwen-plus",
        "to": "openrouter/qwen/qwen3.7-max",
        "reason": f"http_{status_code}",
    }
    assert entries[-1].type is TrailEntryType.QWEN_ATTEMPT
    assert entries[-1].content == _terminal_content(
        "openrouter",
        "qwen/qwen3.7-max",
        outcome="success",
        reason="success",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "error": {
                "code": "model_not_found",
                "message": "provider-body-secret-sentinel",
            }
        },
        {
            "error": {
                "code": 400,
                "message": "qwen/qwen-flash is not a valid model ID",
            }
        },
        {
            "code": "ModelNotFound",
            "message": "provider-body-secret-sentinel",
        },
    ],
)
def test_explicit_model_unavailable_http_400_enters_fallback(
    payload: dict[str, Any],
) -> None:
    transport = ScriptedTransport([(400, payload), (200, _completion())])
    trail = DecisionTrailStore()

    result = _execute(transport, trail=trail)

    assert (result.provider, result.model) == ("qwencloud", "qwen3-max")
    assert len(transport.requests) == 2
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [
        TrailEntryType.FALLBACK,
        TrailEntryType.QWEN_ATTEMPT,
    ]
    assert entries[0].content == {
        "from": "qwencloud/qwen3.7-max",
        "to": "qwencloud/qwen3-max",
        "reason": "model_unavailable",
    }
    assert entries[1].content == _terminal_content(
        "qwencloud",
        "qwen3-max",
        outcome="success",
        reason="success",
    )
    assert "provider-body-secret-sentinel" not in str(entries)


def test_generic_model_related_http_400_remains_terminal() -> None:
    transport = ScriptedTransport(
        [
            (
                400,
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "The model input must be a string",
                    }
                },
            )
        ]
    )

    with pytest.raises(QwenCallError) as raised:
        _execute(transport)

    assert raised.value.reason == "http_400"
    assert len(transport.requests) == 1


def test_oversized_model_unavailable_http_400_remains_terminal() -> None:
    transport = ScriptedTransport(
        [
            (
                400,
                {
                    "error": {
                        "code": "model_not_found",
                        "message": "x" * 5_000,
                    }
                },
            )
        ]
    )

    with pytest.raises(QwenCallError) as raised:
        _execute(transport)

    assert raised.value.reason == "http_400"
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status_code", [300, 400, 405, 409, 422, 499])
def test_undocumented_http_failures_are_terminal(status_code: int) -> None:
    transport = ScriptedTransport(
        [(status_code, {"secret": "provider-body-secret-sentinel"})]
    )
    trail = DecisionTrailStore()

    with pytest.raises(QwenCallError) as raised:
        _execute(transport, trail=trail)

    assert raised.value.reason == f"http_{status_code}"
    assert len(transport.requests) == 1
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [TrailEntryType.QWEN_ATTEMPT]
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="failure",
        reason=f"http_{status_code}",
    )
    assert "provider-body-secret-sentinel" not in str(raised.value)
    assert "provider-body-secret-sentinel" not in str(entries[0].content)


def test_httpx_timeout_falls_back_with_safe_reason() -> None:
    transport = ScriptedTransport(
        ["timeout", "timeout", "timeout", (200, _completion())]
    )
    trail = DecisionTrailStore()

    result = _execute(transport, trail=trail)

    assert result.provider == "openrouter"
    entries = trail.list_for_incident("inc-1")
    assert all(
        entry.content["reason"] == "timeout"
        for entry in entries
        if entry.type is TrailEntryType.FALLBACK
    )
    assert entries[-1].type is TrailEntryType.QWEN_ATTEMPT
    assert entries[-1].content["reason"] == "success"
    assert "provider-body-secret-sentinel" not in str(
        entries[0].content
    )


def test_each_fallback_emits_one_correlated_secret_safe_structured_log() -> None:
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.Logger("praxis.test.qwen-fallback")
    logger.addHandler(CaptureHandler())
    transport = ScriptedTransport(
        [
            (503, {"secret": "provider-body-secret-sentinel"}),
            (200, _completion()),
        ]
    )
    trail = DecisionTrailStore()

    _execute(transport, trail=trail, logger=logger)

    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == (
        "qwen_provider_fallback "
        "from=qwencloud/qwen3.7-max "
        "to=qwencloud/qwen3-max reason=http_503"
    )
    assert record.incident_id == "inc-1"  # type: ignore[attr-defined]
    assert record.trace_id == "trace-1"  # type: ignore[attr-defined]
    assert getattr(record, "from") == "qwencloud/qwen3.7-max"
    assert record.to == "qwencloud/qwen3-max"  # type: ignore[attr-defined]
    assert record.reason == "http_503"  # type: ignore[attr-defined]
    rendered_record = str(record.__dict__)
    assert "provider-body-secret-sentinel" not in rendered_record
    assert "dashscope-secret-sentinel" not in rendered_record
    assert "openrouter-secret-sentinel" not in rendered_record
    entries = trail.list_for_incident("inc-1")
    assert entries[0].content == {
        "from": "qwencloud/qwen3.7-max",
        "to": "qwencloud/qwen3-max",
        "reason": "http_503",
    }
    assert entries[-1].type is TrailEntryType.QWEN_ATTEMPT


def test_wall_clock_attempt_deadline_reaches_openrouter_after_slow_qwen_cloud() -> None:
    requests: list[httpx.Request] = []

    async def slow_qwen_cloud(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "dashscope-intl.aliyuncs.com":
            # MockTransport performs no socket I/O, so HTTPX phase timeouts do
            # not interrupt this slowly progressing handler. The application
            # wall-clock deadline must cancel it and advance the route.
            await asyncio.sleep(1)
        return httpx.Response(200, json=_completion("openrouter-ok"))

    async def run() -> tuple[ChatCompletion, float, DecisionTrailStore]:
        trail = DecisionTrailStore()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_qwen_cloud)
        ) as http_client:
            client = QwenClient(
                _settings(),
                trail=trail,
                attempt_timeout_seconds=0.01,
                call_timeout_seconds=0.25,
                http_client=http_client,
            )
            started = asyncio.get_running_loop().time()
            result = await client.chat(
                [{"role": "user", "content": "triage"}],
                incident_id="inc-1",
                trace_id="trace-1",
            )
            return result, asyncio.get_running_loop().time() - started, trail

    result, elapsed, trail = asyncio.run(run())

    assert result.provider == "openrouter"
    assert result.model == "qwen/qwen3.7-max"
    assert result.content == "openrouter-ok"
    assert elapsed < 0.2
    assert [_request_json(request)["model"] for request in requests] == [
        "qwen3.7-max",
        "qwen3-max",
        "qwen-plus",
        "qwen/qwen3.7-max",
    ]
    entries = trail.list_for_incident("inc-1")
    assert [
        entry.content["reason"]
        for entry in entries
        if entry.type is TrailEntryType.FALLBACK
    ] == [
        "timeout",
        "timeout",
        "timeout",
    ]
    assert entries[-1].content == _terminal_content(
        "openrouter",
        "qwen/qwen3.7-max",
        outcome="success",
        reason="success",
    )


def test_logical_call_deadline_caps_route_and_redacts_timeout_context() -> None:
    requests: list[httpx.Request] = []

    async def never_finishes(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json=_completion())

    async def run() -> tuple[QwenExhaustedError, float, DecisionTrailStore]:
        trail = DecisionTrailStore()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(never_finishes)
        ) as http_client:
            client = QwenClient(
                _settings(),
                trail=trail,
                attempt_timeout_seconds=0.2,
                call_timeout_seconds=0.03,
                http_client=http_client,
            )
            started = asyncio.get_running_loop().time()
            with pytest.raises(QwenExhaustedError) as raised:
                await client.chat(
                    [{"role": "user", "content": "triage"}],
                    incident_id="inc-1",
                    trace_id="trace-1",
                )
            return raised.value, asyncio.get_running_loop().time() - started, trail

    error, elapsed, trail = asyncio.run(run())

    assert error.provider == "qwencloud"
    assert error.model == "qwen3.7-max"
    assert error.reason == "logical_timeout"
    assert error.__context__ is None
    assert error.__cause__ is None
    assert elapsed < 0.15
    assert len(requests) == 1
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [TrailEntryType.QWEN_ATTEMPT]
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="failure",
        reason="logical_timeout",
    )


def test_non_timeout_transport_error_is_terminal_and_redacted() -> None:
    transport = ScriptedTransport(["connect_error"])
    trail = DecisionTrailStore()

    with pytest.raises(QwenCallError) as raised:
        _execute(transport, trail=trail)

    assert raised.value.reason == "transport_error"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert len(transport.requests) == 1
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [TrailEntryType.QWEN_ATTEMPT]
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="failure",
        reason="transport_error",
    )
    assert "provider-body-secret-sentinel" not in str(raised.value)
    assert "provider-body-secret-sentinel" not in str(entries[0].content)
    assert "provider-body-secret-sentinel" not in "".join(
        traceback.format_exception(raised.value)
    )


def test_terminal_documented_failure_does_not_invent_a_transition() -> None:
    transport = ScriptedTransport([(503, {})] * 6)
    trail = DecisionTrailStore()

    with pytest.raises(QwenExhaustedError) as raised:
        _execute(transport, trail=trail)

    assert raised.value.provider == "openrouter"
    assert raised.value.reason == "http_503"
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [
        *([TrailEntryType.FALLBACK] * 5),
        TrailEntryType.QWEN_ATTEMPT,
    ]
    assert entries[-1].content == _terminal_content(
        "openrouter",
        "qwen/qwen-plus",
        outcome="failure",
        reason="http_503",
    )
    assert all(
        entry.content.get("to") != ""
        for entry in entries
        if entry.type is TrailEntryType.FALLBACK
    )


def test_final_timeout_has_no_provider_exception_context_or_traceback_secret() -> None:
    transport = ScriptedTransport(["timeout"] * 6)

    with pytest.raises(QwenExhaustedError) as raised:
        _execute(transport)

    assert raised.value.reason == "timeout"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    rendered = "".join(traceback.format_exception(raised.value))
    assert "provider-body-secret-sentinel" not in rendered


def test_missing_primary_credential_fails_closed_without_network_or_fallback() -> None:
    transport = ScriptedTransport([])
    settings = _settings(dashscope_api_key="")
    trail = DecisionTrailStore()

    with pytest.raises(QwenConfigurationError, match="qwencloud credential"):
        _execute(transport, settings=settings, trail=trail)

    assert transport.requests == []
    assert trail.list_for_incident("inc-1") == []


def test_missing_fallback_credential_does_not_record_unattemptable_transition() -> None:
    transport = ScriptedTransport([(503, {})] * 3)
    settings = _settings(openrouter_api_key="")
    trail = DecisionTrailStore()

    with pytest.raises(QwenConfigurationError, match="openrouter credential"):
        _execute(transport, settings=settings, trail=trail)

    assert len(transport.requests) == 3
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [
        TrailEntryType.FALLBACK,
        TrailEntryType.FALLBACK,
        TrailEntryType.QWEN_ATTEMPT,
    ]
    assert entries[-1].content == _terminal_content(
        "qwencloud",
        "qwen-plus",
        outcome="failure",
        reason="http_503",
    )
    assert all(
        entry.content.get("to") != "openrouter/qwen/qwen3.7-max"
        for entry in entries
        if entry.type is TrailEntryType.FALLBACK
    )


def test_timeout_before_missing_fallback_credential_has_no_provider_context() -> None:
    transport = ScriptedTransport(["timeout"] * 3)
    settings = _settings(openrouter_api_key="")

    with pytest.raises(QwenConfigurationError) as raised:
        _execute(transport, settings=settings)

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    rendered = "".join(traceback.format_exception(raised.value))
    assert "provider-body-secret-sentinel" not in rendered


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("invalid_json", "invalid_json"),
        ("recursive_json", "invalid_json"),
        ("recursive_response", "invalid_response"),
        ((200, {"choices": []}), "invalid_response"),
    ],
)
def test_success_schema_errors_are_terminal(action: Action, reason: str) -> None:
    transport = ScriptedTransport([action])
    trail = DecisionTrailStore()

    with pytest.raises(QwenCallError) as raised:
        _execute(transport, trail=trail)

    assert raised.value.reason == reason
    assert raised.value.__context__ is None
    assert len(transport.requests) == 1
    entries = trail.list_for_incident("inc-1")
    assert [entry.type for entry in entries] == [TrailEntryType.QWEN_ATTEMPT]
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="failure",
        reason=reason,
    )
    assert "provider-body-secret-sentinel" not in str(raised.value)
    assert "provider-body-secret-sentinel" not in str(entries[0].content)
    assert "provider-body-secret-sentinel" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.parametrize(
    "payload",
    (
        _completion("unsafe-content-\ud800-secret-sentinel"),
        _completion(
            "visible",
            message_extra={
                "reasoning_content": "unsafe-reasoning-\udfff-secret-sentinel"
            },
        ),
        _completion(
            None,
            message_extra={
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "fetch_logs",
                            "arguments": '{"service":"unsafe-\ud800-secret-sentinel"}',
                        },
                    }
                ]
            },
            finish_reason="tool_calls",
        ),
        {
            **_completion("visible"),
            "provider_metadata": {
                "unsafe-key-\ud800-secret-sentinel": "metadata"
            },
        },
    ),
)
def test_non_scalar_unicode_anywhere_in_provider_response_is_terminal(
    payload: dict[str, Any],
) -> None:
    transport = ScriptedTransport([(200, payload)])
    trail = DecisionTrailStore()

    with pytest.raises(QwenCallError) as raised:
        _execute(transport, trail=trail)

    assert raised.value.reason == "invalid_response"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    rendered_error = "".join(traceback.format_exception(raised.value))
    assert "secret-sentinel" not in rendered_error
    entries = trail.list_for_incident("inc-1")
    assert len(entries) == 1
    assert entries[0].content == _terminal_content(
        "qwencloud",
        "qwen3.7-max",
        outcome="failure",
        reason="invalid_response",
    )
    serialized_trail = json.dumps(
        [entry.model_dump(mode="json") for entry in entries],
        ensure_ascii=False,
    ).encode("utf-8")
    assert b"invalid_response" in serialized_trail
    assert b"secret-sentinel" not in serialized_trail


def test_valid_non_bmp_unicode_in_provider_response_remains_serializable() -> None:
    result = _execute(
        ScriptedTransport(
            [
                (
                    200,
                    _completion(
                        "Rocket \U0001f680",
                        message_extra={"reasoning_content": "Check \U0001f50d"},
                    ),
                )
            ]
        )
    )

    assert result.content == "Rocket \U0001f680"
    assert result.reasoning_content == "Check \U0001f50d"
    assert json.dumps(result.raw_response, ensure_ascii=False).encode("utf-8")


def test_fast_role_uses_adr_009_provider_specific_models() -> None:
    transport = ScriptedTransport([(408, {}), (200, _completion())])
    trail = DecisionTrailStore()

    result = _execute(transport, role=ModelRole.FAST, trail=trail)

    assert result.provider == "openrouter"
    assert [_request_json(request)["model"] for request in transport.requests] == [
        "qwen-flash",
        "qwen/qwen3.6-flash",
    ]
    entries = trail.list_for_incident("inc-1")
    assert entries[0].content == {
        "from": "qwencloud/qwen-flash",
        "to": "openrouter/qwen/qwen3.6-flash",
        "reason": "http_408",
    }
    assert entries[1].type is TrailEntryType.QWEN_ATTEMPT
    assert entries[1].content == _terminal_content(
        "openrouter",
        "qwen/qwen3.6-flash",
        outcome="success",
        reason="success",
    )


def test_fast_role_rejects_thinking_before_network() -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ValueError, match="primary reasoning role"):
        _execute(transport, role=ModelRole.FAST, thinking=True)

    assert transport.requests == []


def test_provider_specific_thinking_and_tool_payloads_are_preserved() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "fetch_logs",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    transport = ScriptedTransport(
        [
            (503, {}),
            (503, {}),
            (503, {}),
            (
                200,
                _completion(
                    None,
                    message_extra={
                        "reasoning": "diagnostic reasoning",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "fetch_logs", "arguments": "{}"},
                            }
                        ],
                    },
                    finish_reason="tool_calls",
                ),
            ),
        ]
    )

    result = _execute(transport, thinking=True, tools=[tool])

    qwen_payload = _request_json(transport.requests[0])
    openrouter_payload = _request_json(transport.requests[-1])
    assert qwen_payload["enable_thinking"] is True
    assert "reasoning" not in qwen_payload
    assert openrouter_payload["reasoning"] == {"enabled": True}
    assert "enable_thinking" not in openrouter_payload
    assert qwen_payload["tools"] == [tool]
    assert openrouter_payload["tools"] == [tool]
    assert result.reasoning_content == "diagnostic reasoning"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0]["function"]["name"] == "fetch_logs"
    assert result.usage["total_tokens"] == 5


def test_inline_thinking_is_separated_from_visible_content() -> None:
    transport = ScriptedTransport(
        [(200, _completion("<think>private reasoning</think>public answer"))]
    )

    result = _execute(transport)

    assert result.reasoning_content == "private reasoning"
    assert result.visible_content == "public answer"


def test_result_accessors_return_isolated_copies() -> None:
    transport = ScriptedTransport([(200, _completion("original"))])

    result = _execute(transport)
    raw = result.raw_response
    raw["choices"][0]["message"]["content"] = "changed"
    message = result.message
    message["content"] = "also changed"

    assert result.content == "original"


def test_each_attempt_receives_phase_timeout_at_or_below_wall_clock_deadline() -> None:
    transport = ScriptedTransport([(503, {}), (200, _completion())])

    _execute(transport, attempt_timeout_seconds=12.5)

    for request in transport.requests:
        assert request.extensions["timeout"] == {
            "connect": 12.5,
            "read": 12.5,
            "write": 12.5,
            "pool": 12.5,
        }


def test_adr_013_production_deadline_defaults_are_fixed() -> None:
    assert DEFAULT_ATTEMPT_TIMEOUT_SECONDS == 15.0
    assert DEFAULT_CALL_TIMEOUT_SECONDS == 90.0

    transport = ScriptedTransport([(200, _completion())])
    _execute(transport)

    assert transport.requests[0].extensions["timeout"] == {
        "connect": 15.0,
        "read": 15.0,
        "write": 15.0,
        "pool": 15.0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qwencloud_models", ()),
        ("qwencloud_models", ("qwen-plus",)),
        (
            "qwencloud_models",
            ("qwen3.8-max-preview", "qwen3.7-max", "qwen3-max", "qwen-plus"),
        ),
        ("qwencloud_models", ("qwen-fake",)),
        ("qwencloud_models", ("qwen-plus", "qwen3.7-max")),
        ("qwencloud_models", ("qwen3.7-max", "qwen3.7-max")),
        ("openrouter_models", ()),
        ("openrouter_models", ("qwen/qwen-plus",)),
        (
            "openrouter_models",
            (
                "qwen/qwen3.8-max-preview",
                "qwen/qwen3.7-max",
                "qwen/qwen3-max",
                "qwen/qwen-plus",
            ),
        ),
        ("openrouter_models", ("qwen/fake",)),
        ("openrouter_models", ("qwen/qwen-plus", "qwen/qwen3.7-max")),
        ("fast_model", "qwen-fast-other"),
        ("openrouter_fast_model", "qwen/qwen-flash"),
    ],
)
def test_unaccepted_manual_model_settings_are_rejected_before_network(
    field: str,
    value: Any,
) -> None:
    transport = ScriptedTransport([])
    settings = _settings(**{field: value})

    with pytest.raises(QwenConfigurationError):
        _execute(transport, settings=settings)

    assert transport.requests == []


def test_manual_primary_model_must_lead_reasoning_chain() -> None:
    transport = ScriptedTransport([])
    settings = _settings(
        primary_model="qwen3-max",
    )

    with pytest.raises(QwenConfigurationError, match="PRIMARY_MODEL"):
        _execute(transport, settings=settings)

    assert transport.requests == []


def test_manual_malicious_qwen_url_is_rejected_before_bearer_can_leave() -> None:
    transport = ScriptedTransport([])
    settings = _settings(
        qwen_base_url="https://attacker.invalid/compatible-mode/v1"
    )

    with pytest.raises(QwenConfigurationError) as raised:
        _execute(transport, settings=settings)

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "dashscope-secret-sentinel" not in str(raised.value)
    assert transport.requests == []


def test_decision_trail_requires_incident_id_before_network() -> None:
    transport = ScriptedTransport([])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as http_client:
            client = QwenClient(
                _settings(),
                trail=DecisionTrailStore(),
                http_client=http_client,
            )
            await client.chat([{"role": "user", "content": "triage"}])

    with pytest.raises(ValueError, match="incident_id"):
        asyncio.run(run())

    assert transport.requests == []


def test_decision_trail_requires_trace_id_before_network() -> None:
    transport = ScriptedTransport([])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as http_client:
            client = QwenClient(
                _settings(),
                trail=DecisionTrailStore(),
                http_client=http_client,
            )
            await client.chat(
                [{"role": "user", "content": "triage"}],
                incident_id="inc-1",
            )

    with pytest.raises(ValueError, match="trace_id"):
        asyncio.run(run())

    assert transport.requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_timeout_seconds", 0.0),
        ("attempt_timeout_seconds", -1.0),
        ("attempt_timeout_seconds", float("inf")),
        ("attempt_timeout_seconds", float("nan")),
        ("call_timeout_seconds", 0.0),
        ("call_timeout_seconds", -1.0),
        ("call_timeout_seconds", float("inf")),
        ("call_timeout_seconds", float("nan")),
    ],
)
def test_deadlines_must_be_finite_and_positive(field: str, value: float) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(ValueError, match="finite positive"):
        QwenClient(
            _settings(),
            **{field: value},
        )


@pytest.mark.parametrize(
    ("field", "value", "maximum"),
    [
        ("attempt_timeout_seconds", 15.01, 15),
        ("call_timeout_seconds", 90.01, 90),
    ],
)
def test_deadlines_cannot_exceed_accepted_policy(
    field: str,
    value: float,
    maximum: int,
) -> None:
    with pytest.raises(ValueError, match=f"ADR-013 maximum of {maximum}"):
        QwenClient(_settings(), **{field: value})
