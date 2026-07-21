"""Qwen Cloud embeddings and incident memory backends [FR-10, FR-11]."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, validate_qwen_base_url
from app.incidents import Incident, IncidentState, IncidentStore
from app.trail import TrailEntryType

EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMENSION = 1024
MEMORY_TABLE = "praxis_memory"
MEMORY_INDEX = "praxis_memory_index"
MEMORY_TOP_K = 3
MAX_MEMORY_SUMMARY_CHARS = 2_000
MAX_MEMORY_RESOLUTION_CHARS = 4_000
MAX_EMBEDDING_INPUT_CHARS = 6_000
MAX_KEYWORD_UTF8_BYTES = 2_048
FLOAT32_MAX = 3.4028235e38
RUNTIME_OPERATION_TIMEOUT_SECONDS = 10.0
_SCHEMA_RETRY_DELAYS = (0.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0)
_VISIBILITY_RETRY_DELAYS = (0.0, 0.25, 0.75, 1.5, 3.0)
_RETRYABLE_SCHEMA_CODES = frozenset(
    {
        "OTSObjectNotExist",
        "OTSPartitionUnavailable",
        "OTSTableNotReady",
        "OTSInternalServerError",
        "OTSServerUnavailable",
        "OTSServerBusy",
        "OTSTimeout",
        "OTSOperationConflict",
        "OTSRowOperationConflict",
        "OTSFlowControl",
    }
)
_ALREADY_EXISTS_CODES = frozenset({"OTSObjectAlreadyExist"})


class _SchemaNotReady(RuntimeError):
    """Internal retry signal; never crosses the public memory boundary."""


class MemoryError(RuntimeError):
    """Base class for deliberately redacted memory failures."""


class MemoryConfigurationError(MemoryError):
    """The accepted memory configuration is incomplete or inconsistent."""


class MemoryProviderError(MemoryError):
    """Qwen Cloud could not return a valid embedding."""


class MemoryBackendError(MemoryError):
    """The configured persistence backend could not complete an operation."""


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    model: str
    tokens: int | None


class IncidentMemory(BaseModel):
    """Persistent, secret-safe record written only after resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    incident_id: str
    service: str
    signal: str
    summary: str
    resolution: str
    tags: tuple[str, ...]
    resolved_at: datetime


class MemoryMatch(BaseModel):
    """Public subset of the top result exposed to planning and the UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    similarity: float = Field(ge=0.0, le=1.0)
    summary: str
    resolution: str


class EmbeddingClient(Protocol):
    async def embed(self, text: str) -> EmbeddingResult: ...

    async def aclose(self) -> None: ...


class MemoryBackend(Protocol):
    name: str

    async def ensure_ready(self) -> None: ...

    async def write(
        self,
        memory: IncidentMemory,
        embedding: Sequence[float],
    ) -> None: ...

    async def search(
        self,
        *,
        service: str,
        embedding: Sequence[float],
        threshold: float,
    ) -> MemoryMatch | None: ...

    async def aclose(self) -> None: ...


class QwenCloudEmbeddingClient:
    """Call only Model Studio's Qwen embedding endpoint; never OpenRouter."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if settings.embedding_model != EMBEDDING_MODEL:
            raise MemoryConfigurationError(
                f"EMBEDDING_MODEL must remain {EMBEDDING_MODEL}"
            )
        if settings.embedding_dim != EMBEDDING_DIMENSION:
            raise MemoryConfigurationError(
                f"EMBEDDING_DIM must remain {EMBEDDING_DIMENSION}"
            )
        if not 0 < timeout_seconds <= 15:
            raise ValueError("embedding timeout must be in (0, 15]")

        self._api_key = settings.dashscope_api_key
        self._model = settings.embedding_model
        self._dimension = settings.embedding_dim
        self._endpoint = f"{validate_qwen_base_url(settings.qwen_base_url)}/embeddings"
        self._client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
        )
        self._owns_client = http_client is None

    async def embed(self, text: str) -> EmbeddingResult:
        value = text.strip()
        if not value:
            raise ValueError("embedding input must not be empty")
        if len(value) > MAX_EMBEDDING_INPUT_CHARS:
            raise ValueError("embedding input exceeds the bounded memory context")
        if not self._api_key:
            raise MemoryProviderError("Qwen Cloud embedding credential is unavailable")

        try:
            response = await self._client.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": value,
                    "dimensions": self._dimension,
                    "encoding_format": "float",
                },
            )
        except httpx.HTTPError:
            raise MemoryProviderError(
                "Qwen Cloud embedding request failed"
            ) from None

        if response.status_code != 200:
            raise MemoryProviderError(
                f"Qwen Cloud embedding returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise MemoryProviderError(
                "Qwen Cloud embedding returned invalid JSON"
            ) from None
        if not isinstance(payload, Mapping):
            raise MemoryProviderError("Qwen Cloud embedding response is invalid")

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
            raise MemoryProviderError("Qwen Cloud embedding response is invalid")
        vector = _validated_vector(data[0].get("embedding"), self._dimension)
        response_model = payload.get("model")
        if response_model not in {None, self._model}:
            raise MemoryProviderError("Qwen Cloud embedding model mismatch")

        usage = payload.get("usage")
        tokens = None
        if isinstance(usage, Mapping):
            candidate = usage.get("total_tokens")
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                tokens = candidate
        return EmbeddingResult(vector=vector, model=self._model, tokens=tokens)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class InMemoryMemoryBackend:
    """Accepted non-persistent fallback behind the same vector interface."""

    name = "inmem"

    def __init__(self, *, dimension: int = EMBEDDING_DIMENSION) -> None:
        self._dimension = dimension
        self._records: dict[str, tuple[IncidentMemory, tuple[float, ...]]] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        return None

    async def write(
        self,
        memory: IncidentMemory,
        embedding: Sequence[float],
    ) -> None:
        vector = _validated_vector(embedding, self._dimension)
        async with self._lock:
            self._records[memory.id] = (memory.model_copy(deep=True), vector)

    async def search(
        self,
        *,
        service: str,
        embedding: Sequence[float],
        threshold: float,
    ) -> MemoryMatch | None:
        query = _validated_vector(embedding, self._dimension)
        bounded_service = _bounded_keyword(service)
        async with self._lock:
            candidates = tuple(self._records.values())

        best: tuple[float, IncidentMemory] | None = None
        for memory, vector in candidates:
            if memory.service != bounded_service:
                continue
            similarity = _cosine_similarity(query, vector)
            if similarity < threshold:
                continue
            if best is None or similarity > best[0]:
                best = (similarity, memory)
        if best is None:
            return None
        similarity, memory = best
        return MemoryMatch(
            incident_id=memory.incident_id,
            similarity=min(1.0, max(0.0, similarity)),
            summary=memory.summary,
            resolution=memory.resolution,
        )

    async def aclose(self) -> None:
        return None


class TablestoreMemoryBackend:
    """Persistent Alibaba Tablestore vector backend fixed by ADR-004."""

    name = "tablestore"

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sdk: Any | None = None,
        allow_schema_changes: bool = False,
        schema_retry_delays: Sequence[float] = _SCHEMA_RETRY_DELAYS,
        visibility_retry_delays: Sequence[float] = _VISIBILITY_RETRY_DELAYS,
        operation_timeout_seconds: float = RUNTIME_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if settings.memory_backend != self.name:
            raise MemoryConfigurationError("Tablestore backend was not selected")
        self._settings = settings
        self._client = client
        self._sdk = sdk
        self._owns_client = client is None
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._allow_schema_changes = allow_schema_changes
        self._schema_retry_delays = tuple(schema_retry_delays)
        self._visibility_retry_delays = tuple(visibility_retry_delays)
        if not self._schema_retry_delays or any(
            delay < 0 for delay in self._schema_retry_delays
        ):
            raise ValueError("schema retry delays must contain non-negative values")
        if not self._visibility_retry_delays or any(
            delay < 0 for delay in self._visibility_retry_delays
        ):
            raise ValueError(
                "visibility retry delays must contain non-negative values"
            )
        if (
            not isinstance(operation_timeout_seconds, (int, float))
            or isinstance(operation_timeout_seconds, bool)
            or not math.isfinite(float(operation_timeout_seconds))
            or operation_timeout_seconds <= 0
        ):
            raise ValueError("operation timeout must be finite and positive")
        self._operation_timeout_seconds = float(operation_timeout_seconds)

    def _client_and_sdk(self) -> tuple[Any, Any]:
        if self._sdk is None:
            try:
                import tablestore as sdk
            except ImportError:
                raise MemoryConfigurationError(
                    "The tablestore package is unavailable"
                ) from None
            self._sdk = sdk
        if self._client is None:
            self._client = self._sdk.AsyncOTSClient(
                self._settings.tablestore_endpoint,
                self._settings.alibaba_access_key_id,
                self._settings.alibaba_access_key_secret,
                self._settings.tablestore_instance,
                sts_token=self._settings.alibaba_security_token or None,
                region="ap-southeast-1",
                socket_timeout=(5, 10),
                enable_native=False,
            )
        return self._client, self._sdk

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            client, sdk = self._client_and_sdk()
            try:
                if self._allow_schema_changes:
                    await self._provision_and_verify(client, sdk)
                else:
                    await asyncio.wait_for(
                        self._verify_existing_schema(client, sdk),
                        timeout=self._operation_timeout_seconds,
                    )
            except MemoryConfigurationError:
                raise
            except MemoryError:
                raise
            except Exception:
                raise MemoryBackendError(
                    "Tablestore memory schema could not be prepared"
                ) from None
            self._ready = True

    async def _provision_and_verify(self, client: Any, sdk: Any) -> None:
        """Create idempotently, then poll until the index is incrementally synced."""

        for attempt, delay in enumerate(self._schema_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                tables = await self._bounded(client.list_table())
                if MEMORY_TABLE not in tables:
                    try:
                        await self._bounded(
                            client.create_table(
                                sdk.TableMeta(MEMORY_TABLE, [("id", "STRING")]),
                                sdk.TableOptions(time_to_live=-1, max_version=1),
                                sdk.ReservedThroughput(sdk.CapacityUnit(0, 0)),
                            )
                        )
                    except Exception as exc:
                        if not _already_exists_error(exc):
                            raise
                    raise _SchemaNotReady

                table = await self._bounded(client.describe_table(MEMORY_TABLE))
                _verify_table_schema(table)

                indexes = await self._bounded(client.list_search_index(MEMORY_TABLE))
                if (MEMORY_TABLE, MEMORY_INDEX) not in indexes:
                    try:
                        await self._bounded(
                            client.create_search_index(
                                MEMORY_TABLE,
                                MEMORY_INDEX,
                                _search_index_meta(
                                    sdk,
                                    self._settings.embedding_dim,
                                ),
                            )
                        )
                    except Exception as exc:
                        if not _already_exists_error(exc):
                            raise
                    raise _SchemaNotReady

                await self._verify_index(client, sdk)
                return
            except MemoryConfigurationError:
                raise
            except Exception as exc:
                if not _retryable_schema_error(exc):
                    raise MemoryBackendError(
                        "Tablestore memory schema could not be provisioned"
                    ) from None
                if attempt == len(self._schema_retry_delays) - 1:
                    break
        raise MemoryBackendError("Tablestore memory schema did not become ready")

    async def _verify_existing_schema(self, client: Any, sdk: Any) -> None:
        table = await client.describe_table(MEMORY_TABLE)
        _verify_table_schema(table)
        indexes = await client.list_search_index(MEMORY_TABLE)
        if (MEMORY_TABLE, MEMORY_INDEX) not in indexes:
            raise MemoryConfigurationError(
                "Tablestore memory search index is not provisioned"
            )
        await self._verify_index(client, sdk)

    async def _verify_index(self, client: Any, sdk: Any) -> None:
        result = await client.describe_search_index(MEMORY_TABLE, MEMORY_INDEX)
        index_meta = result[0] if isinstance(result, tuple) else result
        sync_stat = (
            result[1]
            if isinstance(result, tuple) and len(result) > 1
            else None
        )
        fields = {
            field.field_name: field
            for field in getattr(index_meta, "fields", ())
        }
        expected_names = {
            "embedding",
            "incident_id",
            "service",
            "signal",
            "resolved_at",
            "summary",
            "resolution",
        }
        if set(fields) != expected_names:
            raise MemoryConfigurationError(
                "Existing Tablestore index fields do not match ADR-004"
            )

        vector = fields["embedding"]
        options = getattr(vector, "vector_options", None)
        if (
            vector.field_type != sdk.FieldType.VECTOR
            or vector.index is not True
            or vector.store is not False
            or options is None
            or options.data_type != sdk.VectorDataType.VD_FLOAT_32
            or options.metric_type != sdk.VectorMetricType.VM_COSINE
            or options.dimension != self._settings.embedding_dim
        ):
            raise MemoryConfigurationError(
                "Existing Tablestore vector index does not match ADR-004"
            )

        keyword_fields = {"incident_id", "service", "signal", "resolved_at"}
        for name in keyword_fields:
            field = fields[name]
            if (
                field.field_type != sdk.FieldType.KEYWORD
                or field.index is not True
                or field.store is not False
            ):
                raise MemoryConfigurationError(
                    "Existing Tablestore keyword fields do not match ADR-004"
                )
        if getattr(fields["service"], "enable_sort_and_agg", None) is not True:
            raise MemoryConfigurationError(
                "Existing Tablestore service field cannot support filtering"
            )
        for name in {"summary", "resolution"}:
            field = fields[name]
            if (
                field.field_type != sdk.FieldType.TEXT
                or field.index is not True
                or field.store is not False
            ):
                raise MemoryConfigurationError(
                    "Existing Tablestore text fields do not match ADR-004"
                )
        if sync_stat is None or sync_stat.sync_phase != sdk.SyncPhase.INCR:
            raise _SchemaNotReady

    async def _bounded(self, operation: Any) -> Any:
        return await asyncio.wait_for(
            operation,
            timeout=self._operation_timeout_seconds,
        )

    async def write(
        self,
        memory: IncidentMemory,
        embedding: Sequence[float],
    ) -> None:
        await self.ensure_ready()
        client, sdk = self._client_and_sdk()
        vector = _validated_vector(embedding, self._settings.embedding_dim)
        row = sdk.Row(
            [("id", memory.id)],
            [
                ("incident_id", memory.incident_id),
                ("service", memory.service),
                ("signal", memory.signal),
                ("resolved_at", memory.resolved_at.isoformat()),
                ("summary", memory.summary),
                ("resolution", memory.resolution),
                ("tags", json.dumps(memory.tags, separators=(",", ":"))),
                ("embedding", json.dumps(vector, separators=(",", ":"))),
            ],
        )
        try:
            await self._bounded(
                client.put_row(
                    MEMORY_TABLE,
                    row,
                    sdk.Condition(sdk.RowExistenceExpectation.IGNORE),
                )
            )
            await self._wait_until_visible(client, sdk, memory.incident_id)
        except Exception:
            raise MemoryBackendError("Tablestore memory write failed") from None

    async def _wait_until_visible(
        self,
        client: Any,
        sdk: Any,
        incident_id: str,
    ) -> None:
        """Poll the async search index so an immediate recurrence is reliable."""

        for attempt, delay in enumerate(self._visibility_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._bounded(
                    client.search(
                        MEMORY_TABLE,
                        MEMORY_INDEX,
                        sdk.SearchQuery(
                            sdk.TermQuery("incident_id", incident_id),
                            limit=1,
                        ),
                        sdk.ColumnsToGet(
                            ["incident_id"],
                            return_type=sdk.ColumnReturnType.SPECIFIED,
                        ),
                        timeout_s=max(
                            1,
                            math.ceil(self._operation_timeout_seconds),
                        ),
                    )
                )
            except Exception as exc:
                if not _retryable_schema_error(exc):
                    raise
                if attempt == len(self._visibility_retry_delays) - 1:
                    break
                continue
            if (
                getattr(response, "is_all_succeed", False) is True
                and bool(
                    getattr(response, "search_hits", ())
                    or getattr(response, "rows", ())
                )
            ):
                return
        raise MemoryBackendError("Tablestore memory row is not search-visible")

    async def search(
        self,
        *,
        service: str,
        embedding: Sequence[float],
        threshold: float,
    ) -> MemoryMatch | None:
        await self.ensure_ready()
        client, sdk = self._client_and_sdk()
        vector = _validated_vector(embedding, self._settings.embedding_dim)
        bounded_service = _bounded_keyword(service)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("memory threshold must be between zero and one")
        query = sdk.KnnVectorQuery(
            field_name="embedding",
            top_k=MEMORY_TOP_K,
            float32_query_vector=list(vector),
            filter=sdk.TermQuery("service", bounded_service),
            min_score=threshold,
        )
        columns = sdk.ColumnsToGet(
            [
                "incident_id",
                "service",
                "signal",
                "resolved_at",
                "summary",
                "resolution",
            ],
            return_type=sdk.ColumnReturnType.SPECIFIED,
        )
        try:
            response = await self._bounded(
                client.search(
                    MEMORY_TABLE,
                    MEMORY_INDEX,
                    sdk.SearchQuery(
                        query,
                        sort=sdk.Sort([sdk.ScoreSort()]),
                        limit=MEMORY_TOP_K,
                    ),
                    columns,
                    timeout_s=max(
                        1,
                        math.ceil(self._operation_timeout_seconds),
                    ),
                )
            )
        except Exception:
            raise MemoryBackendError("Tablestore memory recall failed") from None

        if getattr(response, "is_all_succeed", False) is not True:
            raise MemoryBackendError("Tablestore memory recall was incomplete")
        hits = getattr(response, "search_hits", ())
        if not hits:
            return None
        top = hits[0]
        score = getattr(top, "score", None)
        row = getattr(top, "row", None)
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or row is None
        ):
            raise MemoryBackendError("Tablestore memory result is invalid")
        similarity = min(1.0, max(0.0, float(score)))
        if similarity < threshold:
            return None
        try:
            primary_key, attributes = row
            primary = _columns_to_mapping(primary_key)
            values = _columns_to_mapping(attributes)
            incident_id = _memory_incident_id(
                values.get("incident_id"),
                primary.get("id"),
            )
            summary = values["summary"]
            resolution = values["resolution"]
        except (KeyError, TypeError, ValueError):
            raise MemoryBackendError("Tablestore memory result is invalid") from None
        if not all(
            isinstance(value, str) and value
            for value in (incident_id, summary, resolution)
        ):
            raise MemoryBackendError("Tablestore memory result is invalid")
        return MemoryMatch(
            incident_id=incident_id,
            similarity=similarity,
            summary=summary,
            resolution=resolution,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()


class IncidentMemoryService:
    """Coordinate embedding, recall, trail evidence, and post-resolution writes."""

    def __init__(
        self,
        store: IncidentStore,
        embedding_client: EmbeddingClient,
        backend: MemoryBackend,
        *,
        threshold: float,
        logger: logging.Logger,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("memory threshold must be between 0 and 1")
        self._store = store
        self._embedding = embedding_client
        self._backend = backend
        self._threshold = threshold
        self._logger = logger

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def ensure_ready(self) -> None:
        await self._backend.ensure_ready()

    async def recall(self, incident: Incident, trace_id: str) -> MemoryMatch | None:
        try:
            result = await self._embedding.embed(_incident_query_text(incident))
            self._record_embedding(incident.id, trace_id, result, operation="query")
            match = await self._backend.search(
                service=incident.service,
                embedding=result.vector,
                threshold=self._threshold,
            )
            serialized = match.model_dump(mode="json") if match is not None else None
            self._store.set_memory_match(incident.id, serialized)
            self._store.append_trail(
                incident.id,
                TrailEntryType.THOUGHT,
                {
                    "stage": "memory_recall",
                    "status": "matched" if match is not None else "miss",
                    "backend": self._backend.name,
                    "match_incident_id": match.incident_id if match is not None else None,
                    "similarity": match.similarity if match is not None else None,
                    "trace_id": trace_id,
                },
            )
            return match
        except Exception as exc:
            self._store.set_memory_match(incident.id, None)
            self._store.append_trail(
                incident.id,
                TrailEntryType.THOUGHT,
                {
                    "stage": "memory_recall",
                    "status": "unavailable",
                    "backend": self._backend.name,
                    "reason": "memory_unavailable",
                    "trace_id": trace_id,
                },
            )
            self._logger.warning(
                "memory_recall_unavailable",
                extra={
                    "incident_id": incident.id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
            return None

    async def remember_resolution(self, incident: Incident, trace_id: str) -> bool:
        if incident.state is not IncidentState.RESOLVED:
            raise ValueError("only resolved incidents can be written to memory")
        plan = self._store.get_plan(incident.id)
        memory = _resolved_memory(incident, plan)
        try:
            result = await self._embedding.embed(_memory_document_text(memory))
            self._record_embedding(incident.id, trace_id, result, operation="document")
            await self._backend.write(memory, result.vector)
            self._store.append_trail(
                incident.id,
                TrailEntryType.THOUGHT,
                {
                    "stage": "memory_write",
                    "status": "stored",
                    "backend": self._backend.name,
                    "memory_id": memory.id,
                    "trace_id": trace_id,
                },
            )
            return True
        except Exception as exc:
            self._store.append_trail(
                incident.id,
                TrailEntryType.THOUGHT,
                {
                    "stage": "memory_write",
                    "status": "unavailable",
                    "backend": self._backend.name,
                    "reason": "memory_unavailable",
                    "trace_id": trace_id,
                },
            )
            self._logger.warning(
                "memory_write_unavailable",
                extra={
                    "incident_id": incident.id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    def _record_embedding(
        self,
        incident_id: str,
        trace_id: str,
        result: EmbeddingResult,
        *,
        operation: str,
    ) -> None:
        self._store.append_trail(
            incident_id,
            TrailEntryType.THOUGHT,
            {
                "stage": "memory_embedding",
                "operation": operation,
                "provider": "qwencloud",
                "model": result.model,
                "dimensions": len(result.vector),
                "trace_id": trace_id,
            },
            model_used=result.model,
            tokens=result.tokens,
        )

    async def aclose(self) -> None:
        try:
            await self._backend.aclose()
        finally:
            await self._embedding.aclose()


def build_memory_backend(settings: Settings) -> MemoryBackend:
    if settings.memory_backend == "inmem":
        return InMemoryMemoryBackend(dimension=settings.embedding_dim)
    if settings.memory_backend == "tablestore":
        return TablestoreMemoryBackend(settings)
    raise MemoryConfigurationError("Unknown memory backend")


def build_memory_service(
    settings: Settings,
    store: IncidentStore,
    logger: logging.Logger,
) -> IncidentMemoryService:
    return IncidentMemoryService(
        store,
        QwenCloudEmbeddingClient(settings),
        build_memory_backend(settings),
        threshold=settings.memory_similarity_threshold,
        logger=logger,
    )


def _search_index_meta(sdk: Any, dimension: int) -> Any:
    return sdk.SearchIndexMeta(
        [
            sdk.FieldSchema(
                "embedding",
                sdk.FieldType.VECTOR,
                index=True,
                store=False,
                vector_options=sdk.VectorOptions(
                    sdk.VectorDataType.VD_FLOAT_32,
                    sdk.VectorMetricType.VM_COSINE,
                    dimension,
                ),
            ),
            sdk.FieldSchema(
                "incident_id",
                sdk.FieldType.KEYWORD,
                index=True,
                store=False,
            ),
            sdk.FieldSchema(
                "service",
                sdk.FieldType.KEYWORD,
                index=True,
                store=False,
                enable_sort_and_agg=True,
            ),
            sdk.FieldSchema("signal", sdk.FieldType.KEYWORD, index=True, store=False),
            sdk.FieldSchema("resolved_at", sdk.FieldType.KEYWORD, index=True, store=False),
            sdk.FieldSchema("summary", sdk.FieldType.TEXT, index=True, store=False),
            sdk.FieldSchema("resolution", sdk.FieldType.TEXT, index=True, store=False),
        ]
    )


def _validated_vector(value: Any, dimension: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MemoryProviderError("Embedding vector is invalid")
    if len(value) != dimension:
        raise MemoryProviderError("Embedding vector dimension is invalid")
    vector: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise MemoryProviderError("Embedding vector is invalid")
        number = float(item)
        if not math.isfinite(number) or abs(number) > FLOAT32_MAX:
            raise MemoryProviderError("Embedding vector is invalid")
        vector.append(number)
    if math.hypot(*vector) == 0.0:
        raise MemoryProviderError("Embedding vector norm is invalid")
    return tuple(vector)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _columns_to_mapping(columns: Sequence[Sequence[Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in columns:
        if len(column) >= 2 and isinstance(column[0], str):
            result[column[0]] = column[1]
    return result


def _error_code(error: Exception) -> str | None:
    getter = getattr(error, "get_error_code", None)
    if getter is not None:
        try:
            value = getter()
            return value if isinstance(value, str) else None
        except Exception:
            return None
    value = getattr(error, "code", None)
    return value if isinstance(value, str) else None


def _retryable_schema_error(error: Exception) -> bool:
    return (
        isinstance(error, (_SchemaNotReady, TimeoutError))
        or _error_code(error) in _RETRYABLE_SCHEMA_CODES
    )


def _already_exists_error(error: Exception) -> bool:
    return _error_code(error) in _ALREADY_EXISTS_CODES


def _verify_table_schema(response: Any) -> None:
    table_meta = getattr(response, "table_meta", None)
    table_options = getattr(response, "table_options", None)
    if (
        table_meta is None
        or list(getattr(table_meta, "schema_of_primary_key", ()))
        != [("id", "STRING")]
        or table_options is None
        or getattr(table_options, "time_to_live", None) != -1
        or getattr(table_options, "max_version", None) != 1
    ):
        raise MemoryConfigurationError(
            "Existing Tablestore table does not match ADR-004"
        )


def _bounded_keyword(value: str) -> str:
    """Bound a UTF-8 keyword without cutting a multi-byte code point."""

    encoded = value.strip().encode("utf-8")[:MAX_KEYWORD_UTF8_BYTES]
    while encoded:
        try:
            decoded = encoded.decode("utf-8")
            if decoded:
                return decoded
            break
        except UnicodeDecodeError as exc:
            encoded = encoded[: exc.start]
    return "unknown"


def _memory_incident_id(attribute: Any, primary_key: Any) -> str:
    if isinstance(attribute, str) and attribute.startswith("inc_"):
        return attribute
    if isinstance(primary_key, str) and primary_key.startswith("mem_inc_"):
        return primary_key.removeprefix("mem_")
    raise ValueError("memory incident identifier is invalid")


def _incident_query_text(incident: Incident) -> str:
    value = (
        f"service: {_bounded_keyword(incident.service)}\n"
        f"signal: {_bounded_keyword(incident.signal)}\n"
        f"severity: {incident.severity.value}\n"
        f"title: {incident.title[:MAX_MEMORY_SUMMARY_CHARS]}"
    )
    return value[:MAX_EMBEDDING_INPUT_CHARS]


def _resolved_memory(incident: Incident, plan: Any) -> IncidentMemory:
    actions: list[str] = []
    if plan is not None:
        actions = [f"{step.tool}: {step.action}" for step in plan.steps]
    resolution = (
        "Approved remediation completed: " + "; ".join(actions)
        if actions
        else "Approved remediation completed successfully."
    )
    service = _bounded_keyword(incident.service)
    signal = _bounded_keyword(incident.signal)
    summary = f"{service} {signal}: {incident.title}"
    return IncidentMemory(
        id=f"mem_{incident.id}",
        incident_id=incident.id,
        service=service,
        signal=signal,
        summary=summary[:MAX_MEMORY_SUMMARY_CHARS],
        resolution=resolution[:MAX_MEMORY_RESOLUTION_CHARS],
        tags=(service, signal, incident.severity.value),
        resolved_at=datetime.now(timezone.utc),
    )


def _memory_document_text(memory: IncidentMemory) -> str:
    value = (
        f"service: {memory.service}\n"
        f"signal: {memory.signal}\n"
        f"summary: {memory.summary}\n"
        f"resolution: {memory.resolution}"
    )
    return value[:MAX_EMBEDDING_INPUT_CHARS]
