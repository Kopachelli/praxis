from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.client import ChatCompletion
from app.agent.tools import build_tool_registry
from app.agent.triage import TriageAgent
from app.incidents import IncidentStore, Severity
from app.redaction import REDACTED, TRUNCATED, redact_structure, redact_text
from app.webhook import normalize_payload


DSN_PASSWORD = "dsn-password-PRAXIS"
JWT_TOKEN = "eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJl"
BEARER_TOKEN = "bearer-secret-PRAXIS"
ASSIGNMENT_SECRET = "assignment-secret-PRAXIS"
DASHSCOPE_SECRET = "dashscope-secret-PRAXIS"
ALIBABA_SECRET = "alibaba-secret-PRAXIS"
HEADER_SECRET = "header-secret-PRAXIS"
OPENROUTER_SECRET = "openrouter-secret-PRAXIS"
DATABASE_SECRET = "database-secret-PRAXIS"
AWS_TOKEN = "aws-token-PRAXIS"
ALIBABA_KEY_ID = "LTAI5tSentinel"
OPENROUTER_KEY_ID = "openrouter-id-secret-PRAXIS"


def _completion(
    content: str | None,
    *,
    model: str,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ChatCompletion:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ChatCompletion.from_response(
        "qwencloud",
        model,
        {
            "choices": [{"message": message}],
            "usage": {"total_tokens": 12},
        },
    )


class _ScriptedClient:
    def __init__(self, completions: list[ChatCompletion]) -> None:
        self._completions = list(completions)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self._completions.pop(0)


def test_text_pipeline_redacts_credentials_and_preserves_operational_context() -> None:
    value = (
        "checkout database failed "
        f"postgresql://alice:{DSN_PASSWORD}@db.internal:5432/praxis; "
        f"session {JWT_TOKEN}; Authorization: Bearer {BEARER_TOKEN}; "
        f"api_key='{ASSIGNMENT_SECRET} with spaces'; retrying safely"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in (DSN_PASSWORD, JWT_TOKEN, BEARER_TOKEN, ASSIGNMENT_SECRET):
        assert secret not in redacted
    assert "postgresql://[REDACTED]@db.internal:5432/praxis" in redacted
    assert "checkout database failed" in redacted
    assert "retrying safely" not in redacted
    assert redacted.count(REDACTED) >= 3


def test_text_pipeline_handles_escaped_dsn_and_has_a_hard_output_bound() -> None:
    value = (
        rf"prefix postgresql:\/\/alice:{DSN_PASSWORD}@db.internal/praxis "
        + ("useful-context " * 200)
    )

    redacted = redact_text(value, max_chars=120)

    assert redacted is not None
    assert DSN_PASSWORD not in redacted
    assert r"postgresql:\/\/[REDACTED]@db.internal/praxis" in redacted
    assert len(redacted) == 121
    assert redacted.endswith("…")


def test_text_pipeline_redacts_prefixed_praxis_keys_without_benign_false_positives() -> None:
    value = (
        f"DASHSCOPE_API_KEY={DASHSCOPE_SECRET}; "
        f'ALIBABA_CLOUD_ACCESS_KEY_SECRET="{ALIBABA_SECRET}"; '
        f"X-Api-Key: {HEADER_SECRET}; "
        "monkey=banana secretary=available token_count=3 "
        "api_key_status=rotating authorization_latency_ms=12"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in (DASHSCOPE_SECRET, ALIBABA_SECRET, HEADER_SECRET):
        assert secret not in redacted
    assert redacted.count(REDACTED) == 3
    assert "monkey=banana" in redacted
    assert "secretary=available" in redacted
    assert "token_count=3" in redacted
    assert "api_key_status=rotating" in redacted
    assert "authorization_latency_ms=12" in redacted


def test_text_pipeline_redacts_camel_and_pascal_sensitive_suffixes() -> None:
    value = (
        f"openRouterApiKey={OPENROUTER_SECRET}; "
        f"DatabasePassword: {DATABASE_SECRET}; "
        "tokenCount=3 apiKeyStatus=rotating passwordPolicy=current"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    assert OPENROUTER_SECRET not in redacted
    assert DATABASE_SECRET not in redacted
    assert redacted.count(REDACTED) == 2
    assert "tokenCount=3" in redacted
    assert "apiKeyStatus=rotating" in redacted
    assert "passwordPolicy=current" in redacted


def test_text_pipeline_redacts_credential_ids_without_generic_id_false_positives() -> None:
    value = (
        f"ALIBABA_CLOUD_ACCESS_KEY_ID={ALIBABA_KEY_ID}; "
        f"openRouterApiKeyId={OPENROUTER_KEY_ID}; "
        "incidentId=inc-42 request_id=req-42 apiKeyIdStatus=active"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    assert ALIBABA_KEY_ID not in redacted
    assert OPENROUTER_KEY_ID not in redacted
    assert redacted.count(REDACTED) == 2
    assert "incidentId=inc-42" in redacted
    assert "request_id=req-42" in redacted
    assert "apiKeyIdStatus=active" in redacted


def test_text_pipeline_redacts_secret_and_signing_key_aliases() -> None:
    secrets = {
        "secret_key": "django-secret-key-sentinel",
        "signing_key": "webhook-signing-key-sentinel",
        "camel_key": "cookie-signing-key-sentinel",
        "compact_key": "compact-signing-key-sentinel",
    }
    value = (
        f"SECRET_KEY={secrets['secret_key']}; "
        f"webhook-signing-key: {secrets['signing_key']}; "
        f"cookieSigningKey={secrets['camel_key']}; "
        f"signingkey={secrets['compact_key']}; "
        "secret_key_status=rotating signing_key_age_days=2"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in secrets.values():
        assert secret not in redacted
    assert redacted.count(REDACTED) == 4
    assert "secret_key_status=rotating" in redacted
    assert "signing_key_age_days=2" in redacted


def test_text_pipeline_redacts_complete_basic_digest_and_aws_authorization_payloads() -> None:
    secrets = (
        "YmFzaWMtdXNlcjpiYXNpYy1wYXNz",
        "digest-user-sentinel",
        "digest-response-sentinel",
        "AKIAIOSFODNN7EXAMPLE",
        "aws-signature-sentinel",
        "legacy-aws-signature-sentinel",
    )
    value = "\n".join(
        (
            f"Authorization: Basic {secrets[0]}; basic-tail",
            (
                'Authorization: Digest username="'
                f'{secrets[1]}", realm="praxis", response="{secrets[2]}"; '
                "digest-tail"
            ),
            (
                "Authorization: AWS4-HMAC-SHA256 "
                f"Credential={secrets[3]}/20260721/us-east-1/service/aws4_request, "
                f"SignedHeaders=host;x-amz-date, Signature={secrets[4]}; aws-tail"
            ),
            f"Authorization: AWS {secrets[3]}:{secrets[5]}; legacy-tail",
            "safe-following-line",
        )
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in secrets:
        assert secret not in redacted
    assert redacted.count("Authorization: [REDACTED]") == 4
    for same_line_tail in (
        "basic-tail",
        "digest-tail",
        "aws-tail",
        "legacy-tail",
    ):
        assert same_line_tail not in redacted
    assert "safe-following-line" in redacted


def test_text_pipeline_redacts_complete_unknown_authorization_payload() -> None:
    secrets = (
        "custom-auth-token-sentinel",
        "custom-auth-parameter-sentinel",
    )
    value = (
        "Authorization: Token "
        f'{secrets[0]}, opaque="{secrets[1]}"; safe-tail\n'
        "safe-following-line"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in secrets:
        assert secret not in redacted
    assert redacted == "Authorization: [REDACTED]\nsafe-following-line"
    assert "safe-tail" not in redacted
    assert "safe-following-line" in redacted


def test_text_pipeline_redacts_authorization_suffixes_and_folded_lines() -> None:
    secrets = (
        "bearer-first-sentinel",
        "bearer-second-sentinel",
        "quoted-first-sentinel",
        "quoted-second-sentinel",
        "folded-first-sentinel",
        "folded-second-sentinel",
    )
    value = (
        f"Authorization: Bearer {secrets[0]} {secrets[1]}\n"
        f'Authorization: "{secrets[2]}" {secrets[3]}\n'
        f"Authorization: Token {secrets[4]};\r\n\topaque={secrets[5]}\r\n"
        "safe-following-line"
    )

    redacted = redact_text(value, max_chars=2_000)

    assert redacted is not None
    for secret in secrets:
        assert secret not in redacted
    assert redacted.count("Authorization: [REDACTED]") == 3
    assert redacted.endswith("safe-following-line")


def test_text_pipeline_fails_closed_for_unterminated_sensitive_quotes() -> None:
    cases = (
        (
            '{"api_key": "json-secret-sentinel trailing-json-leak',
            ("json-secret-sentinel", "trailing-json-leak"),
            '{"api_key": "[REDACTED]"',
        ),
        (
            r'{\"api_key\": \"escaped-json-secret-sentinel trailing-escaped-leak',
            ("escaped-json-secret-sentinel", "trailing-escaped-leak"),
            r'{\"api_key\": \"[REDACTED]\"',
        ),
        (
            "DASHSCOPE_API_KEY='env-secret-sentinel trailing-env-leak",
            ("env-secret-sentinel", "trailing-env-leak"),
            "DASHSCOPE_API_KEY='[REDACTED]'",
        ),
        (
            'Authorization: "header-secret-sentinel trailing-header-leak',
            ("header-secret-sentinel", "trailing-header-leak"),
            "Authorization: [REDACTED]",
        ),
    )

    for value, secrets, expected in cases:
        redacted = redact_text(value, max_chars=2_000)

        assert redacted is not None
        assert redacted == expected
        for secret in secrets:
            assert secret not in redacted


def test_message_fallback_is_redacted_during_alert_normalization() -> None:
    normalized = normalize_payload(
        {
            "service": "checkout-service",
            "message": (
                f"Database unavailable at mysql://alice:{DSN_PASSWORD}@db.internal/praxis "
                f"Authorization: Bearer {BEARER_TOKEN}"
            ),
        }
    )

    assert DSN_PASSWORD not in normalized.title
    assert BEARER_TOKEN not in normalized.title
    assert "mysql://[REDACTED]@db.internal/praxis" in normalized.title
    assert normalized.signal == "service_unavailable"


def test_recursive_tool_evidence_redacts_sensitive_keys_and_bounds_shape() -> None:
    evidence = {
        "source": "incident_context",
        "nested": {
            "token": "nested-secret",
            "url": f"redis://worker:{DSN_PASSWORD}@cache.internal/0",
            "events": [f"event-{index}" for index in range(6)],
        },
    }

    redacted = redact_structure(evidence, max_depth=2, max_items=3)
    rendered = json.dumps(redacted)

    assert "nested-secret" not in rendered
    assert DSN_PASSWORD not in rendered
    assert redacted["nested"]["token"] == REDACTED
    assert redacted["nested"]["url"] == "redis://[REDACTED]@cache.internal/0"
    assert redacted["nested"]["events"][-1] == TRUNCATED


def test_structured_evidence_redacts_prefixed_praxis_keys_only() -> None:
    evidence = {
        "DASHSCOPE_API_KEY": DASHSCOPE_SECRET,
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": ALIBABA_SECRET,
        "X-Api-Key": HEADER_SECRET,
        "monkey": "banana",
        "secretary": "available",
        "token_count": 3,
        "api_key_status": "rotating",
    }

    redacted = redact_structure(evidence)

    assert redacted["DASHSCOPE_API_KEY"] == REDACTED
    assert redacted["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] == REDACTED
    assert redacted["X-Api-Key"] == REDACTED
    assert redacted["monkey"] == "banana"
    assert redacted["secretary"] == "available"
    assert redacted["token_count"] == 3
    assert redacted["api_key_status"] == "rotating"


def test_recursive_evidence_redacts_nested_secret_and_signing_key_aliases() -> None:
    secrets = (
        "nested-secret-key-sentinel",
        "nested-signing-key-sentinel",
        "nested-compact-key-sentinel",
        "nested-basic-authorization-sentinel",
    )
    evidence = {
        "tool": {
            "headers": {
                "SECRET_KEY": secrets[0],
                "webhookSigningKey": secrets[1],
                "Authorization": f"Basic {secrets[3]}",
            },
            "nested": [{"signingkey": secrets[2], "status": "degraded"}],
        }
    }

    redacted = redact_structure(evidence)
    rendered = json.dumps(redacted)

    for secret in secrets:
        assert secret not in rendered
    assert redacted["tool"]["headers"]["SECRET_KEY"] == REDACTED
    assert redacted["tool"]["headers"]["webhookSigningKey"] == REDACTED
    assert redacted["tool"]["headers"]["Authorization"] == REDACTED
    assert redacted["tool"]["nested"][0]["signingkey"] == REDACTED
    assert redacted["tool"]["nested"][0]["status"] == "degraded"


def test_recursive_evidence_redacts_camel_and_acronym_sensitive_suffixes() -> None:
    evidence = {
        "provider": {
            "awsAccessToken": AWS_TOKEN,
            "openRouterApiKey": OPENROUTER_SECRET,
            "databasePassword": DATABASE_SECRET,
            "tokenCount": 3,
            "apiKeyStatus": "rotating",
            "passwordPolicy": "current",
        }
    }

    redacted = redact_structure(evidence)
    provider = redacted["provider"]

    assert provider["awsAccessToken"] == REDACTED
    assert provider["openRouterApiKey"] == REDACTED
    assert provider["databasePassword"] == REDACTED
    assert provider["tokenCount"] == 3
    assert provider["apiKeyStatus"] == "rotating"
    assert provider["passwordPolicy"] == "current"


def test_recursive_evidence_redacts_credential_ids_but_preserves_generic_ids() -> None:
    evidence = {
        "credentials": {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": ALIBABA_KEY_ID,
            "openRouterApiKeyId": OPENROUTER_KEY_ID,
            "incidentId": "inc-42",
            "request_id": "req-42",
            "apiKeyIdStatus": "active",
        }
    }

    redacted = redact_structure(evidence)
    credentials = redacted["credentials"]

    assert credentials["ALIBABA_CLOUD_ACCESS_KEY_ID"] == REDACTED
    assert credentials["openRouterApiKeyId"] == REDACTED
    assert credentials["incidentId"] == "inc-42"
    assert credentials["request_id"] == "req-42"
    assert credentials["apiKeyIdStatus"] == "active"


def test_alert_and_nested_log_secrets_never_reach_qwen_or_public_trail() -> None:
    store = IncidentStore(600, id_factory=lambda: "inc-redaction")
    title = (
        "Database timeout at "
        f"postgresql://alice:{DSN_PASSWORD}@db.internal/praxis {JWT_TOKEN} "
        f"DASHSCOPE_API_KEY={DASHSCOPE_SECRET}"
    )
    raw_payload = {
        "extra": {
            "logs": [
                {
                    "level": "error",
                    "message": (
                        f"Authorization: Bearer {BEARER_TOKEN}; "
                        f"password={ASSIGNMENT_SECRET}; "
                        f'ALIBABA_CLOUD_ACCESS_KEY_SECRET="{ALIBABA_SECRET}"; '
                        f"X-Api-Key: {HEADER_SECRET}; "
                        f"redis://worker:{DSN_PASSWORD}@cache.internal/0"
                    ),
                }
            ]
        }
    }
    incident, _ = store.create_or_get(
        source="sentry",
        raw_payload=raw_payload,
        service="checkout-service",
        severity=Severity.HIGH,
        signal="database_timeout",
        title=title,
        idempotency_key="redaction-key",
    )
    tool_call = {
        "id": "call-logs",
        "type": "function",
        "function": {
            "name": "fetch_logs",
            "arguments": json.dumps({"service": "checkout-service"}),
        },
    }
    plan = json.dumps(
        {
            "steps": [
                {
                    "seq": 1,
                    "action": "Restart the checkout worker",
                    "tool": "restart_service",
                    "args": {"service": "checkout-service"},
                    "risk_level": "safe",
                    "rollback": "Restart the prior worker revision",
                }
            ]
        }
    )
    client = _ScriptedClient(
        [
            _completion("database timeout", model="qwen-flash"),
            _completion(
                None,
                model="qwen3.7-max",
                reasoning="Inspect bounded alert evidence.",
                tool_calls=[tool_call],
            ),
            _completion(plan, model="qwen3.7-max"),
        ]
    )
    agent = TriageAgent(
        store,
        client,  # type: ignore[arg-type]
        registry=build_tool_registry(),
        logger=logging.getLogger("praxis.test.redaction"),
    )

    asyncio.run(agent.run(incident.id, "trace-redaction"))

    public = store.view(incident.id, "trace-public")
    qwen_rendered = json.dumps(client.calls)
    public_rendered = json.dumps(public.model_dump(mode="json"))
    for secret in (
        DSN_PASSWORD,
        JWT_TOKEN,
        BEARER_TOKEN,
        ASSIGNMENT_SECRET,
        DASHSCOPE_SECRET,
        ALIBABA_SECRET,
        HEADER_SECRET,
    ):
        assert secret not in qwen_rendered
        assert secret not in public_rendered
    assert "db.internal/praxis" in public.title
    assert any(entry.type.value == "tool_result" for entry in public.trail)
