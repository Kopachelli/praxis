"""Disposable Function Compute remediation target [FR-8, ADR-010]."""

from __future__ import annotations

import hmac
import os
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, status

from app.config import require_nontrivial_secret

DEMO_TARGET_NAME = "praxis-demo-target"
RESTART_TOKEN_ENV = "PRAXIS_DEMO_TARGET_TOKEN"
RESTART_TOKEN_HEADER = "X-Praxis-Restart-Token"
RESTART_FLUSH_DELAY_SECONDS = 2.0


def _is_visible_ascii(value: str) -> bool:
    """Return whether a value can safely cross an HTTP field-value boundary."""

    return bool(value) and all("!" <= character <= "~" for character in value)


def _terminate_process() -> None:
    """Terminate only this disposable target process after the response is sent."""

    os._exit(0)


def _invoke_terminator(terminator: Callable[[], None]) -> None:
    """Keep injected terminator failures out of HTTP responses and tracebacks."""

    try:
        # FC's gateway still has to flush the already-sent 202 response to the
        # controller. Exiting the custom-runtime process immediately can reset
        # that transport even though the process subsequently recycles.
        time.sleep(RESTART_FLUSH_DELAY_SECONDS)
        terminator()
    except BaseException:
        # The caller verifies a real restart by polling for a new boot_id. A
        # failed terminator therefore becomes a bounded, secret-safe timeout.
        return


def _configured_token(token: str | None) -> str | None:
    candidate = os.getenv(RESTART_TOKEN_ENV) if token is None else token
    if candidate is None or candidate == "":
        if os.getenv("APP_ENV", "").strip().lower() in {"prod", "production"}:
            raise ValueError(
                f"{RESTART_TOKEN_ENV} must be configured with a non-trivial secret"
            )
        return None
    return require_nontrivial_secret(
        RESTART_TOKEN_ENV,
        candidate,
        min_length=32,
        max_length=4096,
        http_header_safe=True,
    )


def create_demo_target_app(
    *,
    token: str | None = None,
    terminator: Callable[[], None] = _terminate_process,
    boot_id: str | None = None,
) -> FastAPI:
    """Build the isolated target with injected process effects for tests."""

    if not callable(terminator):
        raise TypeError("terminator must be callable")
    configured_token = _configured_token(token)
    process_boot_id = boot_id or uuid.uuid4().hex
    if (
        not isinstance(process_boot_id, str)
        or len(process_boot_id) != 32
        or any(character not in "0123456789abcdef" for character in process_boot_id)
    ):
        raise ValueError("boot_id must be 32 lowercase hexadecimal characters")

    application = FastAPI(
        title="Praxis isolated remediation target",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/healthz", status_code=status.HTTP_200_OK)
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "target": DEMO_TARGET_NAME,
            "boot_id": process_boot_id,
        }

    @application.post("/restart", status_code=status.HTTP_202_ACCEPTED)
    async def restart(
        background_tasks: BackgroundTasks,
        supplied_token: Annotated[
            str | None,
            Header(alias=RESTART_TOKEN_HEADER),
        ] = None,
    ) -> dict[str, Any]:
        if configured_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="restart unavailable",
            )
        if (
            supplied_token is None
            or not _is_visible_ascii(supplied_token)
            or not hmac.compare_digest(supplied_token, configured_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorized",
            )

        # FastAPI/Starlette runs BackgroundTasks only after the response body
        # has been sent. The controller then proves the process changed by
        # polling for a different boot_id.
        background_tasks.add_task(_invoke_terminator, terminator)
        return {
            "status": "restart_accepted",
            "target": DEMO_TARGET_NAME,
            "boot_id": process_boot_id,
        }

    return application


app = create_demo_target_app()
