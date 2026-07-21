"""Prove the production Qwen route reaches same-Qwen OpenRouter [FR-12].

The verifier loads the normal settings, changes only an in-memory copy of the
Qwen Cloud credential to a fixed public invalid value, and calls the real
``QwenClient`` reasoning route.  Its stdout contract is deliberately smaller
than the runtime trail: provider, model, and the exact fallback transitions on
success; one fixed envelope on every failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.client import ModelRole, QwenClient  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_OPENROUTER_FAST_MODEL,
    RUNTIME_OPENROUTER_MODELS,
    RUNTIME_QWENCLOUD_MODELS,
    Settings,
)
from app.trail import DecisionTrailStore, TrailEntryType  # noqa: E402

INVALID_QWENCLOUD_CREDENTIAL = (
    "PRAXIS_PUBLIC_INVALID_QWENCLOUD_CREDENTIAL_TEST_ONLY"
)
_EXPECTED_PROVIDER_ORDER = ("qwencloud", "openrouter")
_AUTH_FAILURE_REASONS = frozenset({"http_401", "http_403"})
_INCIDENT_ID = "runtime-failover-verification"
_TRACE_ID = "runtime-failover-verification"
_FAILURE_PAYLOAD: dict[str, Any] = {
    "ok": False,
    "reason": "runtime_failover_verification_failed",
}


class _VerificationFailed(RuntimeError):
    """Internal marker whose details never cross the JSON output boundary."""


def _validate_source_settings(settings: Settings) -> None:
    """Reject any route drift before constructing a network client."""

    expected_openrouter = tuple(
        f"qwen/{model}" for model in RUNTIME_QWENCLOUD_MODELS
    )
    valid = (
        settings.provider_order == _EXPECTED_PROVIDER_ORDER
        and settings.primary_model == RUNTIME_QWENCLOUD_MODELS[0]
        and settings.fast_model == "qwen-flash"
        and settings.openrouter_fast_model == DEFAULT_OPENROUTER_FAST_MODEL
        and settings.qwencloud_models == RUNTIME_QWENCLOUD_MODELS
        and settings.openrouter_models == RUNTIME_OPENROUTER_MODELS
        and settings.openrouter_models == expected_openrouter
        and bool(settings.dashscope_api_key)
        and settings.dashscope_api_key != INVALID_QWENCLOUD_CREDENTIAL
        and bool(settings.openrouter_api_key)
    )
    if not valid:
        raise _VerificationFailed


def _settings_with_invalid_qwencloud_credential(settings: Settings) -> Settings:
    """Return a copy that differs only at the Qwen Cloud credential field."""

    _validate_source_settings(settings)
    probe_settings = replace(
        settings,
        dashscope_api_key=INVALID_QWENCLOUD_CREDENTIAL,
    )
    for setting_field in fields(Settings):
        before = getattr(settings, setting_field.name)
        after = getattr(probe_settings, setting_field.name)
        if setting_field.name == "dashscope_api_key":
            if after != INVALID_QWENCLOUD_CREDENTIAL:
                raise _VerificationFailed
        elif after != before:
            raise _VerificationFailed
    return probe_settings


def _verified_fallbacks(trail: DecisionTrailStore) -> list[dict[str, str]]:
    """Return only exact, ordered auth-failure transitions from the trail."""

    entries = trail.list_for_incident(_INCIDENT_ID)
    qwen_routes = tuple(
        f"qwencloud/{model}" for model in RUNTIME_QWENCLOUD_MODELS
    )
    expected_pairs = tuple(
        zip(
            qwen_routes,
            (*qwen_routes[1:], f"openrouter/{RUNTIME_OPENROUTER_MODELS[0]}"),
        )
    )
    if len(entries) != len(expected_pairs) + 1:
        raise _VerificationFailed

    safe_entries: list[dict[str, str]] = []
    for index, (entry, (expected_from, expected_to)) in enumerate(
        zip(entries[:-1], expected_pairs),
        start=1,
    ):
        content = entry.content
        if (
            entry.incident_id != _INCIDENT_ID
            or entry.seq != index
            or entry.type is not TrailEntryType.FALLBACK
            or entry.model_used != RUNTIME_QWENCLOUD_MODELS[index - 1]
            or entry.tokens is not None
            or not isinstance(content, dict)
            or set(content) != {"from", "to", "reason"}
            or content.get("from") != expected_from
            or content.get("to") != expected_to
            or content.get("reason") not in _AUTH_FAILURE_REASONS
        ):
            raise _VerificationFailed
        safe_entries.append(
            {
                "from": expected_from,
                "to": expected_to,
                "reason": content["reason"],
            }
        )

    terminal = entries[-1]
    terminal_content = terminal.content
    if (
        terminal.incident_id != _INCIDENT_ID
        or terminal.seq != len(expected_pairs) + 1
        or terminal.type is not TrailEntryType.QWEN_ATTEMPT
        or terminal.model_used != RUNTIME_OPENROUTER_MODELS[0]
        or terminal.tokens is not None
        or not isinstance(terminal_content, dict)
        or set(terminal_content)
        != {"provider", "model", "outcome", "reason", "trace_id"}
        or terminal_content.get("provider") != "openrouter"
        or terminal_content.get("model") != RUNTIME_OPENROUTER_MODELS[0]
        or terminal_content.get("outcome") != "success"
        or terminal_content.get("reason") != "success"
        or terminal_content.get("trace_id") != _TRACE_ID
    ):
        raise _VerificationFailed
    return safe_entries


async def _verify(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exercise the real production reasoning route with one masked credential."""

    probe_settings = _settings_with_invalid_qwencloud_credential(settings)
    trail = DecisionTrailStore()
    quiet_logger = logging.Logger(
        "praxis.runtime_failover_verifier",
        level=logging.CRITICAL + 1,
    )
    quiet_logger.propagate = False

    client = QwenClient(
        probe_settings,
        trail=trail,
        http_client=http_client,
        logger=quiet_logger,
    )
    try:
        completion = await client.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "Reply with one short failover-readiness acknowledgement."
                    ),
                }
            ],
            role=ModelRole.PRIMARY,
            thinking=True,
            incident_id=_INCIDENT_ID,
            trace_id=_TRACE_ID,
        )
    finally:
        await client.aclose()

    if (
        completion.provider != "openrouter"
        or completion.model != RUNTIME_OPENROUTER_MODELS[0]
    ):
        raise _VerificationFailed

    return {
        "fallbacks": _verified_fallbacks(trail),
        "model": completion.model,
        "ok": True,
        "provider": completion.provider,
    }


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("ok") is True else 2


def run(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """Run once, suppress library logs, and fail through one fixed envelope."""

    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            output = asyncio.run(_verify(settings, http_client=http_client))
        except Exception:
            output = dict(_FAILURE_PAYLOAD)
    finally:
        logging.disable(previous_logging_disable)
    return _emit(output)


def main() -> int:
    try:
        settings = Settings.from_env()
    except Exception:
        return _emit(dict(_FAILURE_PAYLOAD))
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
