"""Exact-allowlisted restart adapter for the isolated FC target [FR-8]."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.demo_target import (
    DEMO_TARGET_NAME,
    RESTART_TOKEN_HEADER,
)

_BOOT_ID = re.compile(r"^[0-9a-f]{32}$")
_FCAPP_HOST = re.compile(r"^[a-z0-9-]+\.ap-southeast-1\.fcapp\.run$")
_MAX_RESPONSE_BYTES = 4096


class RestartAdapterError(RuntimeError):
    """Secret-safe failure from the isolated restart boundary."""


class RestartTargetPolicyError(RestartAdapterError):
    """The configured or observed target is outside the exact allowlist."""


class _TransientTargetError(RestartAdapterError):
    """A target response that may be expected while its process recycles."""


class FunctionComputeRestartAdapter:
    """Restart one fixed FC target and prove its process boot identity changed."""

    __slots__ = (
        "_base_url",
        "_clock",
        "_deadline_seconds",
        "_poll_interval_seconds",
        "_request_timeout_seconds",
        "_sleep",
        "_target_name",
        "_token",
        "_transport",
    )

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        target_name: str = DEMO_TARGET_NAME,
        deadline_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
        request_timeout_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if target_name != DEMO_TARGET_NAME:
            raise RestartTargetPolicyError("restart target is not allowlisted")
        self._base_url = _validated_base_url(base_url)
        self._token = _validated_token(token)
        self._target_name = target_name
        self._deadline_seconds = _positive_bounded(
            deadline_seconds,
            name="deadline_seconds",
            maximum=60.0,
        )
        self._poll_interval_seconds = _positive_bounded(
            poll_interval_seconds,
            name="poll_interval_seconds",
            maximum=self._deadline_seconds,
        )
        self._request_timeout_seconds = _positive_bounded(
            request_timeout_seconds,
            name="request_timeout_seconds",
            maximum=60.0,
        )
        if not callable(clock) or not callable(sleep):
            raise TypeError("clock and sleep must be callable")
        self._transport = transport
        self._clock = clock
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(target_name={self._target_name!r}, "
            "credentials='[REDACTED]')"
        )

    async def __call__(
        self,
        context: Any,
        target_name: str,
        *,
        record_intent: Callable[[str], object] | None = None,
    ) -> dict[str, Any]:
        """Execute the registry's trusted target; incident args never reach URLs."""

        del context
        if target_name != self._target_name or target_name != DEMO_TARGET_NAME:
            raise RestartTargetPolicyError("restart target is not allowlisted")

        deadline_at = self._clock() + self._deadline_seconds
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=httpx.Timeout(self._request_timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                previous_boot_id = await self._health(
                    client,
                    deadline_at=deadline_at,
                    polling=False,
                )
                if record_intent is not None:
                    # ADR-028: durably record the execution intent (with the exact
                    # pre-action boot-id baseline) before the external restart POST.
                    # If this raises, no intent exists and no restart is dispatched,
                    # so the runner classifies it as not-applied (re-approvable).
                    record_intent(previous_boot_id)
                accepted_boot_id = await self._request_restart(
                    client,
                    deadline_at=deadline_at,
                )
                if accepted_boot_id != previous_boot_id:
                    raise RestartAdapterError(
                        "isolated restart target changed before acknowledgement"
                    )

                while self._remaining(deadline_at) > 0:
                    try:
                        current_boot_id = await self._health(
                            client,
                            deadline_at=deadline_at,
                            polling=True,
                        )
                    except _TransientTargetError:
                        current_boot_id = previous_boot_id
                    if current_boot_id != previous_boot_id:
                        return {
                            "source": "alibaba_function_compute",
                            "dry_run": False,
                            "target": self._target_name,
                            "status": "restarted",
                            "previous_boot_id": previous_boot_id,
                            "current_boot_id": current_boot_id,
                        }
                    await self._bounded_sleep(deadline_at)
        except RestartTargetPolicyError:
            raise
        except RestartAdapterError:
            raise
        except Exception:
            raise RestartAdapterError("isolated restart failed") from None

        raise RestartAdapterError(
            "isolated restart did not complete before deadline"
        )

    async def _health(
        self,
        client: httpx.AsyncClient,
        *,
        deadline_at: float,
        polling: bool,
    ) -> str:
        try:
            response = await self._request(
                client,
                "GET",
                "healthz",
                deadline_at=deadline_at,
            )
        except _TransientTargetError:
            if polling:
                raise
            raise RestartAdapterError("isolated restart target is unavailable") from None

        if response.status_code != status_code_ok():
            if polling:
                raise _TransientTargetError("target is recycling")
            raise RestartAdapterError("isolated restart target is unavailable")
        payload = _json_object(response)
        _validate_identity(payload, self._target_name)
        if payload.get("status") != "ok":
            raise RestartAdapterError(
                "isolated restart target returned an invalid response"
            )
        return _boot_id(payload)

    async def _request_restart(
        self,
        client: httpx.AsyncClient,
        *,
        deadline_at: float,
    ) -> str:
        try:
            response = await self._request(
                client,
                "POST",
                "restart",
                deadline_at=deadline_at,
                headers={RESTART_TOKEN_HEADER: self._token},
            )
        except _TransientTargetError:
            raise RestartAdapterError("isolated restart request failed") from None
        if response.status_code != 202:
            raise RestartAdapterError("isolated restart request was not accepted")
        payload = _json_object(response)
        _validate_identity(payload, self._target_name)
        if payload.get("status") != "restart_accepted":
            raise RestartAdapterError(
                "isolated restart target returned an invalid response"
            )
        return _boot_id(payload)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        deadline_at: float,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        remaining = self._remaining(deadline_at)
        if remaining <= 0:
            raise _TransientTargetError("target request deadline elapsed")
        phase_timeout = min(self._request_timeout_seconds, remaining)
        try:
            return await asyncio.wait_for(
                client.request(
                    method,
                    path,
                    headers=headers,
                    timeout=httpx.Timeout(phase_timeout),
                ),
                timeout=remaining,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.RequestError):
            raise _TransientTargetError("target request failed") from None

    async def _bounded_sleep(self, deadline_at: float) -> None:
        remaining = self._remaining(deadline_at)
        if remaining <= 0:
            return
        delay = min(self._poll_interval_seconds, remaining)
        try:
            await asyncio.wait_for(self._sleep(delay), timeout=remaining)
        except asyncio.TimeoutError:
            return

    def _remaining(self, deadline_at: float) -> float:
        return max(0.0, deadline_at - self._clock())


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("invalid isolated target URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid isolated target URL") from None
    if (
        parsed.scheme != "https"
        or _FCAPP_HOST.fullmatch((parsed.hostname or "").lower()) is None
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("invalid isolated target URL")
    return value.rstrip("/") + "/"


def _validated_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 4096
        or value.strip() != value
        or any(character in value for character in "\r\n")
    ):
        raise ValueError("invalid isolated target token")
    return value


def _positive_bounded(value: float, *, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0 or normalized > maximum:
        raise ValueError(f"{name} must be greater than zero and bounded")
    return normalized


def _json_object(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise RestartAdapterError(
            "isolated restart target returned an invalid response"
        )
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise RestartAdapterError(
            "isolated restart target returned an invalid response"
        ) from None
    if not isinstance(payload, dict):
        raise RestartAdapterError(
            "isolated restart target returned an invalid response"
        )
    return payload


def _validate_identity(payload: dict[str, Any], target_name: str) -> None:
    if payload.get("target") != target_name or target_name != DEMO_TARGET_NAME:
        raise RestartTargetPolicyError("restart target identity mismatch")


def _boot_id(payload: dict[str, Any]) -> str:
    boot_id = payload.get("boot_id")
    if not isinstance(boot_id, str) or _BOOT_ID.fullmatch(boot_id) is None:
        raise RestartAdapterError(
            "isolated restart target returned an invalid response"
        )
    return boot_id


def status_code_ok() -> int:
    """Keep the expected health status explicit without trusting redirects."""

    return 200
