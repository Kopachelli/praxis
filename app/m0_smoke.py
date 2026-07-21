"""Run the M0 Qwen Cloud completion inside a deployed FC instance."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings

EXPECTED_SENTINEL = "PRAXIS_M0_FC_OK"


def _matches_sentinel(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0]
    if not isinstance(choice, dict):
        return False
    message = choice.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, str) and content.strip() == EXPECTED_SENTINEL


def _usage(response: Any) -> dict[str, int | None]:
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "prompt_tokens": _optional_int(usage.get("prompt_tokens")),
        "completion_tokens": _optional_int(usage.get("completion_tokens")),
        "total_tokens": _optional_int(usage.get("total_tokens")),
    }


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def run(settings: Settings) -> int:
    """Call only the first verified Qwen Cloud model and print secret-safe evidence."""

    fc_identity = (
        settings.fc_function_name,
        settings.fc_instance_id,
        settings.fc_region,
    )
    if (
        settings.app_env not in {"prod", "production"}
        or settings.deployed_on != "alibaba-fc"
        or not all(fc_identity)
    ):
        print(json.dumps({"ok": False, "reason": "not a Function Compute instance"}))
        return 2
    if not settings.dashscope_api_key:
        print(json.dumps({"ok": False, "reason": "DASHSCOPE_API_KEY not configured"}))
        return 2

    model = settings.primary_model
    try:
        http_response = httpx.post(
            f"{settings.qwen_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.dashscope_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Reply with exactly {EXPECTED_SENTINEL} and nothing else."
                        ),
                    }
                ],
                "max_tokens": 16,
                "temperature": 0,
            },
            timeout=30.0,
        )
        http_response.raise_for_status()
        response = http_response.json()
    except Exception as exc:
        status_code = (
            exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError)
            else None
        )
        detail = type(exc).__name__
        if status_code is not None:
            detail += f" HTTP {status_code}"
        print(json.dumps({"ok": False, "provider": "qwencloud", "model": model, "reason": detail}))
        return 1

    if not _matches_sentinel(response):
        print(
            json.dumps(
                {
                    "ok": False,
                    "marker_match": False,
                    "provider": "qwencloud",
                    "model": model,
                    "reason": "sentinel mismatch",
                }
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "marker_match": True,
                "provider": "qwencloud",
                "model": model,
                "fc_function_name": settings.fc_function_name,
                "fc_instance_id": settings.fc_instance_id,
                "fc_region": settings.fc_region,
                "usage": _usage(response),
            },
            separators=(",", ":"),
        )
    )
    return 0


def main() -> int:
    return run(Settings.from_env())


if __name__ == "__main__":
    raise SystemExit(main())
