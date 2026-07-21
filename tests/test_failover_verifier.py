import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from scripts.verify_failover import EXPECTED_SENTINEL, run


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "dev", "app_version": "0.1.0", "deployed_on": "local", "port": 8000,
        "provider_order": ("qwencloud", "openrouter"), "primary_model": "qwen3.7-max",
        "fast_model": "qwen-flash", "qwen_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwencloud_models": ("qwen3.7-max",), "openrouter_models": ("qwen/qwen3.7-max",),
        "dashscope_api_key": "dash-secret", "openrouter_api_key": "router-secret",
        "fc_function_name": "", "fc_instance_id": "", "fc_region": "",
    }
    values.update(overrides)
    return Settings(**values)


def _factory(response=None, error=None, calls=None):
    calls = calls if calls is not None else []

    def make(**kwargs):
        calls.append((kwargs, None))

        def create(**request):
            calls[-1] = (kwargs, request)
            if error:
                raise error
            return response

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    return make


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_success_uses_only_configured_openrouter_qwen_model(capsys) -> None:
    calls = []
    assert run(_settings(), client_factory=_factory(_response(EXPECTED_SENTINEL), calls=calls)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reason"] == "FALLBACK_READINESS_VERIFIED"
    assert payload["primary"]["outcome"] == "CONTROLLED_FAILURE"
    assert payload["fallback"] == {"model": "qwen/qwen3.7-max", "outcome": "WORKING", "provider": "openrouter"}
    assert len(calls) == 1
    assert calls[0][0]["api_key"] == "router-secret"
    assert calls[0][0]["api_key"] != "dash-secret"
    assert calls[0][0]["base_url"] == "https://openrouter.ai/api/v1"
    assert calls[0][1]["model"] == "qwen/qwen3.7-max"


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (_settings(provider_order=("openrouter", "qwencloud")), "INVALID_PROVIDER_ORDER"),
        (_settings(openrouter_api_key=""), "OPENROUTER_CREDENTIAL_MISSING"),
        (_settings(openrouter_models=("qwen/unapproved",)), "INVALID_OPENROUTER_MODEL"),
    ],
)
def test_preflight_failures_do_not_construct_client(settings, reason, capsys) -> None:
    def forbidden(**kwargs):
        raise AssertionError("client must not be constructed")

    assert run(settings, client_factory=forbidden) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == reason


def test_sentinel_mismatch_is_fixed_safe_failure(capsys) -> None:
    assert run(_settings(), client_factory=_factory(_response("response-secret"))) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == "SENTINEL_MISMATCH"
    assert "response-secret" not in output


def test_fallback_does_not_run_if_controlled_failure_is_not_triggered(capsys) -> None:
    def forbidden(**kwargs):
        raise AssertionError("client must not be constructed")

    assert run(
        _settings(),
        client_factory=forbidden,
        failure_injector=lambda: None,
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary"]["outcome"] == "CONTROLLED_FAILURE_NOT_TRIGGERED"
    assert payload["fallback"]["outcome"] == "NOT_RUN"
    assert payload["reason"] == "CONTROLLED_FAILURE_NOT_TRIGGERED"


def test_request_exception_is_fixed_safe_failure(capsys) -> None:
    assert run(_settings(), client_factory=_factory(error=RuntimeError("exception-secret"))) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == "OPENROUTER_REQUEST_FAILED"
    assert "exception-secret" not in output


def test_output_never_leaks_credentials(capsys) -> None:
    settings = _settings(dashscope_api_key="dash-leak-sentinel", openrouter_api_key="router-leak-sentinel")
    assert run(settings, client_factory=_factory(_response(EXPECTED_SENTINEL))) == 0
    output = capsys.readouterr().out
    assert "dash-leak-sentinel" not in output
    assert "router-leak-sentinel" not in output
