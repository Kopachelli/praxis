"""Environment-backed configuration with Qwen-only runtime guardrails."""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

QWEN_CLOUD_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_MEMORY_SIMILARITY_THRESHOLD = 0.80
QWEN_CLOUD_LEGACY_HOST = "dashscope-intl.aliyuncs.com"
QWEN_CLOUD_WORKSPACE_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,62}\.ap-southeast-1\.maas\.aliyuncs\.com$"
)
QWEN_CLOUD_API_PATH = "/compatible-mode/v1"
FCAPP_HOST = re.compile(r"^[a-z0-9-]+\.ap-southeast-1\.fcapp\.run$")
TABLESTORE_PUBLIC_HOST = re.compile(
    r"^(?P<instance>[a-z0-9][a-z0-9-]{0,62})\.ap-southeast-1\.ots\.aliyuncs\.com$"
)
DEFAULT_MAX_WEBHOOK_BODY_BYTES = 262_144
DEFAULT_MAX_APPROVAL_BODY_BYTES = 16_384
# The deterministic demo alert must remain admissible in every production config.
MIN_PRODUCTION_WEBHOOK_BODY_BYTES = 213
MIN_PRODUCTION_PROVIDER_CREDENTIAL_LENGTH = 20
MIN_PRODUCTION_WEBHOOK_SECRET_LENGTH = 32
MIN_OPERATOR_TOKEN_LENGTH = 32
MAX_OPERATOR_TOKEN_LENGTH = 4096
MIN_SECRET_UNIQUE_CHARACTERS = 8

_OBVIOUS_SECRET_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "defaultsecret",
        "defaulttoken",
        "examplecredential",
        "exampletoken",
        "placeholdercredential",
        "placeholdertoken",
        "replaceme",
        "replacewithrealcredential",
        "replacewithrealsecret",
        "replacewithrealtoken",
        "yourapikey",
        "yourcredential",
        "yoursecret",
        "yourtoken",
    }
)

_OBVIOUS_SECRET_PLACEHOLDER_PREFIXES = (
    "changeme",
    "defaultsecret",
    "defaulttoken",
    "examplecredential",
    "exampletoken",
    "placeholder",
    "replaceme",
    "replacewithreal",
    "yourapikey",
    "yourcredential",
    "yoursecret",
    "yourtoken",
)

ACCEPTED_QWENCLOUD_MODELS = (
    "qwen3.8-max-preview",
    "qwen3.7-max",
    "qwen3-max",
    "qwen-plus",
)
ACCEPTED_OPENROUTER_MODELS = (
    "qwen/qwen3.8-max-preview",
    "qwen/qwen3.7-max",
    "qwen/qwen3-max",
    "qwen/qwen-plus",
)
# The broader accepted constants retain M0 probe history. Runtime traffic uses
# only the exact post-M0 verified chains below; the preview failed both probes.
RUNTIME_QWENCLOUD_MODELS = (
    "qwen3.7-max",
    "qwen3-max",
    "qwen-plus",
)
RUNTIME_OPENROUTER_MODELS = (
    "qwen/qwen3.7-max",
    "qwen/qwen3-max",
    "qwen/qwen-plus",
)
DEFAULT_QWENCLOUD_MODELS = RUNTIME_QWENCLOUD_MODELS
DEFAULT_OPENROUTER_MODELS = RUNTIME_OPENROUTER_MODELS
DEFAULT_OPENROUTER_FAST_MODEL = "qwen/qwen3.6-flash"


def load_dotenv(path: Path | None = None) -> None:
    """Load a small, dependency-free subset of dotenv without overriding env vars."""

    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier():
            continue

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _require_qwen_models(provider: str, models: tuple[str, ...]) -> None:
    if not models:
        raise ValueError(f"{provider} must have at least one configured model")

    prefix = "qwen/" if provider == "openrouter" else "qwen"
    invalid = [model for model in models if not model.lower().startswith(prefix)]
    if invalid:
        raise ValueError(f"Non-Qwen chat model configured for {provider}: {invalid!r}")


def _require_runtime_model_chain(
    provider: str,
    models: tuple[str, ...],
    required: tuple[str, ...],
) -> None:
    """Require the exact post-M0 chain fixed by ADR-005 and ADR-008."""

    _require_qwen_models(provider, models)
    if len(models) != len(set(models)):
        raise ValueError(f"{provider} model chain must not contain duplicates")
    if models != required:
        raise ValueError(
            f"{provider} model chain must exactly match the post-M0 runtime chain"
        )


def require_nontrivial_secret(
    variable: str,
    value: object,
    *,
    min_length: int,
    max_length: int | None = None,
    http_header_safe: bool = False,
) -> str:
    """Return a non-trivial secret or fail without ever echoing its value."""

    invalid_type = not isinstance(value, str)
    candidate = value if isinstance(value, str) else ""
    normalized_placeholder = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
    invalid = invalid_type or (
        not candidate
        or candidate != candidate.strip()
        or any(character.isspace() for character in candidate)
        or len(candidate) < min_length
        or (max_length is not None and len(candidate) > max_length)
        or len(set(candidate)) < MIN_SECRET_UNIQUE_CHARACTERS
        or (
            http_header_safe
            and not all("!" <= character <= "~" for character in candidate)
        )
        or normalized_placeholder in _OBVIOUS_SECRET_PLACEHOLDERS
        or any(
            normalized_placeholder.startswith(prefix)
            for prefix in _OBVIOUS_SECRET_PLACEHOLDER_PREFIXES
        )
    )
    if invalid:
        raise ValueError(
            f"{variable} must be configured with a non-trivial secret"
        )
    return candidate


def validate_qwen_base_url(value: str) -> str:
    """Return the canonical URL only for an approved Alibaba Model Studio host."""

    parsed = urlsplit(value.rstrip("/"))
    host = (parsed.hostname or "").lower()
    allowed_host = host == QWEN_CLOUD_LEGACY_HOST or bool(
        QWEN_CLOUD_WORKSPACE_HOST.fullmatch(host)
    )
    try:
        has_port = parsed.port is not None
    except ValueError as exc:
        raise ValueError("QWEN_BASE_URL contains an invalid port") from exc

    if (
        parsed.scheme != "https"
        or not allowed_host
        or has_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != QWEN_CLOUD_API_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("QWEN_BASE_URL must be an approved Alibaba Model Studio endpoint")
    return f"https://{host}{QWEN_CLOUD_API_PATH}"


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    app_version: str
    deployed_on: str
    port: int
    provider_order: tuple[str, ...]
    primary_model: str
    fast_model: str
    qwen_base_url: str
    qwencloud_models: tuple[str, ...]
    openrouter_models: tuple[str, ...]
    dashscope_api_key: str = field(repr=False)
    openrouter_api_key: str = field(repr=False)
    fc_function_name: str
    fc_instance_id: str
    fc_region: str
    openrouter_fast_model: str = DEFAULT_OPENROUTER_FAST_MODEL
    demo_target_url: str = ""
    demo_target_token: str = field(default="", repr=False)
    operator_token: str = field(default="", repr=False)
    viewer_token: str = field(default="", repr=False)
    public_demo_reads: bool = False
    webhook_signing_secret: str = field(default="", repr=False)
    dedup_window_seconds: int = 600
    max_webhook_body_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES
    max_approval_body_bytes: int = DEFAULT_MAX_APPROVAL_BODY_BYTES
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    memory_backend: str = "inmem"
    memory_similarity_threshold: float = DEFAULT_MEMORY_SIMILARITY_THRESHOLD
    tablestore_endpoint: str = ""
    tablestore_instance: str = ""
    alibaba_access_key_id: str = field(default="", repr=False)
    alibaba_access_key_secret: str = field(default="", repr=False)
    alibaba_security_token: str = field(default="", repr=False)

    @property
    def resolved_primary_model(self) -> str:
        """Return the first provider-specific model identifier in the active chain."""

        first_provider = self.provider_order[0]
        if first_provider == "openrouter":
            return self.openrouter_models[0]
        return self.qwencloud_models[0]

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        app_env = os.getenv("APP_ENV", "dev").lower()
        deployed_on = os.getenv("DEPLOYED_ON", "local").lower()
        is_production = app_env in {"prod", "production"}
        expected_order = ("qwencloud", "openrouter")
        provider_order = _csv(os.getenv("PROVIDER_ORDER", ",".join(expected_order)))
        if provider_order != expected_order:
            raise ValueError(
                f"PROVIDER_ORDER must be {expected_order!r} when APP_ENV={app_env!r}"
            )
        if is_production and deployed_on != "alibaba-fc":
            raise ValueError("Production must declare DEPLOYED_ON=alibaba-fc")
        if not is_production and deployed_on != "local":
            raise ValueError("Non-production must declare DEPLOYED_ON=local")

        qwencloud_models = _csv(
            os.getenv("QWENCLOUD_MODELS", ",".join(DEFAULT_QWENCLOUD_MODELS))
        )
        openrouter_models = _csv(
            os.getenv("OPENROUTER_MODELS", ",".join(DEFAULT_OPENROUTER_MODELS))
        )
        _require_runtime_model_chain(
            "qwencloud", qwencloud_models, RUNTIME_QWENCLOUD_MODELS
        )
        _require_runtime_model_chain(
            "openrouter", openrouter_models, RUNTIME_OPENROUTER_MODELS
        )

        primary_model = os.getenv("PRIMARY_MODEL", qwencloud_models[0]).strip()
        fast_model = os.getenv("FAST_MODEL", "qwen-flash").strip()
        _require_qwen_models("qwencloud", (primary_model, fast_model))
        if primary_model != RUNTIME_QWENCLOUD_MODELS[0]:
            raise ValueError("PRIMARY_MODEL must remain qwen3.7-max after M0")
        if fast_model != "qwen-flash":
            raise ValueError("FAST_MODEL must remain qwen-flash under ADR-005")
        openrouter_fast_model = os.getenv(
            "OPENROUTER_FAST_MODEL", DEFAULT_OPENROUTER_FAST_MODEL
        ).strip()
        _require_qwen_models("openrouter", (openrouter_fast_model,))
        if openrouter_fast_model != DEFAULT_OPENROUTER_FAST_MODEL:
            raise ValueError(
                "OPENROUTER_FAST_MODEL must remain "
                f"{DEFAULT_OPENROUTER_FAST_MODEL} under ADR-009"
            )

        dedup_window_seconds = int(os.getenv("DEDUP_WINDOW_SECONDS", "600"))
        if dedup_window_seconds <= 0:
            raise ValueError("DEDUP_WINDOW_SECONDS must be greater than zero")

        max_webhook_body_bytes = int(
            os.getenv(
                "MAX_WEBHOOK_BODY_BYTES",
                str(DEFAULT_MAX_WEBHOOK_BODY_BYTES),
            )
        )
        if max_webhook_body_bytes <= 0:
            raise ValueError("MAX_WEBHOOK_BODY_BYTES must be greater than zero")
        if (
            is_production
            and max_webhook_body_bytes < MIN_PRODUCTION_WEBHOOK_BODY_BYTES
        ):
            raise ValueError(
                "MAX_WEBHOOK_BODY_BYTES must admit the deterministic demo alert "
                f"({MIN_PRODUCTION_WEBHOOK_BODY_BYTES} bytes) in production"
            )

        max_approval_body_bytes = int(
            os.getenv(
                "MAX_APPROVAL_BODY_BYTES",
                str(DEFAULT_MAX_APPROVAL_BODY_BYTES),
            )
        )
        if max_approval_body_bytes <= 0:
            raise ValueError("MAX_APPROVAL_BODY_BYTES must be greater than zero")

        embedding_model = os.getenv(
            "EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        ).strip()
        if embedding_model != DEFAULT_EMBEDDING_MODEL:
            raise ValueError(
                f"EMBEDDING_MODEL must remain {DEFAULT_EMBEDDING_MODEL} under ADR-004"
            )
        embedding_dim = int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))
        if embedding_dim != DEFAULT_EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDING_DIM must remain {DEFAULT_EMBEDDING_DIM} under ADR-004"
            )

        memory_backend = os.getenv("MEMORY_BACKEND", "inmem").strip().lower()
        if memory_backend not in {"inmem", "tablestore"}:
            raise ValueError("MEMORY_BACKEND must be 'tablestore' or 'inmem'")
        memory_similarity_threshold = float(
            os.getenv(
                "MEMORY_SIMILARITY_THRESHOLD",
                str(DEFAULT_MEMORY_SIMILARITY_THRESHOLD),
            )
        )
        if not 0.0 <= memory_similarity_threshold <= 1.0:
            raise ValueError(
                "MEMORY_SIMILARITY_THRESHOLD must be between zero and one"
            )

        tablestore_endpoint = os.getenv("TABLESTORE_ENDPOINT", "").strip().rstrip("/")
        tablestore_instance = os.getenv("TABLESTORE_INSTANCE", "").strip()
        alibaba_access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
        alibaba_access_key_secret = os.getenv(
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""
        )
        alibaba_security_token = os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN", "")
        if memory_backend == "tablestore":
            parsed_tablestore = urlsplit(tablestore_endpoint)
            try:
                tablestore_has_port = parsed_tablestore.port is not None
            except ValueError as exc:
                raise ValueError("TABLESTORE_ENDPOINT is invalid") from exc
            host = (parsed_tablestore.hostname or "").lower()
            match = TABLESTORE_PUBLIC_HOST.fullmatch(host)
            if (
                parsed_tablestore.scheme != "https"
                or match is None
                or tablestore_has_port
                or parsed_tablestore.username is not None
                or parsed_tablestore.password is not None
                or parsed_tablestore.path not in {"", "/"}
                or parsed_tablestore.query
                or parsed_tablestore.fragment
            ):
                raise ValueError(
                    "TABLESTORE_ENDPOINT must be the exact Singapore Public data endpoint"
                )
            if not tablestore_instance or match.group("instance") != tablestore_instance:
                raise ValueError(
                    "TABLESTORE_INSTANCE must match the Public endpoint instance"
                )
            if not alibaba_access_key_id or not alibaba_access_key_secret:
                raise ValueError(
                    "Tablestore credentials must be available from env or the FC execution role"
                )

        demo_target_url = os.getenv("PRAXIS_DEMO_TARGET_URL", "").strip()
        demo_target_token = os.getenv("PRAXIS_DEMO_TARGET_TOKEN", "")
        if demo_target_url and not demo_target_token:
            raise ValueError(
                "PRAXIS_DEMO_TARGET_TOKEN is required when "
                "PRAXIS_DEMO_TARGET_URL is set"
            )
        if demo_target_url:
            parsed_target = urlsplit(demo_target_url)
            try:
                target_has_port = parsed_target.port is not None
            except ValueError as exc:
                raise ValueError("PRAXIS_DEMO_TARGET_URL is invalid") from exc
            if (
                parsed_target.scheme != "https"
                or not FCAPP_HOST.fullmatch((parsed_target.hostname or "").lower())
                or target_has_port
                or parsed_target.username is not None
                or parsed_target.password is not None
                or parsed_target.path not in {"", "/"}
                or parsed_target.query
                or parsed_target.fragment
            ):
                raise ValueError(
                    "PRAXIS_DEMO_TARGET_URL must be the exact Singapore FC origin"
                )
        if demo_target_token:
            require_nontrivial_secret(
                "PRAXIS_DEMO_TARGET_TOKEN",
                demo_target_token,
                min_length=32,
                max_length=4096,
                http_header_safe=True,
            )
        if is_production:
            missing_demo_target = [
                name
                for name, value in (
                    ("PRAXIS_DEMO_TARGET_URL", demo_target_url),
                    ("PRAXIS_DEMO_TARGET_TOKEN", demo_target_token),
                )
                if not value
            ]
            if missing_demo_target:
                raise ValueError(
                    "Production requires the real isolated restart adapter: "
                    + ", ".join(missing_demo_target)
                )

        dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", "")
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        operator_token = os.getenv("PRAXIS_OPERATOR_TOKEN", "")
        webhook_signing_secret = os.getenv("WEBHOOK_SIGNING_SECRET", "")
        if is_production or operator_token:
            operator_token = require_nontrivial_secret(
                "PRAXIS_OPERATOR_TOKEN",
                operator_token,
                min_length=MIN_OPERATOR_TOKEN_LENGTH,
                max_length=MAX_OPERATOR_TOKEN_LENGTH,
                http_header_safe=True,
            )
        # ADR-029: an optional, distinct, read-only viewer credential. Validated
        # with the same strong policy and required to differ from the operator
        # token; equality fails startup without echoing either value.
        viewer_token = os.getenv("PRAXIS_VIEWER_TOKEN", "")
        if viewer_token:
            viewer_token = require_nontrivial_secret(
                "PRAXIS_VIEWER_TOKEN",
                viewer_token,
                min_length=MIN_OPERATOR_TOKEN_LENGTH,
                max_length=MAX_OPERATOR_TOKEN_LENGTH,
                http_header_safe=True,
            )
            if operator_token and hmac.compare_digest(
                viewer_token.encode("ascii"), operator_token.encode("ascii")
            ):
                raise ValueError(
                    "PRAXIS_VIEWER_TOKEN must differ from PRAXIS_OPERATOR_TOKEN"
                )
        if is_production:
            require_nontrivial_secret(
                "DASHSCOPE_API_KEY",
                dashscope_api_key,
                min_length=MIN_PRODUCTION_PROVIDER_CREDENTIAL_LENGTH,
            )
            require_nontrivial_secret(
                "OPENROUTER_API_KEY",
                openrouter_api_key,
                min_length=MIN_PRODUCTION_PROVIDER_CREDENTIAL_LENGTH,
            )
            require_nontrivial_secret(
                "WEBHOOK_SIGNING_SECRET",
                webhook_signing_secret,
                min_length=MIN_PRODUCTION_WEBHOOK_SECRET_LENGTH,
            )

        # ADR-031: an opt-in flag that opens the incident READ endpoints to
        # anonymous callers for a public demo. Defaults off, preserving ADR-025's
        # protected reads. Mutations stay operator-only regardless of this flag.
        public_demo_reads = os.getenv(
            "PRAXIS_PUBLIC_DEMO_READS", ""
        ).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            app_env=app_env,
            app_version=os.getenv("APP_VERSION", "0.1.0"),
            deployed_on=deployed_on,
            port=int(os.getenv("PORT", "8000")),
            provider_order=provider_order,
            primary_model=primary_model,
            fast_model=fast_model,
            qwen_base_url=validate_qwen_base_url(
                os.getenv("QWEN_BASE_URL", QWEN_CLOUD_BASE_URL)
            ),
            qwencloud_models=qwencloud_models,
            openrouter_models=openrouter_models,
            dashscope_api_key=dashscope_api_key,
            openrouter_api_key=openrouter_api_key,
            fc_function_name=os.getenv("FC_FUNCTION_NAME", ""),
            fc_instance_id=os.getenv("FC_INSTANCE_ID", ""),
            fc_region=os.getenv("FC_REGION", ""),
            openrouter_fast_model=openrouter_fast_model,
            demo_target_url=demo_target_url.rstrip("/"),
            demo_target_token=demo_target_token,
            operator_token=operator_token,
            viewer_token=viewer_token,
            public_demo_reads=public_demo_reads,
            webhook_signing_secret=webhook_signing_secret,
            dedup_window_seconds=dedup_window_seconds,
            max_webhook_body_bytes=max_webhook_body_bytes,
            max_approval_body_bytes=max_approval_body_bytes,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            memory_backend=memory_backend,
            memory_similarity_threshold=memory_similarity_threshold,
            tablestore_endpoint=tablestore_endpoint,
            tablestore_instance=tablestore_instance,
            alibaba_access_key_id=alibaba_access_key_id,
            alibaba_access_key_secret=alibaba_access_key_secret,
            alibaba_security_token=alibaba_security_token,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
