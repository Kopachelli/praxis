"""Focused tests for the exact-target Function Compute adapter [FR-8]."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.agent.tools.fc_restart import (
    FunctionComputeRestartAdapter,
    RestartAdapterError,
    RestartTargetPolicyError,
)
from app.demo_target import DEMO_TARGET_NAME, RESTART_TOKEN_HEADER

_OLD_BOOT_ID = "a" * 32
_NEW_BOOT_ID = "b" * 32
_TOKEN = "adapter-token-secret-sentinel-123456789"
_BASE_URL = "https://praxis-demo-target.ap-southeast-1.fcapp.run/"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _health(boot_id: str, *, target: str = DEMO_TARGET_NAME) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "ok", "target": target, "boot_id": boot_id},
    )


def _accepted(boot_id: str = _OLD_BOOT_ID) -> httpx.Response:
    return httpx.Response(
        202,
        json={
            "status": "restart_accepted",
            "target": DEMO_TARGET_NAME,
            "boot_id": boot_id,
        },
    )


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> FunctionComputeRestartAdapter:
    return FunctionComputeRestartAdapter(
        base_url=_BASE_URL,
        token=_TOKEN,
        deadline_seconds=kwargs.pop("deadline_seconds", 1.0),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        request_timeout_seconds=kwargs.pop("request_timeout_seconds", 0.5),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_restart_uses_fixed_paths_and_token_only_on_authenticated_post() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [_health(_OLD_BOOT_ID), _accepted(), _health(_OLD_BOOT_ID), _health(_NEW_BOOT_ID)]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    adapter = _adapter(handler)

    result = _run(adapter(object(), DEMO_TARGET_NAME))

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/healthz"),
        ("POST", "/restart"),
        ("GET", "/healthz"),
        ("GET", "/healthz"),
    ]
    assert RESTART_TOKEN_HEADER not in requests[0].headers
    assert requests[1].headers[RESTART_TOKEN_HEADER] == _TOKEN
    assert RESTART_TOKEN_HEADER not in requests[2].headers
    assert all(
        request.url.host == "praxis-demo-target.ap-southeast-1.fcapp.run"
        for request in requests
    )
    assert result == {
        "source": "alibaba_function_compute",
        "dry_run": False,
        "target": DEMO_TARGET_NAME,
        "status": "restarted",
        "previous_boot_id": _OLD_BOOT_ID,
        "current_boot_id": _NEW_BOOT_ID,
    }
    assert _TOKEN not in repr(adapter)
    assert _TOKEN not in str(result)


def test_runtime_target_argument_cannot_override_configured_target() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _health(_OLD_BOOT_ID)

    adapter = _adapter(handler)

    with pytest.raises(RestartTargetPolicyError, match="not allowlisted"):
        _run(adapter({"service": "untrusted-plan-value"}, "other-target"))

    assert requests == []


def test_constructor_rejects_non_allowlisted_target_before_transport() -> None:
    with pytest.raises(RestartTargetPolicyError, match="not allowlisted"):
        _adapter(lambda request: _health(_OLD_BOOT_ID), target_name="other-target")


def test_observed_identity_mismatch_fails_without_reflecting_payload() -> None:
    malicious_identity = "attacker-secret-identity-sentinel"
    adapter = _adapter(
        lambda request: _health(_OLD_BOOT_ID, target=malicious_identity)
    )

    with pytest.raises(RestartTargetPolicyError) as raised:
        _run(adapter(None, DEMO_TARGET_NAME))

    rendered = str(raised.value)
    assert malicious_identity not in rendered
    assert _TOKEN not in rendered
    assert "fcapp.run" not in rendered


def test_transport_exception_is_rewritten_without_secret_or_url() -> None:
    transport_secret = "transport-exception-secret-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"{transport_secret} {_TOKEN} {request.url}",
            request=request,
        )

    adapter = _adapter(handler)

    with pytest.raises(RestartAdapterError) as raised:
        _run(adapter(None, DEMO_TARGET_NAME))

    rendered = str(raised.value)
    assert transport_secret not in rendered
    assert _TOKEN not in rendered
    assert "fcapp.run" not in rendered
    assert raised.value.__cause__ is None


def test_restart_acknowledgement_must_match_observed_process() -> None:
    responses = iter([_health(_OLD_BOOT_ID), _accepted(_NEW_BOOT_ID)])
    adapter = _adapter(lambda request: next(responses))

    with pytest.raises(RestartAdapterError, match="changed before acknowledgement"):
        _run(adapter(None, DEMO_TARGET_NAME))


def test_polling_is_bounded_when_boot_id_never_changes() -> None:
    class FakeTime:
        now = 0.0

        def clock(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.now += seconds

    fake_time = FakeTime()
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request.method == "POST":
            return _accepted()
        return _health(_OLD_BOOT_ID)

    adapter = _adapter(
        handler,
        deadline_seconds=0.03,
        poll_interval_seconds=0.01,
        request_timeout_seconds=0.02,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    with pytest.raises(RestartAdapterError, match="before deadline"):
        _run(adapter(None, DEMO_TARGET_NAME))

    assert fake_time.now == pytest.approx(0.03)
    assert 4 <= request_count <= 6


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com/",
        "http://praxis-demo-target.ap-southeast-1.fcapp.run/",
        "https://praxis-demo-target.ap-southeast-1.fcapp.run:443/",
        "https://user:password@praxis-demo-target.ap-southeast-1.fcapp.run/",
        "https://praxis-demo-target.ap-southeast-1.fcapp.run/path",
        "https://praxis-demo-target.ap-southeast-1.fcapp.run/?target=other",
        "https://praxis-demo-target.ap-southeast-1.fcapp.run/#fragment",
    ],
)
def test_base_url_is_fixed_https_origin(base_url: str) -> None:
    with pytest.raises(ValueError, match="invalid isolated target URL"):
        FunctionComputeRestartAdapter(base_url=base_url, token=_TOKEN)


def test_constructor_rejects_weak_token() -> None:
    with pytest.raises(ValueError, match="invalid isolated target token"):
        FunctionComputeRestartAdapter(base_url=_BASE_URL, token="too-short")
