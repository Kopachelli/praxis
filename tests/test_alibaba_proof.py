from types import SimpleNamespace

import pytest

from deploy.alibaba_proof import ProofStageError, _validated_fc_url, prove


_FC_URL = "https://praxis-api-example.ap-southeast-1.fcapp.run"
_TRACE_ID = "151fe73c-8802-48db-83a9-f96411d9b4bc"


class FakeQwenClient:
    def __init__(
        self,
        *,
        content: str = "PRAXIS_ALIBABA_PROOF",
        returned_model: str = "qwen3.7-max",
    ) -> None:
        self.kwargs = None
        self.closed = False
        self.content = content
        self.returned_model = returned_model
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            model=self.returned_model,
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.content))
            ],
        )

    def close(self) -> None:
        self.closed = True


def _field(
    name: str,
    field_type: str,
    *,
    sort_and_agg: bool | None = None,
    vector_options=None,
):
    return SimpleNamespace(
        field_name=name,
        field_type=SimpleNamespace(name=field_type),
        index=True,
        store=False,
        enable_sort_and_agg=sort_and_agg,
        vector_options=vector_options,
    )


class FakeTablestoreClient:
    def __init__(self) -> None:
        self.tables = ["praxis_memory"]
        self.table = SimpleNamespace(
            table_meta=SimpleNamespace(schema_of_primary_key=[("id", "STRING")]),
            table_options=SimpleNamespace(time_to_live=-1, max_version=1),
        )
        self.indexes = [("praxis_memory", "praxis_memory_index")]
        self.vector_options = SimpleNamespace(
            data_type=SimpleNamespace(name="VD_FLOAT_32"),
            metric_type=SimpleNamespace(name="VM_COSINE"),
            dimension=1024,
        )
        self.index_meta = SimpleNamespace(
            fields=[
                _field(
                    "embedding",
                    "VECTOR",
                    vector_options=self.vector_options,
                ),
                _field("incident_id", "KEYWORD"),
                _field("service", "KEYWORD", sort_and_agg=True),
                _field("signal", "KEYWORD"),
                _field("resolved_at", "KEYWORD"),
                _field("summary", "TEXT"),
                _field("resolution", "TEXT"),
            ]
        )
        self.sync_stat = SimpleNamespace(sync_phase=SimpleNamespace(name="INCR"))

    def list_table(self) -> list[str]:
        return self.tables

    def describe_table(self, _table_name: str):
        return self.table

    def list_search_index(self, _table_name: str):
        return self.indexes

    def describe_search_index(self, _table_name: str, _index_name: str):
        return self.index_meta, self.sync_stat


def _settings():
    return SimpleNamespace(
        primary_model="qwen3.7-max",
        embedding_model="text-embedding-v4",
        embedding_dim=1024,
        app_version="0.1.0",
    )


def _health_response(**updates):
    payload = {
        "ok": True,
        "primary_model": "qwen3.7-max",
        "deployed_on": "alibaba-fc",
        "version": "0.1.0",
        "trace_id": _TRACE_ID,
    }
    payload.update(updates.pop("payload", {}))
    headers = {"X-Trace-Id": _TRACE_ID}
    headers.update(updates.pop("headers", {}))
    status_code = updates.pop("status_code", 200)
    assert not updates
    return SimpleNamespace(
        status_code=status_code,
        headers=headers,
        json=lambda: payload,
    )


def _prove(*, qwen=None, table=None, health=None):
    qwen = qwen or FakeQwenClient()
    table = table or FakeTablestoreClient()
    requested_health_urls: list[str] = []

    def health_get(url: str):
        requested_health_urls.append(url)
        return health or _health_response()

    result = prove(
        _settings(),
        _FC_URL,
        qwen_factory=lambda _settings: qwen,
        tablestore_factory=lambda _settings: table,
        health_get=health_get,
    )
    return result, qwen, requested_health_urls


def test_proof_verifies_qwen_schema_and_deployed_health_contract() -> None:
    result, qwen, requested_health_urls = _prove()

    assert result == {
        "ok": True,
        "provider": "qwencloud",
        "requested_model": "qwen3.7-max",
        "returned_model": "qwen3.7-max",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": 1024,
        "tablestore_table": "praxis_memory",
        "tablestore_index": "praxis_memory_index",
        "deployed_url": _FC_URL,
        "health_endpoint": f"{_FC_URL}/healthz",
        "deployment_version": "0.1.0",
        "health_trace_id": _TRACE_ID,
    }
    assert requested_health_urls == [f"{_FC_URL}/healthz"]
    assert qwen.kwargs["model"] == "qwen3.7-max"
    assert qwen.kwargs["extra_body"] == {"enable_thinking": False}
    assert "exactly PRAXIS_ALIBABA_PROOF and nothing else" in (
        qwen.kwargs["messages"][0]["content"]
    )
    assert qwen.closed is True


@pytest.mark.parametrize(
    "content",
    (
        "",
        "PRAXIS_ALIBABA_PROOF extra",
        "The answer is PRAXIS_ALIBABA_PROOF",
        "PRAXIS_ALIBABA_PROOF.",
    ),
)
def test_qwen_proof_requires_exact_sentinel(content: str) -> None:
    with pytest.raises(ProofStageError) as caught:
        _prove(qwen=FakeQwenClient(content=content))

    assert caught.value.stage == "qwencloud"


def test_qwen_proof_rejects_unsafe_returned_model_identity() -> None:
    secret = "qwen3.7-max\nsecret-model-response-sentinel"

    with pytest.raises(ProofStageError) as caught:
        _prove(qwen=FakeQwenClient(returned_model=secret))

    assert caught.value.stage == "qwencloud"
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_table",
        "table_pk",
        "missing_index",
        "extra_index_field",
        "vector_dimension",
        "sync",
    ),
)
def test_tablestore_proof_requires_expected_schema(mutation: str) -> None:
    table = FakeTablestoreClient()
    if mutation == "missing_table":
        table.tables = []
    elif mutation == "table_pk":
        table.table.table_meta.schema_of_primary_key = [("wrong", "STRING")]
    elif mutation == "missing_index":
        table.indexes = []
    elif mutation == "extra_index_field":
        table.index_meta.fields.append(_field("unexpected", "TEXT"))
    elif mutation == "vector_dimension":
        table.vector_options.dimension = 1536
    elif mutation == "sync":
        table.sync_stat.sync_phase = SimpleNamespace(name="FULL")

    with pytest.raises(ProofStageError) as caught:
        _prove(table=table)

    assert caught.value.stage == "tablestore"


@pytest.mark.parametrize(
    "health",
    (
        _health_response(status_code=503),
        _health_response(payload={"ok": False}),
        _health_response(payload={"deployed_on": "local"}),
        _health_response(payload={"primary_model": "qwen-plus"}),
        _health_response(payload={"version": "0.2.0"}),
        _health_response(payload={"version": "bad version"}),
        _health_response(payload={"trace_id": "not-a-uuid"}),
        _health_response(headers={"X-Trace-Id": "different"}),
    ),
)
def test_fc_proof_requires_praxis_deployment_markers(health) -> None:
    with pytest.raises(ProofStageError) as caught:
        _prove(health=health)

    assert caught.value.stage == "function_compute"


@pytest.mark.parametrize(
    "url",
    (
        "http://praxis-api-example.ap-southeast-1.fcapp.run",
        "https://praxis-api-example.ap-southeast-1.fcapp.run.evil.test",
        "https://user@praxis-api-example.ap-southeast-1.fcapp.run",
        "https://praxis-api-example.ap-southeast-1.fcapp.run/path",
    ),
)
def test_fc_public_url_fails_closed(url: str) -> None:
    with pytest.raises(ValueError, match="FC_PUBLIC_URL"):
        _validated_fc_url(url)


def test_proof_reports_contract_stage_without_raw_value() -> None:
    with pytest.raises(ProofStageError) as caught:
        prove(
            _settings(),
            "https://user:secret@example.test",
            qwen_factory=lambda _settings: FakeQwenClient(),
            tablestore_factory=lambda _settings: FakeTablestoreClient(),
            health_get=lambda _url: _health_response(),
        )

    assert caught.value.stage == "contract"
    assert caught.value.error_type == "ValueError"
    assert "secret" not in str(caught.value)
