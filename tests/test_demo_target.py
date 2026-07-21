"""Safety contract for the disposable FC remediation target [FR-8]."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.demo_target import (
    DEMO_TARGET_NAME,
    RESTART_FLUSH_DELAY_SECONDS,
    RESTART_TOKEN_HEADER,
    _invoke_terminator,
    create_demo_target_app,
)

_BOOT_ID = "a" * 32
_TOKEN = "demo-target-token-sentinel-123456789"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_health_exposes_only_stable_process_identity() -> None:
    app = create_demo_target_app(token=_TOKEN, boot_id=_BOOT_ID)

    first = _run(_request(app, "GET", "/healthz"))
    second = _run(_request(app, "GET", "/healthz"))

    assert first.status_code == 200
    assert first.json() == {
        "status": "ok",
        "target": DEMO_TARGET_NAME,
        "boot_id": _BOOT_ID,
    }
    assert second.json() == first.json()
    assert _TOKEN not in first.text


def test_restart_requires_token_and_never_runs_terminator_when_unauthorized() -> None:
    terminations: list[str] = []
    app = create_demo_target_app(
        token=_TOKEN,
        boot_id=_BOOT_ID,
        terminator=lambda: terminations.append("called"),
    )

    response = _run(
        _request(
            app,
            "POST",
            "/restart",
            headers={RESTART_TOKEN_HEADER: "wrong-token-sentinel"},
        )
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert terminations == []
    assert _TOKEN not in response.text
    assert "wrong-token-sentinel" not in response.text


def test_restart_fails_closed_when_no_token_is_configured(monkeypatch: Any) -> None:
    monkeypatch.delenv("PRAXIS_DEMO_TARGET_TOKEN", raising=False)
    terminations: list[str] = []
    app = create_demo_target_app(
        boot_id=_BOOT_ID,
        terminator=lambda: terminations.append("called"),
    )

    response = _run(
        _request(
            app,
            "POST",
            "/restart",
            headers={RESTART_TOKEN_HEADER: _TOKEN},
        )
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "restart unavailable"}
    assert terminations == []
    assert _TOKEN not in response.text


@pytest.mark.parametrize(
    "invalid_token",
    (
        "too-short",
        " demo-target-token-sentinel-123456789",
        "demo-target-token sentinel-123456789",
        "placeholder-token-00000000000000000000",
        "abcd" * 8,
        "demo-target-token-sentinel-123456789\r\n",
        "démo-target-token-sentinel-123456789",
        "Xy9-demo-target-" + "a" * 4096,
    ),
)
def test_demo_target_rejects_weak_token_configuration_without_echoing_it(
    invalid_token: str,
) -> None:
    with pytest.raises(ValueError, match="PRAXIS_DEMO_TARGET_TOKEN") as captured:
        create_demo_target_app(token=invalid_token)

    assert invalid_token not in str(captured.value)


def test_blank_demo_target_token_cannot_authorize_restart() -> None:
    app = create_demo_target_app(token="", boot_id=_BOOT_ID)

    response = _run(
        _request(
            app,
            "POST",
            "/restart",
            headers={RESTART_TOKEN_HEADER: ""},
        )
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "restart unavailable"}


@pytest.mark.parametrize("token", (None, ""))
def test_production_target_refuses_a_missing_token(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PRAXIS_DEMO_TARGET_TOKEN", raising=False)

    with pytest.raises(ValueError, match="PRAXIS_DEMO_TARGET_TOKEN"):
        create_demo_target_app(token=token)


def test_non_ascii_request_token_fails_closed_without_a_server_error() -> None:
    app = create_demo_target_app(token=_TOKEN, boot_id=_BOOT_ID)

    response = _run(
        _request(
            app,
            "POST",
            "/restart",
            headers=[
                (
                    RESTART_TOKEN_HEADER.encode("ascii"),
                    b"\xff" * len(_TOKEN),
                )
            ],
        )
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_restart_response_body_is_sent_before_terminator_runs() -> None:
    events: list[str] = []

    def terminate() -> None:
        events.append("terminator")

    target = create_demo_target_app(
        token=_TOKEN,
        boot_id=_BOOT_ID,
        terminator=terminate,
    )

    class SendRecorder:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            async def record_send(message: dict[str, Any]) -> None:
                if (
                    message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                ):
                    events.append("response-complete")
                await send(message)

            await target(scope, receive, record_send)

    response = _run(
        _request(
            SendRecorder(),
            "POST",
            "/restart",
            headers={RESTART_TOKEN_HEADER: _TOKEN},
        )
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "restart_accepted",
        "target": DEMO_TARGET_NAME,
        "boot_id": _BOOT_ID,
    }
    assert events == ["response-complete", "terminator"]


def test_terminator_exception_text_is_not_exposed() -> None:
    secret = "terminator-exception-secret-sentinel"

    def fail() -> None:
        raise RuntimeError(secret)

    app = create_demo_target_app(
        token=_TOKEN,
        boot_id=_BOOT_ID,
        terminator=fail,
    )

    response = _run(
        _request(
            app,
            "POST",
            "/restart",
            headers={RESTART_TOKEN_HEADER: _TOKEN},
        )
    )

    assert response.status_code == 202
    assert secret not in response.text
    assert _TOKEN not in response.text


def test_terminator_waits_for_gateway_flush_before_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, float | None]] = []
    monkeypatch.setattr(
        "app.demo_target.time.sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    _invoke_terminator(lambda: events.append(("terminate", None)))

    assert events == [
        ("sleep", RESTART_FLUSH_DELAY_SECONDS),
        ("terminate", None),
    ]
