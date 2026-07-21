import hashlib
import hmac
import io
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.webhook as webhook_module
from app.agent.runtime import AgentTaskManager
from app.config import get_settings
from app.incidents import IncidentStore
from app.main import create_app
from app.logging_config import PraxisJsonFormatter


SECRET = "test-webhook-signing-secret"
OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
PAYLOAD = {
    "source": "sentry",
    "title": "TimeoutError in checkout-service",
    "service": "checkout-service",
    "level": "error",
    "message": "Upstream payment gateway timed out after 30s",
    "extra": {"region": "eu-central", "occurrences": 47},
}


class _InertAgent:
    async def run(self, incident_id: str, trace_id: str) -> None:
        return None


def _test_task_manager() -> AgentTaskManager:
    return AgentTaskManager(_InertAgent(), logging.getLogger("praxis.test.webhook"))


class _RecordingTaskManager:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str]] = []

    def schedule(self, incident_id: str, trace_id: str) -> bool:
        self.scheduled.append((incident_id, trace_id))
        return True

    async def shutdown(self) -> None:
        return None


def _client(
    *,
    store: IncidentStore | None = None,
    secret: str = SECRET,
) -> tuple[TestClient, IncidentStore]:
    settings = replace(
        get_settings(),
        webhook_signing_secret=secret,
        operator_token=OPERATOR_TOKEN,
        dedup_window_seconds=600,
    )
    active_store = store or IncidentStore(600)
    return (
        TestClient(
            create_app(
                settings,
                active_store,
                agent_task_manager=_test_task_manager(),
            ),
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        ),
        active_store,
    )


def _body(payload=PAYLOAD) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _signature(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(
    body: bytes,
    *,
    key: str | None = "alert-123",
    signature: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Praxis-Signature": signature or _signature(body),
    }
    if key is not None:
        headers["X-Idempotency-Key"] = key
    return headers


def test_signed_webhook_persists_normalized_incident_before_202() -> None:
    client, store = _client()
    body = _body()

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["state"] == "NEW"
    assert accepted["duplicate"] is False
    assert accepted["trace_id"] == response.headers["X-Trace-Id"]
    assert store.count() == 1

    timeline = client.get(f"/incidents/{accepted['incident_id']}")
    assert timeline.status_code == 200
    detail = timeline.json()
    assert detail["service"] == "checkout-service"
    assert detail["severity"] == "high"
    assert detail["signal"] == "upstream_timeout"
    assert detail["title"] == "TimeoutError in checkout-service"
    assert detail["state"] == "NEW"
    assert detail["trail"] == []
    assert detail["trace_id"] == timeline.headers["X-Trace-Id"]
    assert "raw_payload" not in detail
    assert "idempotency_key" not in detail


def test_signed_arbitrary_json_uses_accepted_defaults_and_stays_internal() -> None:
    client, store = _client()
    payload = ["arbitrary", {"nested": True, "secret": "internal-only"}]
    body = _body(payload)

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 202
    incident_id = response.json()["incident_id"]
    stored = store.get(incident_id)
    assert stored.source == "unknown"
    assert stored.service == "unknown-service"
    assert stored.severity.value == "medium"
    assert stored.title == "Alert for unknown-service"
    assert stored.signal == "generic_alert"
    assert stored.raw_payload == payload

    public = client.get(f"/incidents/{incident_id}")
    assert public.status_code == 200
    assert public.json()["source"] == "unknown"
    assert public.json()["service"] == "unknown-service"
    assert public.json()["severity"] == "medium"
    assert public.json()["title"] == "Alert for unknown-service"
    assert public.json()["signal"] == "generic_alert"
    assert "raw_payload" not in public.json()
    assert "internal-only" not in public.text


def test_exact_whitespace_newline_and_utf8_body_is_signed() -> None:
    client, _ = _client()
    body = (
        '{\n  "source": "custom", "service": "café", '
        '"title": "latency ⚠", "level": "warning"\n}\n'
    ).encode("utf-8")

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 202


def test_valid_escaped_utf16_surrogate_pair_is_accepted_as_one_scalar() -> None:
    client, store = _client()
    body = b'{"source":"custom","service":"checkout","title":"Rocket \\ud83d\\ude80"}'

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 202
    assert store.get(response.json()["incident_id"]).title == "Rocket \U0001f680"


def test_signature_for_reserialized_body_does_not_authorize_raw_body() -> None:
    client, store = _client()
    raw_body = b'{ "source": "sentry", "service": "checkout-service" }\n'
    canonical = b'{"service":"checkout-service","source":"sentry"}'

    response = client.post(
        "/webhook",
        content=raw_body,
        headers=_headers(raw_body, signature=_signature(canonical)),
    )

    assert response.status_code == 401
    assert store.count() == 0


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "md5=" + "0" * 32,
        "sha256=abcd",
        "sha256=" + "z" * 64,
        "sha256=" + "0" * 64,
    ],
)
def test_invalid_signature_shapes_fail_closed(signature: str | None) -> None:
    client, store = _client()
    body = _body()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Praxis-Signature"] = signature

    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]
    assert store.count() == 0


def test_compare_digest_receives_two_sha256_byte_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = _body()
    captured: list[tuple[bytes, bytes]] = []
    original = hmac.compare_digest

    def capture(expected: bytes, provided: bytes) -> bool:
        captured.append((expected, provided))
        return original(expected, provided)

    monkeypatch.setattr(webhook_module.hmac, "compare_digest", capture)

    assert webhook_module.verify_signature(raw_body, _signature(raw_body), SECRET)
    assert len(captured) == 1
    assert [len(value) for value in captured[0]] == [32, 32]


def test_missing_runtime_secret_fails_closed() -> None:
    client, store = _client(secret="")
    body = _body()

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 401
    assert store.count() == 0


def test_signature_is_checked_before_json_parsing() -> None:
    client, store = _client()
    body = b'{"source":"sentry"'

    response = client.post(
        "/webhook",
        content=body,
        headers=_headers(body, signature="sha256=" + "0" * 64),
    )

    assert response.status_code == 401
    assert store.count() == 0


def test_signed_unparseable_payload_returns_422_and_does_not_reserve_key() -> None:
    client, store = _client()
    malformed = b'{"source":"sentry"'
    key = "reusable-after-invalid"

    rejected = client.post(
        "/webhook",
        content=malformed,
        headers=_headers(malformed, key=key),
    )
    valid = _body()
    accepted = client.post(
        "/webhook",
        content=valid,
        headers=_headers(valid, key=key),
    )

    assert rejected.status_code == 422
    assert rejected.json()["trace_id"] == rejected.headers["X-Trace-Id"]
    assert accepted.status_code == 202
    assert store.count() == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e10000}',
    ],
    ids=["nan", "positive-infinity", "negative-infinity", "overflowed-exponent"],
)
def test_signed_nonfinite_numbers_are_rejected_before_persistence(
    body: bytes,
) -> None:
    client, store = _client()

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Unparseable JSON payload",
        "trace_id": response.headers["X-Trace-Id"],
    }
    assert store.count() == 0


def test_signed_pathological_integer_returns_422_without_persistence() -> None:
    client, store = _client()
    body = b'{"value":' + (b"9" * 5_000) + b"}"

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Unparseable JSON payload",
        "trace_id": response.headers["X-Trace-Id"],
    }
    assert store.count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {**PAYLOAD, "source": "sentry\ud800"},
        {**PAYLOAD, "service": "checkout\udfff"},
        {**PAYLOAD, "signal": "upstream\ud800_timeout"},
        {**PAYLOAD, "title": "Timeout\udfff"},
        {**PAYLOAD, "extra": {"events": ["valid", {"value": "\ud800"}]}},
        {**PAYLOAD, "extra": {"invalid\udfff": "object key"}},
    ],
    ids=["source", "service", "signal", "title", "nested-value", "nested-key"],
)
def test_signed_non_scalar_unicode_is_rejected_before_persistence_and_triage(
    payload: dict[str, object],
) -> None:
    settings = replace(
        get_settings(),
        webhook_signing_secret=SECRET,
        operator_token=OPERATOR_TOKEN,
        dedup_window_seconds=600,
    )
    store = IncidentStore(600)
    task_manager = _RecordingTaskManager()
    client = TestClient(
        create_app(
            settings,
            store,
            agent_task_manager=task_manager,
        ),
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    )
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    key = "reusable-after-non-scalar"

    rejected = client.post(
        "/webhook",
        content=body,
        headers=_headers(body, key=key),
    )

    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": "Unparseable JSON payload",
        "trace_id": rejected.headers["X-Trace-Id"],
    }
    assert store.count() == 0
    assert task_manager.scheduled == []
    incident_list = client.get("/incidents")
    assert incident_list.status_code == 200
    assert incident_list.json()["incidents"] == []
    missing = client.get("/incidents/inc_not_created")
    assert missing.status_code == 404
    assert missing.json()["trace_id"] == missing.headers["X-Trace-Id"]

    valid_body = _body()
    accepted = client.post(
        "/webhook",
        content=valid_body,
        headers=_headers(valid_body, key=key),
    )

    assert accepted.status_code == 202
    assert store.count() == 1
    assert len(task_manager.scheduled) == 1
    detail = client.get(f"/incidents/{accepted.json()['incident_id']}")
    assert detail.status_code == 200
    assert detail.json()["title"] == PAYLOAD["title"]


def test_malformed_payload_log_does_not_include_raw_body() -> None:
    client, _ = _client()
    body = b'{"secret_sentinel":"must-not-be-logged"'
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = CaptureHandler()
    praxis_logger = logging.getLogger("praxis")
    praxis_logger.addHandler(handler)

    try:
        response = client.post("/webhook", content=body, headers=_headers(body))
    finally:
        praxis_logger.removeHandler(handler)

    malformed_record = next(
        record
        for record in records
        if record.getMessage() == "webhook_payload_unparseable"
    )
    rendered = PraxisJsonFormatter().format(malformed_record)

    assert response.status_code == 422
    assert "must-not-be-logged" not in rendered
    assert SECRET not in rendered
    assert _signature(body) not in rendered
    assert malformed_record.raw_body_sha256 == hashlib.sha256(body).hexdigest()
    assert malformed_record.raw_body_bytes == len(body)


def test_duplicate_returns_original_incident_without_second_record() -> None:
    client, store = _client()
    first_body = _body()
    changed_body = _body({**PAYLOAD, "title": "Different alert"})
    key = "same-explicit-key"

    first = client.post(
        "/webhook", content=first_body, headers=_headers(first_body, key=key)
    )
    second = client.post(
        "/webhook", content=changed_body, headers=_headers(changed_body, key=key)
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["incident_id"] == first.json()["incident_id"]
    stored = store.get(first.json()["incident_id"])
    assert stored.title == PAYLOAD["title"]
    assert stored.raw_payload == PAYLOAD
    assert store.count() == 1


def test_missing_idempotency_key_derives_hash_from_exact_body() -> None:
    client, store = _client()
    body = _body()
    headers = _headers(body, key=None)

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    stored = store.get(first.json()["incident_id"])
    assert stored.idempotency_key == hashlib.sha256(body).hexdigest()


def test_empty_idempotency_header_is_treated_as_absent() -> None:
    client, store = _client()
    body = _body()
    headers = _headers(body, key="")

    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    stored = store.get(response.json()["incident_id"])
    assert stored.idempotency_key == hashlib.sha256(body).hexdigest()


def test_incident_list_is_newest_first_and_trace_correlated() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    current = [now]
    ids = iter(["inc_first", "inc_second"])
    store = IncidentStore(
        600,
        clock=lambda: current[0],
        id_factory=lambda: next(ids),
    )
    client, _ = _client(store=store)
    first_body = _body()
    client.post("/webhook", content=first_body, headers=_headers(first_body, key="1"))
    current[0] = now + timedelta(seconds=1)
    second_body = _body({**PAYLOAD, "title": "Second alert"})
    client.post(
        "/webhook", content=second_body, headers=_headers(second_body, key="2")
    )

    response = client.get("/incidents")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["incidents"]] == [
        "inc_second",
        "inc_first",
    ]
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_unknown_incident_has_trace_bearing_404() -> None:
    client, _ = _client()

    response = client.get("/incidents/inc_missing")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Incident not found",
        "trace_id": response.headers["X-Trace-Id"],
    }


def test_framework_404_and_405_have_trace_bearing_bodies() -> None:
    client, _ = _client()

    missing = client.get("/does-not-exist")
    wrong_method = client.put("/incidents")

    for response in (missing, wrong_method):
        assert response.status_code in {404, 405}
        assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_framework_validation_error_has_trace_bearing_body() -> None:
    settings = replace(get_settings(), webhook_signing_secret=SECRET)
    application = create_app(
        settings,
        IncidentStore(600),
        agent_task_manager=_test_task_manager(),
    )

    @application.get("/_test/positive/{value}")
    async def positive_integer(value: int):
        return {"value": value}

    client = TestClient(application)

    response = client.get("/_test/positive/not-an-integer")

    assert response.status_code == 422
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_unhandled_error_has_trace_bearing_500_and_secret_safe_log() -> None:
    provider_detail = "provider-response-secret-sentinel"
    settings = replace(get_settings(), webhook_signing_secret=SECRET)
    application = create_app(
        settings,
        IncidentStore(600),
        agent_task_manager=_test_task_manager(),
    )

    @application.get("/_test/failure")
    async def forced_failure():
        raise RuntimeError(provider_detail)

    client = TestClient(application, raise_server_exceptions=False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PraxisJsonFormatter())
    application_logger = logging.getLogger("praxis")
    application_logger.addHandler(handler)
    try:
        response = client.get("/_test/failure")
    finally:
        application_logger.removeHandler(handler)

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Internal server error",
        "trace_id": response.headers["X-Trace-Id"],
    }
    rendered = stream.getvalue()
    assert provider_detail not in rendered
    records = [json.loads(line) for line in rendered.splitlines()]
    failure = next(item for item in records if item["message"] == "request_failed")
    assert failure["error_type"] == "RuntimeError"
    assert failure["trace_id"] == response.headers["X-Trace-Id"]
    assert "exception" not in failure


def test_server_trace_id_ignores_caller_supplied_value() -> None:
    client, _ = _client()
    supplied = "attacker-controlled-trace"

    response = client.get("/healthz", headers={"X-Trace-Id": supplied})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] != supplied
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]
    assert len(response.json()["trace_id"]) == 32


def test_signed_excessive_json_depth_returns_trace_bearing_422() -> None:
    client, store = _client()
    body = ("[" * 500 + "0" + "]" * 500).encode()

    response = client.post("/webhook", content=body, headers=_headers(body))

    assert response.status_code == 422
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]
    assert store.count() == 0
