import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from app.agent.runtime import AgentTaskManager
from app.body_limit import WebhookBodyLimitMiddleware
from app.config import get_settings
from app.incidents import IncidentStore
from app.logging_config import PraxisJsonFormatter
from app.main import create_app


TRACE_ID = "trace-body-limit-test"
SIGNING_SECRET = "body-limit-signing-secret"
OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"


class _InertAgent:
    async def run(self, incident_id: str, trace_id: str) -> None:
        return None


def _test_task_manager() -> AgentTaskManager:
    return AgentTaskManager(_InertAgent(), logging.getLogger("praxis.test.body-limit"))


def _client(max_body_bytes: int) -> tuple[TestClient, IncidentStore]:
    store = IncidentStore(600)
    settings = replace(
        get_settings(),
        webhook_signing_secret=SIGNING_SECRET,
        operator_token=OPERATOR_TOKEN,
        max_webhook_body_bytes=max_body_bytes,
    )
    return (
        TestClient(
            create_app(
                settings,
                store,
                agent_task_manager=_test_task_manager(),
            ),
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        ),
        store,
    )


def _invoke_limit(
    *,
    max_body_bytes: int,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
    method: str = "POST",
    path: str = "/webhook",
) -> dict[str, Any]:
    sent: list[Message] = []
    records: list[logging.LogRecord] = []
    receive_calls = 0
    downstream_called = False

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    limit_logger = logging.Logger("praxis.body-limit-test")
    limit_logger.addHandler(CaptureHandler())

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get(
                "more_body", False
            ):
                break
        await Response(status_code=204)(scope, receive, send)

    messages: list[Message] = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {"trace_id": TRACE_ID},
    }
    middleware = WebhookBodyLimitMiddleware(
        downstream,
        max_body_bytes=max_body_bytes,
        logger=limit_logger,
    )
    asyncio.run(middleware(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    return {
        "status": start["status"],
        "body": body,
        "headers": response_headers,
        "downstream_called": downstream_called,
        "receive_calls": receive_calls,
        "records": records,
    }


def test_exact_body_limit_boundary_is_allowed_and_one_byte_over_is_rejected() -> None:
    body = b'{"source":"custom"}'
    headers = {
        "Content-Type": "application/json",
        "X-Praxis-Signature": "sha256="
        + hmac.new(
            SIGNING_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest(),
    }
    allowed_client, allowed_store = _client(len(body))
    rejected_client, rejected_store = _client(len(body) - 1)

    allowed = allowed_client.post("/webhook", content=body, headers=headers)
    rejected = rejected_client.post("/webhook", content=body, headers=headers)

    assert allowed.status_code == 202
    assert allowed_store.count() == 1
    assert rejected.status_code == 413
    assert rejected.json() == {
        "detail": "Payload too large",
        "trace_id": rejected.headers["X-Trace-Id"],
    }
    assert rejected_store.count() == 0


def test_full_app_stream_enforcement_returns_trace_bearing_413() -> None:
    body = b'{"source":"custom","padding":"oversize"}'
    signature = "sha256=" + hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    client, store = _client(len(body) - 1)

    def chunks():
        yield body[:10]
        yield body[10:]

    response = client.post(
        "/webhook",
        content=chunks(),
        headers={
            "Content-Type": "application/json",
            "X-Praxis-Signature": signature,
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "Payload too large",
        "trace_id": response.headers["X-Trace-Id"],
    }
    assert store.count() == 0


def test_approval_body_over_16_kib_is_rejected_before_json_parsing() -> None:
    client, store = _client(262_144)
    malformed_json = b"{" + (b" " * 16_384)

    response = client.post(
        "/incidents/missing/approve",
        content=malformed_json,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "Payload too large",
        "trace_id": response.headers["X-Trace-Id"],
    }
    assert store.count() == 0


def test_equal_duplicate_content_lengths_enable_early_rejection() -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=[
            (b"content-length", b"6"),
            (b"content-length", b"6"),
        ],
        chunks=[b"secret-body-that-must-not-be-read"],
    )

    assert result["status"] == 413
    assert json.loads(result["body"]) == {
        "detail": "Payload too large",
        "trace_id": TRACE_ID,
    }
    assert result["headers"]["x-trace-id"] == TRACE_ID
    assert result["downstream_called"] is False
    assert result["receive_calls"] == 0
    assert result["records"][0].limit_detection == "content_length"


def test_very_long_numeric_content_length_is_rejected_without_body_read() -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=[(b"content-length", b"9" * 5_000)],
        chunks=[b"secret-body-that-must-not-be-read"],
    )

    assert result["status"] == 413
    assert json.loads(result["body"]) == {
        "detail": "Payload too large",
        "trace_id": TRACE_ID,
    }
    assert result["headers"]["x-trace-id"] == TRACE_ID
    assert result["downstream_called"] is False
    assert result["receive_calls"] == 0
    assert result["records"][0].observed_body_bytes == 6
    assert result["records"][0].limit_detection == "content_length"


def test_leading_zero_content_lengths_compare_by_numeric_value() -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=[
            (b"content-length", (b"0" * 5_000) + b"6"),
            (b"content-length", b"6"),
        ],
        chunks=[b"secret-body-that-must-not-be-read"],
    )

    assert result["status"] == 413
    assert result["downstream_called"] is False
    assert result["receive_calls"] == 0
    assert result["records"][0].observed_body_bytes == 6
    assert result["records"][0].limit_detection == "content_length"


def test_conflicting_very_long_numeric_content_lengths_stream_fallback() -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=[
            (b"content-length", b"9" * 5_000),
            (b"content-length", b"8" * 5_000),
        ],
        chunks=[b"12345"],
    )

    assert result["status"] == 204
    assert result["downstream_called"] is True
    assert result["receive_calls"] == 1
    assert result["records"] == []


@pytest.mark.parametrize(
    "content_length_headers",
    [
        [(b"content-length", b"malformed")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"999"), (b"content-length", b"5")],
    ],
)
def test_unusable_content_length_falls_back_to_stream_without_new_400(
    content_length_headers: list[tuple[bytes, bytes]],
) -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=content_length_headers,
        chunks=[b"12", b"345"],
    )

    assert result["status"] == 204
    assert result["downstream_called"] is True
    assert result["receive_calls"] == 2
    assert result["records"] == []


@pytest.mark.parametrize(
    "content_length_headers",
    [
        [(b"content-length", b"malformed")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"1"), (b"content-length", b"999")],
    ],
)
def test_stream_limit_catches_oversize_body_when_length_is_unusable(
    content_length_headers: list[tuple[bytes, bytes]],
) -> None:
    result = _invoke_limit(
        max_body_bytes=5,
        headers=content_length_headers,
        chunks=[b"123", b"456"],
    )

    assert result["status"] == 413
    assert json.loads(result["body"]) == {
        "detail": "Payload too large",
        "trace_id": TRACE_ID,
    }
    assert result["downstream_called"] is True
    assert result["receive_calls"] == 2
    assert result["records"][0].observed_body_bytes == 6
    assert result["records"][0].limit_detection == "stream"


def test_non_webhook_and_non_post_requests_are_not_limited() -> None:
    client, _ = _client(1)
    oversized = b"not-targeted"

    missing = client.post("/not-webhook", content=oversized)
    wrong_method = client.request("GET", "/webhook", content=oversized)

    assert missing.status_code == 404
    assert wrong_method.status_code == 405


def test_rejection_log_contains_only_safe_size_and_correlation_metadata() -> None:
    secret_sentinel = "configured-secret-must-not-appear"
    body_sentinel = "body-secret-must-not-appear"
    signature_sentinel = "signature-secret-must-not-appear"
    body = body_sentinel.encode("utf-8")
    store = IncidentStore(600)
    settings = replace(
        get_settings(),
        webhook_signing_secret=secret_sentinel,
        max_webhook_body_bytes=4,
    )
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = CaptureHandler()
    praxis_logger = logging.getLogger("praxis")
    praxis_logger.addHandler(capture)
    try:
        response = TestClient(
            create_app(
                settings,
                store,
                agent_task_manager=_test_task_manager(),
            )
        ).post(
            "/webhook",
            content=body,
            headers={"X-Praxis-Signature": signature_sentinel},
        )
    finally:
        praxis_logger.removeHandler(capture)

    rejection = next(
        record
        for record in records
        if record.getMessage() == "webhook_payload_too_large"
    )
    rendered = PraxisJsonFormatter().format(rejection)
    rendered_records = "\n".join(
        PraxisJsonFormatter().format(record) for record in records
    )
    assert response.status_code == 413
    assert rejection.incident_id == "-"
    assert rejection.trace_id == response.headers["X-Trace-Id"]
    assert rejection.max_body_bytes == 4
    assert rejection.observed_body_bytes == len(body)
    assert rejection.limit_detection == "content_length"
    assert '"max_body_bytes":4' in rendered
    assert f'"observed_body_bytes":{len(body)}' in rendered
    assert '"limit_detection":"content_length"' in rendered
    assert secret_sentinel not in rendered_records
    assert body_sentinel not in rendered_records
    assert signature_sentinel not in rendered_records
    assert store.count() == 0
