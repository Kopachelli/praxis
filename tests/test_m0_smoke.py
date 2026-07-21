import json

import httpx

from app.config import Settings
from app.m0_smoke import _matches_sentinel, run


def _settings(*, dashscope_api_key: str = "dash-test-key") -> Settings:
    return Settings(
        app_env="prod",
        app_version="0.1.0",
        deployed_on="alibaba-fc",
        port=9000,
        provider_order=("qwencloud", "openrouter"),
        primary_model="qwen3.7-max",
        fast_model="qwen-flash",
        qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        qwencloud_models=("qwen3.7-max",),
        openrouter_models=("qwen/qwen3.7-max",),
        dashscope_api_key=dashscope_api_key,
        openrouter_api_key="",
        fc_function_name="praxis-api",
        fc_instance_id="c-test",
        fc_region="ap-southeast-1",
    )


def test_m0_smoke_refuses_missing_qwen_cloud_key(capsys) -> None:
    settings = _settings(dashscope_api_key="")

    assert run(settings) == 2
    assert capsys.readouterr().out.strip() == (
        '{"ok": false, "reason": "DASHSCOPE_API_KEY not configured"}'
    )


def test_m0_smoke_requires_exact_sentinel() -> None:
    matching = {"choices": [{"message": {"content": "PRAXIS_M0_FC_OK"}}]}
    mismatch = {"choices": [{"message": {"content": "something else"}}]}

    assert _matches_sentinel(matching) is True
    assert _matches_sentinel(mismatch) is False


def test_m0_smoke_calls_qwen_cloud_directly_and_emits_allowlisted_proof(
    monkeypatch,
    capsys,
) -> None:
    settings = _settings()
    captured: dict = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": "PRAXIS_M0_FC_OK"}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        )

    monkeypatch.setattr("app.m0_smoke.httpx.post", fake_post)

    assert run(settings) == 0
    output = capsys.readouterr().out
    proof = json.loads(output)
    assert captured["url"] == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer dash-test-key"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"]["model"] == "qwen3.7-max"
    assert captured["timeout"] == 30.0
    assert proof == {
        "ok": True,
        "marker_match": True,
        "provider": "qwencloud",
        "model": "qwen3.7-max",
        "fc_function_name": "praxis-api",
        "fc_instance_id": "c-test",
        "fc_region": "ap-southeast-1",
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 4,
            "total_tokens": 12,
        },
    }
    assert "dash-test-key" not in output


def test_m0_smoke_http_failure_does_not_emit_key_or_response_body(
    monkeypatch,
    capsys,
) -> None:
    settings = _settings(dashscope_api_key="dash-secret-sentinel")

    def fake_post(url, **kwargs):
        return httpx.Response(
            401,
            request=httpx.Request("POST", url),
            text="provider-body-secret-sentinel",
        )

    monkeypatch.setattr("app.m0_smoke.httpx.post", fake_post)

    assert run(settings) == 1
    output = capsys.readouterr().out
    proof = json.loads(output)
    assert proof == {
        "ok": False,
        "provider": "qwencloud",
        "model": "qwen3.7-max",
        "reason": "HTTPStatusError HTTP 401",
    }
    assert "dash-secret-sentinel" not in output
    assert "provider-body-secret-sentinel" not in output
