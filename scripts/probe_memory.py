"""Read-only, allowlisted Tablestore memory diagnostics [FR-10, NFR-5]."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.memory import MEMORY_INDEX, MEMORY_TABLE  # noqa: E402
from app.config import Settings  # noqa: E402


_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_EXPECTED_INDEX_FIELDS = {
    "embedding",
    "incident_id",
    "service",
    "signal",
    "resolved_at",
    "summary",
    "resolution",
}
_SCHEMA_ACTION = (
    "Run python scripts/provision_memory.py for this environment, then rerun "
    "python scripts/probe_memory.py."
)


def _error_code(error: Exception) -> str | None:
    getter = getattr(error, "get_error_code", None)
    if getter is None:
        return None
    try:
        value = getter()
    except Exception:
        return None
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _table_mismatches(table: Any) -> list[str]:
    """Return fixed, secret-safe identifiers for accepted table-schema drift."""

    mismatches: list[str] = []
    table_meta = getattr(table, "table_meta", None)
    table_options = getattr(table, "table_options", None)
    if (
        table_meta is None
        or list(getattr(table_meta, "schema_of_primary_key", ()))
        != [("id", "STRING")]
    ):
        mismatches.append("table.primary_key")
    if table_options is None or getattr(table_options, "time_to_live", None) != -1:
        mismatches.append("table.time_to_live")
    if table_options is None or getattr(table_options, "max_version", None) != 1:
        mismatches.append("table.max_version")
    return mismatches


def _index_mismatches(
    index_meta: Any,
    sync_stat: Any,
    sdk: Any,
    *,
    embedding_dimension: int,
) -> list[str]:
    """Return fixed identifiers for every runtime-required search-index field."""

    raw_fields = list(getattr(index_meta, "fields", ()))
    fields = {
        field.field_name: field
        for field in raw_fields
        if getattr(field, "field_name", None) in _EXPECTED_INDEX_FIELDS
    }
    mismatches: list[str] = []
    if (
        len(raw_fields) != len(_EXPECTED_INDEX_FIELDS)
        or set(fields) != _EXPECTED_INDEX_FIELDS
    ):
        mismatches.append("index.fields")
        return mismatches

    vector = fields["embedding"]
    options = getattr(vector, "vector_options", None)
    if (
        getattr(vector, "field_type", None) != sdk.FieldType.VECTOR
        or getattr(vector, "index", None) is not True
        or getattr(vector, "store", None) is not False
        or options is None
        or getattr(options, "data_type", None) != sdk.VectorDataType.VD_FLOAT_32
        or getattr(options, "metric_type", None) != sdk.VectorMetricType.VM_COSINE
        or getattr(options, "dimension", None) != embedding_dimension
    ):
        mismatches.append("index.embedding")

    for name in ("incident_id", "service", "signal", "resolved_at"):
        field = fields[name]
        if (
            getattr(field, "field_type", None) != sdk.FieldType.KEYWORD
            or getattr(field, "index", None) is not True
            or getattr(field, "store", None) is not False
        ):
            mismatches.append(f"index.{name}")
    if getattr(fields["service"], "enable_sort_and_agg", None) is not True:
        mismatches.append("index.service_filter")

    for name in ("summary", "resolution"):
        field = fields[name]
        if (
            getattr(field, "field_type", None) != sdk.FieldType.TEXT
            or getattr(field, "index", None) is not True
            or getattr(field, "store", None) is not False
        ):
            mismatches.append(f"index.{name}")

    if (
        sync_stat is None
        or getattr(sync_stat, "sync_phase", None) != sdk.SyncPhase.INCR
    ):
        mismatches.append("index.sync_phase")
    return mismatches


async def _probe(settings: Settings) -> dict[str, Any]:
    import tablestore as sdk

    client = sdk.AsyncOTSClient(
        settings.tablestore_endpoint,
        settings.alibaba_access_key_id,
        settings.alibaba_access_key_secret,
        settings.tablestore_instance,
        sts_token=settings.alibaba_security_token or None,
        region="ap-southeast-1",
        socket_timeout=(5, 10),
        enable_native=False,
    )
    result: dict[str, Any] = {
        "ok": False,
        "backend": "tablestore",
        "table_exists": False,
        "index_exists": False,
    }
    try:
        tables = await asyncio.wait_for(client.list_table(), timeout=15)
        result["table_exists"] = MEMORY_TABLE in tables
        if not result["table_exists"]:
            result["reason"] = "table_missing"
            result["action"] = _SCHEMA_ACTION
            return result

        table = await asyncio.wait_for(client.describe_table(MEMORY_TABLE), timeout=15)
        table_mismatches = _table_mismatches(table)
        result["table_pk_matches"] = "table.primary_key" not in table_mismatches
        result["table_options_match"] = not any(
            item.startswith("table.") and item != "table.primary_key"
            for item in table_mismatches
        )

        indexes = await asyncio.wait_for(
            client.list_search_index(MEMORY_TABLE),
            timeout=15,
        )
        result["index_exists"] = (MEMORY_TABLE, MEMORY_INDEX) in indexes
        if not result["index_exists"]:
            result["reason"] = "index_missing"
            result["action"] = _SCHEMA_ACTION
            return result

        index_meta, sync_stat = await asyncio.wait_for(
            client.describe_search_index(MEMORY_TABLE, MEMORY_INDEX),
            timeout=15,
        )
        mismatches = table_mismatches + _index_mismatches(
            index_meta,
            sync_stat,
            sdk,
            embedding_dimension=settings.embedding_dim,
        )
        if mismatches:
            result["reason"] = "schema_mismatch"
            result["mismatches"] = mismatches
            result["action"] = _SCHEMA_ACTION
            return result

        result.update(
            {
                "ok": True,
                "index_field_count": len(_EXPECTED_INDEX_FIELDS),
                "index_schema_matches": True,
                "index_sync_phase": "INCR",
                "embedding_dimension": settings.embedding_dim,
            }
        )
        return result
    finally:
        await client.close()


def run() -> int:
    try:
        output = asyncio.run(_probe(Settings.from_env()))
    except Exception as exc:
        output = {
            "ok": False,
            "reason": "memory_probe_failed",
            "error_type": type(exc).__name__,
            "error_code": _error_code(exc),
        }
    print(json.dumps(output, sort_keys=True))
    return 0 if output.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(run())
