from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import httpx
import pytest

from app.config import (
    QWEN_CLOUD_BASE_URL,
    RUNTIME_OPENROUTER_MODELS,
    RUNTIME_QWENCLOUD_MODELS,
    Settings,
)
from scripts.verify_runtime_failover import (
    INVALID_QWENCLOUD_CREDENTIAL,
    _settings_with_invalid_qwencloud_credential,
    run,
)


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


def _completion() -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "model-output-secret-sentinel",
                    "reasoning_content": "model-reasoning-secret-sentinel",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"account_id": "account-secret-sentinel", "total_tokens": 5},
    }


class ScriptedHttpClient:
    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((url, kwargs))
        if not self.actions:
            raise AssertionError("unexpected provider request")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        status_code, payload = action
        return httpx.Response(status_code, json=payload)


def _success_client() -> ScriptedHttpClient:
    qwen_error = {
        "error": {
            "message": "provider-body-secret-sentinel",
            "request_id": "account-secret-sentinel",
        }
    }
    return ScriptedHttpClient(
        [
            (401, qwen_error),
            (401, qwen_error),
            (401, qwen_error),
            (200, _completion()),
        ]
    )


def test_success_uses_real_route_and_emits_only_allowlisted_proof(capsys) -> None:
    client = _success_client()

    assert run(_settings(), http_client=client) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload == {
        "fallbacks": [
            {
                "from": "qwencloud/qwen3.7-max",
                "to": "qwencloud/qwen3-max",
                "reason": "http_401",
            },
            {
                "from": "qwencloud/qwen3-max",
                "to": "qwencloud/qwen-plus",
                "reason": "http_401",
            },
            {
                "from": "qwencloud/qwen-plus",
                "to": "openrouter/qwen/qwen3.7-max",
                "reason": "http_401",
            },
        ],
        "model": "qwen/qwen3.7-max",
        "ok": True,
        "provider": "openrouter",
    }
    assert set(payload) == {"fallbacks", "model", "ok", "provider"}
    assert all(
        set(transition) == {"from", "to", "reason"}
        for transition in payload["fallbacks"]
    )

    assert [request[1]["json"]["model"] for request in client.requests] == [
        "qwen3.7-max",
        "qwen3-max",
        "qwen-plus",
        "qwen/qwen3.7-max",
    ]
    assert [request[1]["headers"]["Authorization"] for request in client.requests] == [
        f"Bearer {INVALID_QWENCLOUD_CREDENTIAL}",
        f"Bearer {INVALID_QWENCLOUD_CREDENTIAL}",
        f"Bearer {INVALID_QWENCLOUD_CREDENTIAL}",
        "Bearer openrouter-secret-sentinel",
    ]
    assert all(
        request[1]["json"]["enable_thinking"] is True
        for request in client.requests[:3]
    )
    assert client.requests[3][1]["json"]["reasoning"] == {"enabled": True}


def test_invalid_credential_is_fixed_and_only_in_memory() -> None:
    settings = _settings()
    before = {
        setting_field.name: getattr(settings, setting_field.name)
        for setting_field in fields(Settings)
    }

    probe_settings = _settings_with_invalid_qwencloud_credential(settings)

    assert (
        INVALID_QWENCLOUD_CREDENTIAL
        == "PRAXIS_PUBLIC_INVALID_QWENCLOUD_CREDENTIAL_TEST_ONLY"
    )
    assert settings.dashscope_api_key == "dashscope-secret-sentinel"
    assert probe_settings.dashscope_api_key == INVALID_QWENCLOUD_CREDENTIAL
    for setting_field in fields(Settings):
        assert getattr(settings, setting_field.name) == before[setting_field.name]
        if setting_field.name != "dashscope_api_key":
            assert (
                getattr(probe_settings, setting_field.name)
                == before[setting_field.name]
            )


@pytest.mark.parametrize(
    "updates",
    [
        {"provider_order": ("openrouter", "qwencloud")},
        {"primary_model": "qwen3-max"},
        {"fast_model": "qwen-plus"},
        {"openrouter_fast_model": "qwen/qwen-plus"},
        {"qwencloud_models": tuple(reversed(RUNTIME_QWENCLOUD_MODELS))},
        {"openrouter_models": tuple(reversed(RUNTIME_OPENROUTER_MODELS))},
        {"dashscope_api_key": ""},
        {"dashscope_api_key": INVALID_QWENCLOUD_CREDENTIAL},
        {"openrouter_api_key": ""},
    ],
)
def test_provider_model_and_credential_guards_fail_before_network(
    updates: dict[str, Any],
    capsys,
) -> None:
    client = ScriptedHttpClient([])

    assert run(_settings(**updates), http_client=client) == 2

    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason": "runtime_failover_verification_failed",
    }
    assert client.requests == []


def test_failure_envelope_never_leaks_secrets_or_exception_text(capsys) -> None:
    client = ScriptedHttpClient(
        [RuntimeError("exception-secret-sentinel with provider response body")]
    )

    assert run(_settings(), http_client=client) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "runtime_failover_verification_failed",
    }
    for forbidden in (
        "exception-secret-sentinel",
        "provider response body",
        "dashscope-secret-sentinel",
        "openrouter-secret-sentinel",
        INVALID_QWENCLOUD_CREDENTIAL,
        "model-output-secret-sentinel",
        "model-reasoning-secret-sentinel",
        "account-secret-sentinel",
    ):
        assert forbidden not in captured.out


def test_success_output_never_leaks_bodies_credentials_output_usage_or_ids(
    capsys,
) -> None:
    assert run(_settings(), http_client=_success_client()) == 0

    output = capsys.readouterr().out
    for forbidden in (
        "provider-body-secret-sentinel",
        "dashscope-secret-sentinel",
        "openrouter-secret-sentinel",
        INVALID_QWENCLOUD_CREDENTIAL,
        "model-output-secret-sentinel",
        "model-reasoning-secret-sentinel",
        "account-secret-sentinel",
        "runtime-failover-verification",
        "total_tokens",
        "usage",
    ):
        assert forbidden not in output
