"""Standalone Alibaba deployment proof: Qwen Cloud + Tablestore + FC URL [FR-15].

This file intentionally has no OpenRouter path. It calls one Qwen-family model on
the Qwen Cloud endpoint, reads the accepted Tablestore table through Alibaba's
SDK, and emits only a small allowlisted proof object.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.memory import MEMORY_INDEX, MEMORY_TABLE  # noqa: E402
from app.config import FCAPP_HOST, Settings, validate_qwen_base_url  # noqa: E402

_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_SAFE_MODEL = re.compile(r"^qwen[A-Za-z0-9_.:/-]{0,74}$", re.IGNORECASE)
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_EXPECTED_SENTINEL = "PRAXIS_ALIBABA_PROOF"
_EXPECTED_INDEX_FIELDS = {
    "embedding",
    "incident_id",
    "service",
    "signal",
    "resolved_at",
    "summary",
    "resolution",
}


class ProofStageError(RuntimeError):
    """Carry only allowlisted failure metadata across the proof boundary."""

    def __init__(self, stage: str, error: Exception) -> None:
        super().__init__(stage)
        self.stage = stage
        self.error_type = type(error).__name__
        self.error_code = _error_code(error)


class QwenProofClient(Protocol):
    chat: Any

    def close(self) -> None: ...


class TablestoreProofClient(Protocol):
    def list_table(self) -> list[str]: ...

    def describe_table(self, table_name: str) -> Any: ...

    def list_search_index(self, table_name: str) -> list[tuple[str, str]]: ...

    def describe_search_index(self, table_name: str, index_name: str) -> Any: ...


class HealthProofResponse(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


def _validated_fc_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    try:
        has_port = parsed.port is not None
    except ValueError as exc:
        raise ValueError("FC_PUBLIC_URL contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not FCAPP_HOST.fullmatch(host)
        or has_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("FC_PUBLIC_URL must be the deployed Singapore fcapp.run root")
    return f"https://{host}"


def _qwen_client(settings: Settings) -> QwenProofClient:
    from openai import OpenAI

    return OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=validate_qwen_base_url(settings.qwen_base_url),
        timeout=20.0,
        max_retries=0,
    )


def _tablestore_client(settings: Settings) -> TablestoreProofClient:
    import tablestore

    return tablestore.OTSClient(
        settings.tablestore_endpoint,
        settings.alibaba_access_key_id,
        settings.alibaba_access_key_secret,
        settings.tablestore_instance,
        sts_token=settings.alibaba_security_token or None,
        region="ap-southeast-1",
        # The synchronous OTSClient accepts one numeric timeout. The async
        # client used by the app accepts a connect/read tuple instead.
        socket_timeout=10,
        enable_native=False,
    )


def _health_get(endpoint: str) -> HealthProofResponse:
    import httpx

    return httpx.get(endpoint, timeout=10.0, follow_redirects=False)


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _require_tablestore_schema(
    client: TablestoreProofClient,
    *,
    embedding_dimension: int,
) -> None:
    """Require the exact table and search-index schema used by Praxis memory."""

    if MEMORY_TABLE not in client.list_table():
        raise RuntimeError("Praxis memory table is missing")

    table = client.describe_table(MEMORY_TABLE)
    table_meta = getattr(table, "table_meta", None)
    table_options = getattr(table, "table_options", None)
    if (
        table_meta is None
        or list(getattr(table_meta, "schema_of_primary_key", ()))
        != [("id", "STRING")]
        or table_options is None
        or getattr(table_options, "time_to_live", None) != -1
        or getattr(table_options, "max_version", None) != 1
    ):
        raise RuntimeError("Praxis memory table schema does not match ADR-004")

    if (MEMORY_TABLE, MEMORY_INDEX) not in client.list_search_index(MEMORY_TABLE):
        raise RuntimeError("Praxis memory search index is missing")
    described = client.describe_search_index(MEMORY_TABLE, MEMORY_INDEX)
    index_meta = described[0] if isinstance(described, tuple) else described
    sync_stat = (
        described[1]
        if isinstance(described, tuple) and len(described) > 1
        else None
    )
    raw_fields = list(getattr(index_meta, "fields", ()))
    fields = {
        field.field_name: field
        for field in raw_fields
        if getattr(field, "field_name", None) in _EXPECTED_INDEX_FIELDS
    }
    if (
        len(raw_fields) != len(_EXPECTED_INDEX_FIELDS)
        or set(fields) != _EXPECTED_INDEX_FIELDS
    ):
        raise RuntimeError("Praxis memory search-index fields do not match ADR-004")

    vector = fields["embedding"]
    options = getattr(vector, "vector_options", None)
    if (
        _enum_name(getattr(vector, "field_type", None)) != "VECTOR"
        or getattr(vector, "index", None) is not True
        or getattr(vector, "store", None) is not False
        or options is None
        or _enum_name(getattr(options, "data_type", None)) != "VD_FLOAT_32"
        or _enum_name(getattr(options, "metric_type", None)) != "VM_COSINE"
        or getattr(options, "dimension", None) != embedding_dimension
    ):
        raise RuntimeError("Praxis memory vector field does not match ADR-004")

    for name in ("incident_id", "service", "signal", "resolved_at"):
        field = fields[name]
        if (
            _enum_name(getattr(field, "field_type", None)) != "KEYWORD"
            or getattr(field, "index", None) is not True
            or getattr(field, "store", None) is not False
        ):
            raise RuntimeError("Praxis memory keyword fields do not match ADR-004")
    if getattr(fields["service"], "enable_sort_and_agg", None) is not True:
        raise RuntimeError("Praxis memory service filter does not match ADR-004")

    for name in ("summary", "resolution"):
        field = fields[name]
        if (
            _enum_name(getattr(field, "field_type", None)) != "TEXT"
            or getattr(field, "index", None) is not True
            or getattr(field, "store", None) is not False
        ):
            raise RuntimeError("Praxis memory text fields do not match ADR-004")
    if (
        sync_stat is None
        or _enum_name(getattr(sync_stat, "sync_phase", None)) != "INCR"
    ):
        raise RuntimeError("Praxis memory search index is not synchronized")


def _header(headers: Any, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if isinstance(value, str):
            return value
    try:
        items = headers.items()
    except Exception:
        return None
    for key, value in items:
        if (
            isinstance(key, str)
            and key.lower() == name.lower()
            and isinstance(value, str)
        ):
            return value
    return None


def _require_fc_health(
    response: HealthProofResponse,
    *,
    expected_model: str,
    expected_version: str,
) -> dict[str, str]:
    """Validate only fixed Praxis deployment markers from the health contract."""

    if response.status_code != 200:
        raise RuntimeError("Praxis health endpoint did not return HTTP 200")
    try:
        payload = response.json()
    except Exception:
        raise RuntimeError("Praxis health endpoint did not return JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Praxis health endpoint returned an invalid contract")
    if (
        payload.get("ok") is not True
        or payload.get("deployed_on") != "alibaba-fc"
        or payload.get("primary_model") != expected_model
    ):
        raise RuntimeError("Praxis deployment markers did not match")

    version = payload.get("version")
    trace_id = payload.get("trace_id")
    if (
        not isinstance(version, str)
        or not _SAFE_VERSION.fullmatch(version)
        or version != expected_version
    ):
        raise RuntimeError("Praxis deployment version marker did not match")
    if (
        not isinstance(trace_id, str)
        or _header(response.headers, "X-Trace-Id") != trace_id
    ):
        raise RuntimeError("Praxis health trace marker did not match")
    try:
        uuid.UUID(trace_id)
    except (ValueError, AttributeError):
        raise RuntimeError("Praxis health trace marker was invalid") from None
    return {"version": version, "trace_id": trace_id}


def prove(
    settings: Settings,
    fc_public_url: str,
    *,
    qwen_factory: Callable[[Settings], QwenProofClient] = _qwen_client,
    tablestore_factory: Callable[
        [Settings], TablestoreProofClient
    ] = _tablestore_client,
    health_get: Callable[[str], HealthProofResponse] = _health_get,
) -> dict[str, Any]:
    """Return allowlisted proof after Qwen, schema, and FC health checks."""

    try:
        if settings.primary_model != "qwen3.7-max":
            raise ValueError("Alibaba proof must use the accepted Qwen Cloud primary")
        if settings.embedding_model != "text-embedding-v4":
            raise ValueError(
                "Alibaba proof must retain the accepted Qwen embedding model"
            )
        if settings.embedding_dim != 1024:
            raise ValueError(
                "Alibaba proof must retain the accepted embedding dimension"
            )
        if (
            not isinstance(settings.app_version, str)
            or not _SAFE_VERSION.fullmatch(settings.app_version)
        ):
            raise ValueError("Alibaba proof requires a safe expected app version")
        deployed_url = _validated_fc_url(fc_public_url)
    except Exception as exc:
        raise ProofStageError("contract", exc) from None

    try:
        qwen = qwen_factory(settings)
        try:
            completion = qwen.chat.completions.create(
                model=settings.primary_model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Reply with exactly {_EXPECTED_SENTINEL} and nothing else."
                        ),
                    }
                ],
                max_tokens=32,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            choices = getattr(completion, "choices", None)
            content = (
                getattr(getattr(choices[0], "message", None), "content", None)
                if isinstance(choices, list) and choices
                else None
            )
            if not isinstance(content, str) or content.strip() != _EXPECTED_SENTINEL:
                raise RuntimeError("Qwen Cloud proof sentinel did not match")
            returned_model = getattr(completion, "model", None)
            if not isinstance(returned_model, str) or not _SAFE_MODEL.fullmatch(
                returned_model
            ):
                raise RuntimeError("Qwen Cloud returned an invalid model identity")
        finally:
            qwen.close()
    except Exception as exc:
        raise ProofStageError("qwencloud", exc) from None

    try:
        _require_tablestore_schema(
            tablestore_factory(settings),
            embedding_dimension=settings.embedding_dim,
        )
    except Exception as exc:
        raise ProofStageError("tablestore", exc) from None

    health_endpoint = f"{deployed_url}/healthz"
    try:
        health = _require_fc_health(
            health_get(health_endpoint),
            expected_model=settings.primary_model,
            expected_version=settings.app_version,
        )
    except Exception as exc:
        raise ProofStageError("function_compute", exc) from None

    return {
        "ok": True,
        "provider": "qwencloud",
        "requested_model": settings.primary_model,
        "returned_model": returned_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dim,
        "tablestore_table": MEMORY_TABLE,
        "tablestore_index": MEMORY_INDEX,
        "deployed_url": deployed_url,
        "health_endpoint": health_endpoint,
        "deployment_version": health["version"],
        "health_trace_id": health["trace_id"],
    }


def _error_code(error: Exception) -> str | None:
    getter = getattr(error, "get_error_code", None)
    candidates = [getattr(error, "code", None)]
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    for candidate in candidates:
        if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate):
            return candidate
    return None


def run() -> int:
    try:
        settings = Settings.from_env()
        import os

        output = prove(settings, os.getenv("FC_PUBLIC_URL", ""))
    except ProofStageError as exc:
        output = {
            "ok": False,
            "reason": "alibaba_proof_failed",
            "stage": exc.stage,
            "error_type": exc.error_type,
        }
        if exc.error_code is not None:
            output["error_code"] = exc.error_code
    except Exception as exc:
        output = {
            "ok": False,
            "reason": "alibaba_proof_failed",
            "error_type": type(exc).__name__,
        }
        code = _error_code(exc)
        if code is not None:
            output["error_code"] = code
    print(json.dumps(output, sort_keys=True))
    return 0 if output.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(run())
