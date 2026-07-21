"""Send the deterministic Praxis demo alert to the webhook intake endpoint."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import MIN_PRODUCTION_WEBHOOK_BODY_BYTES, load_dotenv  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8000"
BASE_URL_ENV = "PRAXIS_BASE_URL"
SIGNING_SECRET_ENV = "WEBHOOK_SIGNING_SECRET"
IDEMPOTENCY_KEY = "praxis-demo-sentry-checkout-timeout-v1"
FRESH_KEY_PREFIX = "praxis-demo-fresh-"
RECURRENCE_KEY_PREFIX = "praxis-demo-recurrence-"
SUCCESS_STATUSES = frozenset({200, 202})

SEED_ALERT = {
    "source": "sentry",
    "title": "TimeoutError in checkout-service",
    "service": "checkout-service",
    "level": "error",
    "message": "Upstream payment gateway timed out after 30s",
    "extra": {"region": "eu-central", "occurrences": 47},
}


class SeedClientError(RuntimeError):
    """Raised when the seed client cannot safely prepare or send an alert."""


@dataclass(frozen=True, slots=True)
class PreparedAlert:
    url: str
    body: bytes
    headers: dict[str, str]


def serialize_seed_alert() -> bytes:
    """Return stable JSON bytes; these exact bytes are signed and transmitted."""

    return json.dumps(
        SEED_ALERT,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    """Build the API-contract HMAC signature for an exact request body."""

    if not secret:
        raise SeedClientError("signing secret must not be empty")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def normalize_base_url(value: str) -> str:
    """Accept HTTPS origins and HTTP origins that are provably loopback-only."""

    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        parsed_port = parsed.port
    except ValueError as exc:
        raise SeedClientError("base URL has an invalid host or port") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SeedClientError("base URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise SeedClientError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SeedClientError("base URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise SeedClientError("base URL must be an origin without a path")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise SeedClientError("base URL port is out of range")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise SeedClientError("remote base URLs must use HTTPS")

    return candidate


def _is_loopback_host(hostname: str) -> bool:
    """Return whether a URL host is the explicit local-development allowlist."""

    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in ipaddress.ip_network("127.0.0.0/8")
    return address == ipaddress.IPv6Address("::1")


def load_signing_secret(dotenv_path: Path | None = None) -> str:
    """Load the signing secret from the environment or ignored repository .env."""

    load_dotenv(dotenv_path or REPOSITORY_ROOT / ".env")
    secret = os.getenv(SIGNING_SECRET_ENV, "")
    if not secret:
        raise SeedClientError(
            f"{SIGNING_SECRET_ENV} must be set in the environment or repository .env"
        )
    return secret


def idempotency_key_for_mode(*, fresh: bool, recurrence: bool) -> str:
    """Return the fixed demo key or a purpose-specific unique identity."""

    if fresh and recurrence:
        raise SeedClientError("fresh and recurrence modes are mutually exclusive")
    if fresh:
        return f"{FRESH_KEY_PREFIX}{uuid.uuid4().hex}"
    if recurrence:
        return f"{RECURRENCE_KEY_PREFIX}{uuid.uuid4().hex}"
    return IDEMPOTENCY_KEY


def prepare_alert(
    base_url: str,
    secret: str,
    *,
    idempotency_key: str = IDEMPOTENCY_KEY,
) -> PreparedAlert:
    """Create one deterministic, correctly signed webhook request."""

    if not idempotency_key.strip():
        raise SeedClientError("idempotency key must not be empty")
    body = serialize_seed_alert()
    if len(body) != MIN_PRODUCTION_WEBHOOK_BODY_BYTES:
        raise SeedClientError("deterministic alert body size is outside its contract")
    origin = normalize_base_url(base_url)
    return PreparedAlert(
        url=f"{origin}/webhook",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
            "X-Praxis-Signature": sign_body(secret, body),
        },
    )


def send_alert(
    client: httpx.Client,
    alert: PreparedAlert,
    *,
    repeat: bool = False,
) -> list[httpx.Response]:
    """POST once by default, or twice with byte-identical content and headers."""

    responses: list[httpx.Response] = []
    for _ in range(2 if repeat else 1):
        responses.append(
            client.post(alert.url, content=alert.body, headers=alert.headers)
        )
    return responses


def preflight_summary(
    base_url: str,
    *,
    repeat: bool = False,
    fresh: bool = False,
    recurrence: bool = False,
) -> dict[str, str | int]:
    """Describe a prospective send without loading secrets or creating a client."""

    selected_modes = sum((repeat, fresh, recurrence))
    if selected_modes > 1:
        raise SeedClientError("repeat, fresh, and recurrence modes are mutually exclusive")
    if repeat:
        mode = "repeat"
    elif fresh:
        mode = "fresh"
    elif recurrence:
        mode = "recurrence"
    else:
        mode = "default"

    body = serialize_seed_alert()
    if len(body) != MIN_PRODUCTION_WEBHOOK_BODY_BYTES:
        raise SeedClientError("deterministic alert body size is outside its contract")
    origin = normalize_base_url(base_url)
    return {
        "mode": mode,
        "webhook_url": f"{origin}/webhook",
        "request_count": 2 if repeat else 1,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _result_fields(response: httpx.Response) -> tuple[str | None, bool | None]:
    incident_id = None
    duplicate = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        candidate_id = payload.get("incident_id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            incident_id = candidate_id
        candidate_duplicate = payload.get("duplicate")
        if isinstance(candidate_duplicate, bool):
            duplicate = candidate_duplicate
    return incident_id, duplicate


def _satisfies_new_incident_contract(response: httpx.Response) -> bool:
    """Require the complete accepted-webhook identity and trace contract."""

    if response.status_code != 202:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False

    incident_id = payload.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id.strip():
        return False
    if payload.get("duplicate") is not False or payload.get("state") != "NEW":
        return False

    trace_id = payload.get("trace_id")
    if not isinstance(trace_id, str):
        return False
    try:
        uuid.UUID(trace_id)
    except (AttributeError, ValueError):
        return False
    return response.headers.get("X-Trace-Id") == trace_id


def responses_satisfy_contract(
    responses: Sequence[httpx.Response],
    *,
    repeat: bool,
    expect_new_incident: bool = False,
) -> bool:
    """Validate the default, new-incident, or replay response contract."""

    if repeat and expect_new_incident:
        return False

    expected_count = 2 if repeat else 1
    if len(responses) != expected_count:
        return False
    if any(response.status_code not in SUCCESS_STATUSES for response in responses):
        return False
    if not repeat:
        if expect_new_incident:
            return _satisfies_new_incident_contract(responses[0])
        return True

    first_id, first_duplicate = _result_fields(responses[0])
    second_id, second_duplicate = _result_fields(responses[1])
    if first_id is None or first_id != second_id:
        return False

    clean_replay = (
        responses[0].status_code == 202
        and first_duplicate is False
        and responses[1].status_code == 200
        and second_duplicate is True
    )
    preexisting_replay = (
        responses[0].status_code == 200
        and first_duplicate is True
        and responses[1].status_code == 200
        and second_duplicate is True
    )
    return clean_replay or preexisting_replay


def _print_result(attempt: int, response: httpx.Response) -> None:
    incident_id, duplicate = _result_fields(response)

    print(
        json.dumps(
            {
                "attempt": attempt,
                "status_code": response.status_code,
                "incident_id": incident_id,
                "duplicate": duplicate,
            },
            separators=(",", ":"),
        )
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv(BASE_URL_ENV, DEFAULT_BASE_URL),
        help=f"Praxis API origin (default: ${BASE_URL_ENV} or {DEFAULT_BASE_URL})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--repeat",
        action="store_true",
        help="send the identical body and idempotency key twice",
    )
    mode.add_argument(
        "--fresh",
        action="store_true",
        help="send one alert with a unique idempotency key for a fresh rehearsal",
    )
    mode.add_argument(
        "--recurrence",
        action="store_true",
        help="send the same alert with a fresh idempotency key for memory recall",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="print a secret-free request summary without sending an alert",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(REPOSITORY_ROOT / ".env")
    args = _parse_args(argv)
    if args.timeout <= 0:
        print("error: --timeout must be greater than zero", file=sys.stderr)
        return 2

    try:
        if args.preflight:
            load_signing_secret()
            print(
                json.dumps(
                    preflight_summary(
                        args.base_url,
                        repeat=args.repeat,
                        fresh=args.fresh,
                        recurrence=args.recurrence,
                    ),
                    separators=(",", ":"),
                )
            )
            return 0
        idempotency_key = idempotency_key_for_mode(
            fresh=args.fresh,
            recurrence=args.recurrence,
        )
        alert = prepare_alert(
            args.base_url,
            load_signing_secret(),
            idempotency_key=idempotency_key,
        )
        with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
            responses = send_alert(client, alert, repeat=args.repeat)
    except SeedClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"error: webhook request failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    for attempt, response in enumerate(responses, start=1):
        _print_result(attempt, response)

    return (
        0
        if responses_satisfy_contract(
            responses,
            repeat=args.repeat,
            expect_new_incident=args.fresh or args.recurrence,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
