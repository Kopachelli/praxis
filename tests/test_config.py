import pytest

from app.config import (
    DEFAULT_MAX_WEBHOOK_BODY_BYTES,
    DEFAULT_OPENROUTER_FAST_MODEL,
    MIN_PRODUCTION_WEBHOOK_BODY_BYTES,
    RUNTIME_OPENROUTER_MODELS,
    RUNTIME_QWENCLOUD_MODELS,
    Settings,
)

_VALID_DEMO_TARGET_TOKEN = "demo-target-token-sentinel-123456789"
_VALID_OPERATOR_TOKEN = "operator-token-sentinel-0123456789abcdef"


def _set_valid_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEPLOYED_ON", "alibaba-fc")
    monkeypatch.setenv(
        "DASHSCOPE_API_KEY",
        "dashscope-production-0123456789abcdef",
    )
    monkeypatch.setenv(
        "OPENROUTER_API_KEY",
        "openrouter-production-0123456789abcdef",
    )
    monkeypatch.setenv(
        "WEBHOOK_SIGNING_SECRET",
        "webhook-production-0123456789abcdef",
    )
    monkeypatch.setenv(
        "PRAXIS_DEMO_TARGET_URL",
        "https://praxis-demo-target.ap-southeast-1.fcapp.run",
    )
    monkeypatch.setenv(
        "PRAXIS_DEMO_TARGET_TOKEN",
        "demo-target-production-0123456789abcdef",
    )
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", _VALID_OPERATOR_TOKEN)


def test_runtime_rejects_non_qwen_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWENCLOUD_MODELS", "qwen-plus,not-allowed")

    with pytest.raises(ValueError, match="Non-Qwen chat model"):
        Settings.from_env()


def test_runtime_rejects_non_alibaba_qwen_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "QWEN_BASE_URL", "https://example.invalid/compatible-mode/v1"
    )

    with pytest.raises(ValueError, match="approved Alibaba Model Studio"):
        Settings.from_env()


def test_runtime_rejects_wrong_local_provider_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.setenv("PROVIDER_ORDER", "openrouter,qwencloud")

    with pytest.raises(ValueError, match="PROVIDER_ORDER must be"):
        Settings.from_env()


def test_local_provider_order_defaults_to_qwen_cloud_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.delenv("PROVIDER_ORDER", raising=False)

    settings = Settings.from_env()

    assert settings.provider_order == ("qwencloud", "openrouter")


def test_primary_model_must_remain_post_m0_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIMARY_MODEL", "qwen3.8-max-preview")

    with pytest.raises(ValueError, match="PRIMARY_MODEL must remain qwen3.7-max"):
        Settings.from_env()


def test_failed_preview_cannot_reenter_runtime_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "QWENCLOUD_MODELS",
        "qwen3.8-max-preview,qwen3.7-max,qwen3-max,qwen-plus",
    )

    with pytest.raises(ValueError, match="exactly match the post-M0 runtime chain"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("QWENCLOUD_MODELS", "qwen3.7-max,qwen3.8-max-preview"),
        ("QWENCLOUD_MODELS", "qwen-plus"),
        ("OPENROUTER_MODELS", "qwen/qwen-plus,qwen/qwen3.7-max"),
        ("OPENROUTER_MODELS", "qwen/qwen-plus"),
    ],
)
def test_provider_chains_must_exactly_match_post_m0_route(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match="exactly match the post-M0 runtime chain"):
        Settings.from_env()


def test_provider_chain_rejects_duplicate_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENCLOUD_MODELS", "qwen3.7-max,qwen3.7-max")

    with pytest.raises(ValueError, match="must not contain duplicates"):
        Settings.from_env()


def test_runtime_defaults_are_exact_verified_post_m0_chains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENCLOUD_MODELS", ",".join(RUNTIME_QWENCLOUD_MODELS))
    monkeypatch.setenv("OPENROUTER_MODELS", ",".join(RUNTIME_OPENROUTER_MODELS))
    monkeypatch.setenv("PRIMARY_MODEL", "qwen3.7-max")

    settings = Settings.from_env()

    assert settings.qwencloud_models == RUNTIME_QWENCLOUD_MODELS
    assert settings.openrouter_models == RUNTIME_OPENROUTER_MODELS
    assert settings.primary_model == "qwen3.7-max"


def test_production_rejects_shortened_post_m0_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("QWENCLOUD_MODELS", "qwen-plus")

    with pytest.raises(ValueError, match="exactly match the post-M0 runtime chain"):
        Settings.from_env()


def test_fast_model_is_fixed_by_adr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAST_MODEL", "qwen-unapproved")

    with pytest.raises(ValueError, match="FAST_MODEL must remain qwen-flash"):
        Settings.from_env()


def test_openrouter_fast_model_defaults_to_accepted_adr_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_FAST_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.openrouter_fast_model == DEFAULT_OPENROUTER_FAST_MODEL
    assert settings.openrouter_fast_model == "qwen/qwen3.6-flash"


@pytest.mark.parametrize("model", ["not-qwen", "qwen/qwen-flash"])
def test_openrouter_fast_model_is_fixed_by_adr_009(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_FAST_MODEL", model)

    with pytest.raises(
        ValueError,
        match="Non-Qwen chat model|OPENROUTER_FAST_MODEL must remain",
    ):
        Settings.from_env()


def test_settings_repr_never_contains_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret-sentinel")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret-sentinel")
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "webhook-secret-sentinel")
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", _VALID_OPERATOR_TOKEN)
    monkeypatch.setenv(
        "PRAXIS_DEMO_TARGET_URL",
        "https://praxis-target.ap-southeast-1.fcapp.run",
    )
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", _VALID_DEMO_TARGET_TOKEN)

    rendered = repr(Settings.from_env())

    assert "dashscope-secret-sentinel" not in rendered
    assert "openrouter-secret-sentinel" not in rendered
    assert "webhook-secret-sentinel" not in rendered
    assert _VALID_DEMO_TARGET_TOKEN not in rendered
    assert _VALID_OPERATOR_TOKEN not in rendered


@pytest.mark.parametrize(
    "invalid_token",
    (
        "",
        "too-short",
        " operator-token-sentinel-0123456789abcdef",
        "operator-token sentinel-0123456789abcdef",
        "placeholder-token-00000000000000000000",
        "abcd" * 8,
        "operator-token-sentinel-0123456789abcdef\n",
        "opérator-token-sentinel-0123456789abcdef",
        "Xy9-operator-token-" + "a" * 4096,
    ),
)
def test_production_rejects_invalid_operator_token_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
    invalid_token: str,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", invalid_token)

    with pytest.raises(ValueError, match="PRAXIS_OPERATOR_TOKEN") as captured:
        Settings.from_env()

    if invalid_token:
        assert invalid_token not in str(captured.value)


def test_non_production_validates_configured_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.setenv("PRAXIS_OPERATOR_TOKEN", _VALID_OPERATOR_TOKEN)

    settings = Settings.from_env()

    assert settings.operator_token == _VALID_OPERATOR_TOKEN


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DASHSCOPE_API_KEY", ""),
        ("DASHSCOPE_API_KEY", " " * 24),
        ("DASHSCOPE_API_KEY", "short-provider-key"),
        ("DASHSCOPE_API_KEY", "a" * 40),
        ("OPENROUTER_API_KEY", ""),
        ("OPENROUTER_API_KEY", " openrouter-production-0123456789abcdef"),
        ("OPENROUTER_API_KEY", "placeholder-credential"),
        ("OPENROUTER_API_KEY", "abcd" * 10),
        ("WEBHOOK_SIGNING_SECRET", ""),
        ("WEBHOOK_SIGNING_SECRET", "webhook-secret-with-internal space-0123456789"),
        ("WEBHOOK_SIGNING_SECRET", "too-short-for-production"),
        ("WEBHOOK_SIGNING_SECRET", "1234" * 8),
    ],
)
def test_production_rejects_missing_or_trivially_weak_secrets_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=variable) as captured:
        Settings.from_env()

    if value.strip():
        assert value not in str(captured.value)


def test_production_accepts_non_trivial_provider_and_webhook_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_production_environment(monkeypatch)

    settings = Settings.from_env()

    assert settings.provider_order == ("qwencloud", "openrouter")
    assert settings.dashscope_api_key.startswith("dashscope-production-")
    assert settings.openrouter_api_key.startswith("openrouter-production-")
    assert settings.webhook_signing_secret.startswith("webhook-production-")


@pytest.mark.parametrize(
    "missing_variable",
    ("PRAXIS_DEMO_TARGET_URL", "PRAXIS_DEMO_TARGET_TOKEN"),
)
def test_production_requires_complete_real_restart_adapter_configuration(
    monkeypatch: pytest.MonkeyPatch,
    missing_variable: str,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(missing_variable, "")

    with pytest.raises(ValueError, match=missing_variable):
        Settings.from_env()


def test_non_production_allows_empty_provider_and_webhook_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEPLOYED_ON", "local")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "")

    settings = Settings.from_env()

    assert settings.dashscope_api_key == ""
    assert settings.openrouter_api_key == ""
    assert settings.webhook_signing_secret == ""


def test_demo_target_configuration_is_paired_and_fc_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PRAXIS_DEMO_TARGET_URL",
        "https://praxis-target.ap-southeast-1.fcapp.run",
    )
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", "")
    with pytest.raises(ValueError, match="required when"):
        Settings.from_env()

    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", _VALID_DEMO_TARGET_TOKEN)
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_URL", "https://example.invalid")
    with pytest.raises(ValueError, match="exact Singapore FC origin"):
        Settings.from_env()

    monkeypatch.setenv(
        "PRAXIS_DEMO_TARGET_URL",
        "https://praxis-target.ap-southeast-1.fcapp.run/",
    )
    settings = Settings.from_env()
    assert settings.demo_target_url.endswith("fcapp.run")


def test_demo_target_token_only_is_validated_but_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_URL", "")
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", _VALID_DEMO_TARGET_TOKEN)

    settings = Settings.from_env()

    assert settings.demo_target_url == ""
    assert settings.demo_target_token == _VALID_DEMO_TARGET_TOKEN


@pytest.mark.parametrize(
    "invalid_token",
    (
        "too-short",
        " demo-target-token-sentinel-123456789",
        "demo-target-token sentinel-123456789",
        "placeholder-token-00000000000000000000",
        "abcd" * 8,
        "demo-target-token-sentinel-123456789\n",
        "démo-target-token-sentinel-123456789",
        "Xy9-demo-target-" + "a" * 4096,
    ),
)
def test_demo_target_token_only_rejects_weak_values_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
    invalid_token: str,
) -> None:
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_URL", "")
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", invalid_token)

    with pytest.raises(ValueError, match="PRAXIS_DEMO_TARGET_TOKEN") as captured:
        Settings.from_env()

    assert invalid_token not in str(captured.value)


def test_empty_demo_target_token_is_inert_without_a_target_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_URL", "")
    monkeypatch.setenv("PRAXIS_DEMO_TARGET_TOKEN", "")

    settings = Settings.from_env()

    assert settings.demo_target_url == ""
    assert settings.demo_target_token == ""


def test_dedup_window_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_WINDOW_SECONDS", "0")

    with pytest.raises(ValueError, match="must be greater than zero"):
        Settings.from_env()


def test_webhook_body_limit_defaults_to_256_kib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAX_WEBHOOK_BODY_BYTES", raising=False)

    settings = Settings.from_env()

    assert settings.max_webhook_body_bytes == DEFAULT_MAX_WEBHOOK_BODY_BYTES
    assert DEFAULT_MAX_WEBHOOK_BODY_BYTES == 262_144


def test_webhook_body_limit_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_BODY_BYTES", "0")

    with pytest.raises(ValueError, match="must be greater than zero"):
        Settings.from_env()


def test_production_webhook_body_limit_admits_deterministic_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(
        "MAX_WEBHOOK_BODY_BYTES",
        str(MIN_PRODUCTION_WEBHOOK_BODY_BYTES),
    )

    settings = Settings.from_env()

    assert settings.max_webhook_body_bytes == MIN_PRODUCTION_WEBHOOK_BODY_BYTES


def test_production_webhook_body_limit_rejects_below_seed_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(
        "MAX_WEBHOOK_BODY_BYTES",
        str(MIN_PRODUCTION_WEBHOOK_BODY_BYTES - 1),
    )

    with pytest.raises(ValueError, match="deterministic demo alert"):
        Settings.from_env()
