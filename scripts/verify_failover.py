"""Verify OpenRouter Qwen fallback readiness without calling the live primary."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import (  # noqa: E402
    ACCEPTED_OPENROUTER_MODELS,
    OPENROUTER_BASE_URL,
    Settings,
)

EXPECTED_ORDER = ("qwencloud", "openrouter")
EXPECTED_SENTINEL = "PRAXIS_M0_FAILOVER_OK"


class _ControlledQwenCloudFailure(RuntimeError):
    """Marker raised locally to exercise the fallback path without a primary call."""


def _inject_qwencloud_failure() -> None:
    raise _ControlledQwenCloudFailure


def _matches_sentinel(response: Any) -> bool:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return False
    return (
        len(response.choices) == 1
        and isinstance(content, str)
        and content.strip() == EXPECTED_SENTINEL
    )


def _emit(*, ok: bool, primary: str, fallback: str, model: str | None, reason: str) -> int:
    payload = {
        "fallback": {"model": model, "outcome": fallback, "provider": "openrouter"},
        "ok": ok,
        "primary": {"outcome": primary, "provider": "qwencloud"},
        "reason": reason,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if ok else 2


def run(
    settings: Settings,
    *,
    client_factory: Callable[..., Any] = OpenAI,
    failure_injector: Callable[[], None] = _inject_qwencloud_failure,
) -> int:
    """Inject a local primary failure, then prove live fallback readiness."""

    if settings.provider_order != EXPECTED_ORDER:
        return _emit(ok=False, primary="NOT_RUN", fallback="NOT_RUN", model=None, reason="INVALID_PROVIDER_ORDER")
    if not settings.openrouter_api_key:
        return _emit(ok=False, primary="NOT_RUN", fallback="NOT_RUN", model=None, reason="OPENROUTER_CREDENTIAL_MISSING")
    if not settings.openrouter_models or settings.openrouter_models[0] not in ACCEPTED_OPENROUTER_MODELS:
        return _emit(ok=False, primary="NOT_RUN", fallback="NOT_RUN", model=None, reason="INVALID_OPENROUTER_MODEL")

    model = settings.openrouter_models[0]
    try:
        failure_injector()
    except _ControlledQwenCloudFailure:
        pass
    else:
        return _emit(
            ok=False,
            primary="CONTROLLED_FAILURE_NOT_TRIGGERED",
            fallback="NOT_RUN",
            model=model,
            reason="CONTROLLED_FAILURE_NOT_TRIGGERED",
        )

    try:
        client = client_factory(
            api_key=settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=30.0,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": f"Reply with exactly {EXPECTED_SENTINEL} and nothing else."}],
            max_tokens=16,
            temperature=0,
        )
    except Exception:
        return _emit(ok=False, primary="CONTROLLED_FAILURE", fallback="FAILED", model=model, reason="OPENROUTER_REQUEST_FAILED")

    if not _matches_sentinel(response):
        return _emit(ok=False, primary="CONTROLLED_FAILURE", fallback="FAILED", model=model, reason="SENTINEL_MISMATCH")
    return _emit(
        ok=True,
        primary="CONTROLLED_FAILURE",
        fallback="WORKING",
        model=model,
        reason="FALLBACK_READINESS_VERIFIED",
    )


def main() -> int:
    try:
        settings = Settings.from_env()
    except Exception:
        return _emit(ok=False, primary="NOT_RUN", fallback="NOT_RUN", model=None, reason="CONFIGURATION_ERROR")
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
