import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from scripts import probe_memory


class _Sdk:
    FieldType = SimpleNamespace(VECTOR="VECTOR", KEYWORD="KEYWORD", TEXT="TEXT")
    VectorDataType = SimpleNamespace(VD_FLOAT_32="VD_FLOAT_32")
    VectorMetricType = SimpleNamespace(VM_COSINE="VM_COSINE")
    SyncPhase = SimpleNamespace(INCR="INCR")


def _field(
    name: str,
    field_type: str,
    *,
    sort_and_agg: bool | None = None,
    vector_options=None,
):
    return SimpleNamespace(
        field_name=name,
        field_type=field_type,
        index=True,
        store=False,
        enable_sort_and_agg=sort_and_agg,
        vector_options=vector_options,
    )


def _valid_client():
    vector_options = SimpleNamespace(
        data_type="VD_FLOAT_32",
        metric_type="VM_COSINE",
        dimension=1024,
    )
    client = SimpleNamespace(
        tables=["praxis_memory"],
        table=SimpleNamespace(
            table_meta=SimpleNamespace(schema_of_primary_key=[("id", "STRING")]),
            table_options=SimpleNamespace(time_to_live=-1, max_version=1),
        ),
        indexes=[("praxis_memory", "praxis_memory_index")],
        index_meta=SimpleNamespace(
            fields=[
                _field("embedding", "VECTOR", vector_options=vector_options),
                _field("incident_id", "KEYWORD"),
                _field("service", "KEYWORD", sort_and_agg=True),
                _field("signal", "KEYWORD"),
                _field("resolved_at", "KEYWORD"),
                _field("summary", "TEXT"),
                _field("resolution", "TEXT"),
            ]
        ),
        sync_stat=SimpleNamespace(sync_phase="INCR"),
        closed=False,
    )

    async def list_table():
        return client.tables

    async def describe_table(_table_name):
        return client.table

    async def list_search_index(_table_name):
        return client.indexes

    async def describe_search_index(_table_name, _index_name):
        return client.index_meta, client.sync_stat

    async def close():
        client.closed = True

    client.list_table = list_table
    client.describe_table = describe_table
    client.list_search_index = list_search_index
    client.describe_search_index = describe_search_index
    client.close = close
    return client


def _settings():
    return SimpleNamespace(
        tablestore_endpoint="https://instance.example.invalid",
        alibaba_access_key_id="secret-id",
        alibaba_access_key_secret="secret-key",
        tablestore_instance="instance",
        alibaba_security_token="secret-token",
        embedding_dim=1024,
    )


def _run_probe(monkeypatch, client):
    sdk = SimpleNamespace(
        AsyncOTSClient=lambda *_args, **_kwargs: client,
        FieldType=_Sdk.FieldType,
        VectorDataType=_Sdk.VectorDataType,
        VectorMetricType=_Sdk.VectorMetricType,
        SyncPhase=_Sdk.SyncPhase,
    )
    monkeypatch.setitem(sys.modules, "tablestore", sdk)
    return asyncio.run(probe_memory._probe(_settings()))


def test_probe_succeeds_only_after_full_schema_verification(monkeypatch) -> None:
    client = _valid_client()

    result = _run_probe(monkeypatch, client)

    assert result == {
        "ok": True,
        "backend": "tablestore",
        "table_exists": True,
        "index_exists": True,
        "table_pk_matches": True,
        "table_options_match": True,
        "index_field_count": 7,
        "index_schema_matches": True,
        "index_sync_phase": "INCR",
        "embedding_dimension": 1024,
    }
    assert client.closed is True


@pytest.mark.parametrize(
    ("resource", "reason"),
    (("table", "table_missing"), ("index", "index_missing")),
)
def test_probe_fails_closed_for_missing_resources(
    monkeypatch,
    resource,
    reason,
) -> None:
    client = _valid_client()
    if resource == "table":
        client.tables = []
    else:
        client.indexes = []

    result = _run_probe(monkeypatch, client)

    assert result["ok"] is False
    assert result["reason"] == reason
    assert "provision_memory.py" in result["action"]
    assert client.closed is True


def _mutate(client, case: str) -> None:
    fields = {field.field_name: field for field in client.index_meta.fields}
    vector = fields["embedding"]
    if case == "table.primary_key":
        client.table.table_meta.schema_of_primary_key = [("wrong", "STRING")]
    elif case == "table.time_to_live":
        client.table.table_options.time_to_live = 86400
    elif case == "table.max_version":
        client.table.table_options.max_version = 2
    elif case == "index.fields":
        client.index_meta.fields = client.index_meta.fields[:-1]
    elif case == "index.extra_field":
        client.index_meta.fields.append(_field("unexpected", "TEXT"))
    elif case == "index.embedding.type":
        vector.field_type = "TEXT"
    elif case == "index.embedding.index":
        vector.index = False
    elif case == "index.embedding.store":
        vector.store = True
    elif case == "index.embedding.data_type":
        vector.vector_options.data_type = "VD_DOUBLE"
    elif case == "index.embedding.metric":
        vector.vector_options.metric_type = "VM_EUCLIDEAN"
    elif case == "index.embedding.dimension":
        vector.vector_options.dimension = 1536
    elif case == "index.incident_id":
        fields["incident_id"].field_type = "TEXT"
    elif case == "index.service_filter":
        fields["service"].enable_sort_and_agg = False
    elif case == "index.summary":
        fields["summary"].store = True
    elif case == "index.sync_phase":
        client.sync_stat.sync_phase = "FULL"
    else:  # pragma: no cover - protects the test table itself
        raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "diagnostic"),
    (
        ("table.primary_key", "table.primary_key"),
        ("table.time_to_live", "table.time_to_live"),
        ("table.max_version", "table.max_version"),
        ("index.fields", "index.fields"),
        ("index.extra_field", "index.fields"),
        ("index.embedding.type", "index.embedding"),
        ("index.embedding.index", "index.embedding"),
        ("index.embedding.store", "index.embedding"),
        ("index.embedding.data_type", "index.embedding"),
        ("index.embedding.metric", "index.embedding"),
        ("index.embedding.dimension", "index.embedding"),
        ("index.incident_id", "index.incident_id"),
        ("index.service_filter", "index.service_filter"),
        ("index.summary", "index.summary"),
        ("index.sync_phase", "index.sync_phase"),
    ),
)
def test_probe_schema_mismatch_matrix(monkeypatch, case, diagnostic) -> None:
    client = _valid_client()
    _mutate(client, case)

    result = _run_probe(monkeypatch, client)

    assert result["ok"] is False
    assert result["reason"] == "schema_mismatch"
    assert diagnostic in result["mismatches"]
    assert "provision_memory.py" in result["action"]


def test_run_returns_nonzero_for_failed_probe(monkeypatch, capsys) -> None:
    async def failed_probe(_settings):
        return {"ok": False, "reason": "table_missing"}

    monkeypatch.setattr(probe_memory.Settings, "from_env", lambda: object())
    monkeypatch.setattr(probe_memory, "_probe", failed_probe)

    assert probe_memory.run() == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_exception_diagnostics_do_not_expose_error_text(monkeypatch, capsys) -> None:
    secret = "provider-response-secret-sentinel"

    async def failed_probe(_settings):
        raise RuntimeError(secret)

    monkeypatch.setattr(probe_memory.Settings, "from_env", lambda: object())
    monkeypatch.setattr(probe_memory, "_probe", failed_probe)

    assert probe_memory.run() == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert json.loads(output)["error_type"] == "RuntimeError"
