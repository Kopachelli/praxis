import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from scripts import fire_alert as fire_alert_module
from scripts.fire_alert import (
    FRESH_KEY_PREFIX,
    IDEMPOTENCY_KEY,
    RECURRENCE_KEY_PREFIX,
    SEED_ALERT,
    SeedClientError,
    idempotency_key_for_mode,
    load_signing_secret,
    normalize_base_url,
    preflight_summary,
    prepare_alert,
    responses_satisfy_contract,
    serialize_seed_alert,
    send_alert,
    sign_body,
)
from app.config import MIN_PRODUCTION_WEBHOOK_BODY_BYTES


def test_prepared_alert_signs_the_exact_raw_bytes() -> None:
    secret = "focused-test-secret"
    alert = prepare_alert("http://localhost:8000/", secret)

    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), alert.body, hashlib.sha256
    ).hexdigest()

    assert alert.url == "http://localhost:8000/webhook"
    assert alert.headers["Content-Type"] == "application/json"
    assert alert.headers["X-Idempotency-Key"] == IDEMPOTENCY_KEY
    assert alert.headers["X-Praxis-Signature"] == expected_signature
    assert json.loads(alert.body) == SEED_ALERT
    assert SEED_ALERT["source"] == "sentry"
    assert SEED_ALERT["service"] == "checkout-service"
    assert "timed out" in SEED_ALERT["message"]


def test_recurrence_uses_fresh_identity_without_changing_signed_alert_body() -> None:
    recurrence_key = f"{RECURRENCE_KEY_PREFIX}{'a' * 32}"
    original = prepare_alert("https://praxis.example", "focused-test-secret")
    recurrence = prepare_alert(
        "https://praxis.example",
        "focused-test-secret",
        idempotency_key=recurrence_key,
    )

    assert recurrence.body == original.body
    assert recurrence.headers["X-Praxis-Signature"] == original.headers[
        "X-Praxis-Signature"
    ]
    assert recurrence.headers["X-Idempotency-Key"] == recurrence_key
    assert recurrence.headers["X-Idempotency-Key"] != IDEMPOTENCY_KEY


def test_default_fresh_and_recurrence_identities_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter(
        [
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
            uuid.UUID("00000000-0000-0000-0000-000000000003"),
        ]
    )
    monkeypatch.setattr(fire_alert_module.uuid, "uuid4", lambda: next(generated))

    default_one = idempotency_key_for_mode(fresh=False, recurrence=False)
    default_two = idempotency_key_for_mode(fresh=False, recurrence=False)
    fresh_one = idempotency_key_for_mode(fresh=True, recurrence=False)
    fresh_two = idempotency_key_for_mode(fresh=True, recurrence=False)
    recurrence = idempotency_key_for_mode(fresh=False, recurrence=True)

    assert default_one == default_two == IDEMPOTENCY_KEY
    assert fresh_one == f"{FRESH_KEY_PREFIX}{'0' * 31}1"
    assert fresh_two == f"{FRESH_KEY_PREFIX}{'0' * 31}2"
    assert fresh_one != fresh_two
    assert recurrence == f"{RECURRENCE_KEY_PREFIX}{'0' * 31}3"
    assert len({default_one, fresh_one, fresh_two, recurrence}) == 4


def test_idempotency_modes_reject_ambiguous_fresh_recurrence_request() -> None:
    with pytest.raises(SeedClientError, match="mutually exclusive"):
        idempotency_key_for_mode(fresh=True, recurrence=True)


def test_deterministic_seed_matches_production_body_limit_floor() -> None:
    assert len(serialize_seed_alert()) == MIN_PRODUCTION_WEBHOOK_BODY_BYTES == 213


def test_sign_body_rejects_empty_secret() -> None:
    with pytest.raises(SeedClientError, match="must not be empty"):
        sign_body("", b"payload")


def test_load_signing_secret_from_dotenv_without_printing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "dotenv-focused-test-secret"
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"WEBHOOK_SIGNING_SECRET={secret}\n", encoding="utf-8")
    monkeypatch.delenv("WEBHOOK_SIGNING_SECRET", raising=False)

    assert load_signing_secret(dotenv_path) == secret
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_fresh_cli_output_is_allowlisted_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "fresh-rehearsal-secret-that-must-not-print"
    fresh_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    expected_key = f"{FRESH_KEY_PREFIX}{fresh_uuid.hex}"
    trace_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    observed: dict[str, str] = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        observed["idempotency_key"] = request.headers["X-Idempotency-Key"]
        observed["signature"] = request.headers["X-Praxis-Signature"]
        return httpx.Response(
            202,
            headers={"X-Trace-Id": trace_id},
            json={
                "incident_id": "inc_fresh_rehearsal",
                "state": "NEW",
                "duplicate": False,
                "trace_id": trace_id,
            },
        )

    def client_factory(*_args, **kwargs) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
            follow_redirects=kwargs["follow_redirects"],
        )

    monkeypatch.setattr(fire_alert_module, "load_signing_secret", lambda: secret)
    monkeypatch.setattr(fire_alert_module.uuid, "uuid4", lambda: fresh_uuid)
    monkeypatch.setattr(fire_alert_module.httpx, "Client", client_factory)

    exit_code = fire_alert_module.main(
        ["--fresh", "--base-url", "https://praxis.example"]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result == {
        "attempt": 1,
        "status_code": 202,
        "incident_id": "inc_fresh_rehearsal",
        "duplicate": False,
    }
    assert captured.err == ""
    assert observed["idempotency_key"] == expected_key
    assert secret not in captured.out
    assert expected_key not in captured.out
    assert observed["signature"] not in captured.out


def test_new_incident_contract_accepts_exact_created_response() -> None:
    trace_id = "12345678123456781234567812345678"
    response = httpx.Response(
        202,
        headers={"X-Trace-Id": trace_id},
        json={
            "incident_id": "inc_new",
            "state": "NEW",
            "duplicate": False,
            "trace_id": trace_id,
        },
    )

    assert responses_satisfy_contract(
        [response],
        repeat=False,
        expect_new_incident=True,
    )


@pytest.mark.parametrize("mode", ["--fresh", "--recurrence"])
def test_unique_cli_modes_return_failure_for_duplicate_response(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "unique-mode-secret-that-must-not-print"
    generated_uuid = uuid.UUID("87654321-4321-8765-4321-876543218765")
    real_client = httpx.Client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"incident_id": "inc_existing", "duplicate": True},
        )

    def client_factory(*_args, **kwargs) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
            follow_redirects=kwargs["follow_redirects"],
        )

    monkeypatch.setattr(fire_alert_module, "load_signing_secret", lambda: secret)
    monkeypatch.setattr(fire_alert_module.uuid, "uuid4", lambda: generated_uuid)
    monkeypatch.setattr(fire_alert_module.httpx, "Client", client_factory)

    exit_code = fire_alert_module.main(
        [mode, "--base-url", "https://praxis.example"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out) == {
        "attempt": 1,
        "status_code": 200,
        "incident_id": "inc_existing",
        "duplicate": True,
    }
    assert secret not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            json={"incident_id": "inc_existing", "duplicate": True},
        ),
        httpx.Response(
            202,
            json={"incident_id": "inc_wrong", "duplicate": True},
        ),
        httpx.Response(202, json={"incident_id": "inc_missing_duplicate"}),
        httpx.Response(202, json={"duplicate": False}),
        httpx.Response(
            202,
            json={"incident_id": "", "duplicate": False},
        ),
        httpx.Response(202, content=b"not-json"),
        httpx.Response(
            202,
            headers={"X-Trace-Id": "12345678123456781234567812345678"},
            json={
                "incident_id": "inc_wrong_state",
                "state": "TRIAGING",
                "duplicate": False,
                "trace_id": "12345678123456781234567812345678",
            },
        ),
        httpx.Response(
            202,
            headers={"X-Trace-Id": "not-a-uuid"},
            json={
                "incident_id": "inc_invalid_trace",
                "state": "NEW",
                "duplicate": False,
                "trace_id": "not-a-uuid",
            },
        ),
        httpx.Response(
            202,
            headers={"X-Trace-Id": "12345678123456781234567812345678"},
            json={
                "incident_id": "inc_missing_trace",
                "state": "NEW",
                "duplicate": False,
            },
        ),
        httpx.Response(
            202,
            headers={"X-Trace-Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            json={
                "incident_id": "inc_trace_mismatch",
                "state": "NEW",
                "duplicate": False,
                "trace_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ),
        httpx.Response(
            202,
            json={
                "incident_id": "inc_missing_trace_header",
                "state": "NEW",
                "duplicate": False,
                "trace_id": "12345678123456781234567812345678",
            },
        ),
    ],
)
def test_new_incident_contract_rejects_duplicate_or_malformed_response(
    response: httpx.Response,
) -> None:
    assert (
        responses_satisfy_contract(
            [response],
            repeat=False,
            expect_new_incident=True,
        )
        is False
    )


@pytest.mark.parametrize("status_code", [200, 202])
def test_default_single_send_preserves_status_only_compatibility(
    status_code: int,
) -> None:
    assert responses_satisfy_contract(
        [httpx.Response(status_code, content=b"legacy-response")],
        repeat=False,
    )


def test_new_incident_expectation_cannot_be_combined_with_repeat() -> None:
    responses = [
        httpx.Response(
            202,
            json={"incident_id": "inc_one", "duplicate": False},
        ),
        httpx.Response(
            200,
            json={"incident_id": "inc_one", "duplicate": True},
        ),
    ]

    assert (
        responses_satisfy_contract(
            responses,
            repeat=True,
            expect_new_incident=True,
        )
        is False
    )


@pytest.mark.parametrize("repeat, expected_count", [(False, 1), (True, 2)])
def test_send_alert_count_and_replay_identity(
    repeat: bool, expected_count: int
) -> None:
    seen: list[tuple[bytes, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.content,
                request.headers["X-Idempotency-Key"],
                request.headers["X-Praxis-Signature"],
            )
        )
        return httpx.Response(202, json={"incident_id": "inc_test"})

    alert = prepare_alert("https://praxis.example", "focused-test-secret")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        responses = send_alert(client, alert, repeat=repeat)

    assert len(responses) == expected_count
    assert len(seen) == expected_count
    assert all(item == seen[0] for item in seen)
    assert seen[0] == (
        alert.body,
        IDEMPOTENCY_KEY,
        alert.headers["X-Praxis-Signature"],
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://LOCALHOST:8000", "http://LOCALHOST:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.99.1.2", "http://127.99.1.2"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://praxis.example/", "https://praxis.example"),
    ],
)
def test_normalize_base_url_allows_https_or_explicit_loopback_http(
    value: str,
    expected: str,
) -> None:
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "localhost:8000",
        "ftp://praxis.example",
        "https://user:pass@praxis.example",
        "https://praxis.example/api",
        "https://praxis.example?token=secret",
        "https://praxis.example/#fragment",
        "https://praxis.example:99999",
        "http://praxis.example",
        "http://192.168.1.10:8000",
        "http://[::2]:8000",
        "http://localhost.example:8000",
        "http://127.0.0.1.example:8000",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_ambiguous_values(value: str) -> None:
    with pytest.raises(SeedClientError):
        normalize_base_url(value)


def test_remote_http_cli_fails_before_constructing_http_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("unsafe target reached HTTP client construction")

    monkeypatch.setattr(
        fire_alert_module,
        "load_signing_secret",
        lambda: "focused-test-secret",
    )
    monkeypatch.setattr(fire_alert_module.httpx, "Client", forbidden_client)

    exit_code = fire_alert_module.main(["--base-url", "http://praxis.example"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: remote base URLs must use HTTPS\n"


@pytest.mark.parametrize(
    ("mode_args", "expected_mode", "expected_count"),
    [
        ([], "default", 1),
        (["--repeat"], "repeat", 2),
        (["--fresh"], "fresh", 1),
        (["--recurrence"], "recurrence", 1),
    ],
)
def test_preflight_is_exact_secret_free_and_never_constructs_http_client(
    mode_args: list[str],
    expected_mode: str,
    expected_count: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "preflight-secret-that-must-never-be-loaded-or-printed"
    secret_loads = 0

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preflight crossed a send-only boundary")

    def load_secret() -> str:
        nonlocal secret_loads
        secret_loads += 1
        return secret

    monkeypatch.setattr(fire_alert_module, "load_signing_secret", load_secret)
    monkeypatch.setattr(fire_alert_module.httpx, "Client", forbidden)
    monkeypatch.setattr(fire_alert_module.uuid, "uuid4", forbidden)
    monkeypatch.setattr(fire_alert_module, "sign_body", forbidden)

    exit_code = fire_alert_module.main(
        [
            "--preflight",
            "--base-url",
            "https://praxis.example/",
            *mode_args,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert secret_loads == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "mode": expected_mode,
        "webhook_url": "https://praxis.example/webhook",
        "request_count": expected_count,
        "body_bytes": len(serialize_seed_alert()),
        "body_sha256": hashlib.sha256(serialize_seed_alert()).hexdigest(),
    }
    assert secret not in captured.out
    assert "signature" not in captured.out.lower()
    assert "idempotency" not in captured.out.lower()


def test_preflight_fails_closed_without_signing_secret_or_http_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_secret() -> str:
        raise SeedClientError("WEBHOOK_SIGNING_SECRET must be set")

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("preflight constructed an HTTP client")

    monkeypatch.setattr(fire_alert_module, "load_signing_secret", missing_secret)
    monkeypatch.setattr(fire_alert_module.httpx, "Client", forbidden_client)

    exit_code = fire_alert_module.main(
        ["--preflight", "--base-url", "https://praxis.example", "--fresh"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: WEBHOOK_SIGNING_SECRET must be set\n"


def test_preflight_summary_rejects_conflicting_modes() -> None:
    with pytest.raises(SeedClientError, match="mutually exclusive"):
        preflight_summary(
            "https://praxis.example",
            repeat=True,
            fresh=True,
        )


def test_preflight_summary_fails_closed_on_seed_body_size_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fire_alert_module, "serialize_seed_alert", lambda: b"{}")

    with pytest.raises(SeedClientError, match="body size"):
        preflight_summary("https://praxis.example")


@pytest.mark.parametrize(
    "responses",
    [
        [
            httpx.Response(
                202,
                json={"incident_id": "inc_one", "duplicate": False},
            ),
            httpx.Response(
                202,
                json={"incident_id": "inc_one", "duplicate": False},
            ),
        ],
        [
            httpx.Response(
                202,
                json={"incident_id": "inc_one", "duplicate": False},
            ),
            httpx.Response(
                200,
                json={"incident_id": "inc_two", "duplicate": True},
            ),
        ],
        [
            httpx.Response(
                202,
                json={"incident_id": "", "duplicate": False},
            ),
            httpx.Response(
                200,
                json={"incident_id": "", "duplicate": True},
            ),
        ],
    ],
)
def test_repeat_contract_rejects_broken_dedup(responses: list[httpx.Response]) -> None:
    assert responses_satisfy_contract(responses, repeat=True) is False


@pytest.mark.parametrize(
    "responses",
    [
        [
            httpx.Response(
                202,
                json={"incident_id": "inc_one", "duplicate": False},
            ),
            httpx.Response(
                200,
                json={"incident_id": "inc_one", "duplicate": True},
            ),
        ],
        [
            httpx.Response(
                200,
                json={"incident_id": "inc_existing", "duplicate": True},
            ),
            httpx.Response(
                200,
                json={"incident_id": "inc_existing", "duplicate": True},
            ),
        ],
    ],
)
def test_repeat_contract_accepts_clean_or_preexisting_dedup(
    responses: list[httpx.Response],
) -> None:
    assert responses_satisfy_contract(responses, repeat=True) is True
