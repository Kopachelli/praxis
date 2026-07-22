"""Praxis FastAPI entrypoint."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent.client import QwenClient
from app.agent.execution_runtime import ApprovedExecutionRunner
from app.agent.executor import PlanExecutor
from app.agent.memory import IncidentMemoryService, build_memory_service
from app.agent.runtime import (
    AgentTaskManager,
    LifecycleJobKind,
    LifecycleTaskManager,
    REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY,
)
from app.agent.tools.fc_restart import FunctionComputeRestartAdapter
from app.agent.tools.registry import ToolRegistry, build_tool_registry
from app.agent.triage import TriageAgent
from app.approval import build_approval_router
from app.body_limit import WebhookBodyLimitMiddleware
from app.config import Settings, get_settings
from app.demo_target import DEMO_TARGET_NAME
from app.incidents import IncidentStore
from app.logging_config import build_application_logger
from app.operator_auth import (
    build_operator_auth_dependency,
    build_reader_auth_dependency,
)
from app.webhook import build_webhook_router


logger = build_application_logger()
UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
_BODY_PARSE_ERROR_DETAIL = "There was an error parsing the body"


class LifecycleLimits(BaseModel):
    """The fixed ADR-024 process-admission and deadline constants."""

    max_running_jobs: int
    max_pending_jobs: int
    pending_timeout_seconds: float
    job_timeout_seconds: float


class SessionResponse(BaseModel):
    """ADR-029: the least-privilege role a presented bearer token authenticates."""

    role: str
    trace_id: str


class HealthResponse(BaseModel):
    ok: bool
    primary_model: str
    deployed_on: str
    version: str
    trace_id: str
    # Liveness (`ok`) is separate from real-execution readiness. These fields
    # report the ADR-024 lifecycle bounds and the ADR-028 real-dispatch guard so
    # readiness can never be inferred from a green liveness check alone.
    real_restart_adapter_configured: bool
    real_dispatch_timeout_reconciliation_ready: bool
    lifecycle: LifecycleLimits


def _validation_diagnostics(
    exc: RequestValidationError,
) -> list[dict[str, object]]:
    """Return only bounded, JSON-safe framework validation metadata."""

    diagnostics: list[dict[str, object]] = []
    for error in exc.errors():
        raw_location = error.get("loc", ())
        if not isinstance(raw_location, (list, tuple)):
            raw_location = ()
        diagnostics.append(
            {
                "type": _validation_text(error.get("type"), "validation_error"),
                "loc": [
                    _validation_location_part(part) for part in raw_location
                ],
                "msg": _validation_text(error.get("msg"), "Invalid input"),
            }
        )
    return diagnostics


def _validation_text(value: object, fallback: str) -> str:
    """Keep fixed diagnostic text while replacing non-scalar Unicode."""

    if not isinstance(value, str):
        return fallback
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _validation_location_part(value: object) -> str | int:
    """Allow only the JSON scalar types emitted for Pydantic locations."""

    if isinstance(value, str):
        return _validation_text(value, "<unknown>")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return "<unknown>"


def _build_runtime_tool_registry(settings: Settings) -> ToolRegistry:
    """Install ADR-010's real adapter and fail closed for final production."""

    has_target_url = bool(settings.demo_target_url)
    has_target_token = bool(settings.demo_target_token)
    if has_target_url != has_target_token:
        raise RuntimeError("Isolated restart adapter configuration is incomplete")

    is_production = settings.app_env in {"prod", "production"}
    if is_production and not has_target_url:
        raise RuntimeError(
            "Production requires the real isolated restart adapter"
        )

    if has_target_url:
        restart_adapter = FunctionComputeRestartAdapter(
            base_url=settings.demo_target_url,
            token=settings.demo_target_token,
        )
        # ADR-024/ADR-028: the real adapter is installed for the demo, but its
        # external boundary stays fail-closed until ADR-028 reconciliation is
        # accepted and implemented. Keying dispatch to the fixed readiness
        # constant means an approved plan cannot cross the real boundary now.
        registry = build_tool_registry(
            restart_handler=restart_adapter,
            restart_target=DEMO_TARGET_NAME,
            real_dispatch_enabled=REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY,
        )
    else:
        registry = build_tool_registry()

    if is_production and not registry.real_restart_configured:
        raise RuntimeError("Production real restart adapter is unavailable")
    return registry


_DEMO_SEED_PAYLOAD = {
    "source": "sentry",
    "title": "TimeoutError in checkout-service",
    "service": "checkout-service",
    "level": "error",
    "message": "Upstream payment gateway timed out after 30s",
    "extra": {"region": "eu-central", "occurrences": 47},
}


def _seed_demo_incident(
    store: IncidentStore,
    task_manager: AgentTaskManager,
) -> None:
    """Seed one real demo incident on startup so the public dashboard is never
    empty for reviewers. Off by default; fail-open — any error only logs and
    never blocks startup [ADR-031]."""

    try:
        if store.list_summaries():
            return
        from app.webhook import normalize_payload

        normalized = normalize_payload(_DEMO_SEED_PAYLOAD)
        incident, duplicate = store.create_or_get(
            source=normalized.source,
            raw_payload=_DEMO_SEED_PAYLOAD,
            service=normalized.service,
            severity=normalized.severity,
            signal=normalized.signal,
            title=normalized.title,
            idempotency_key="praxis-demo-seed-v1",
        )
        if not duplicate:
            task_manager.schedule(incident.id, "demo-seed")
            logger.info(
                "demo_incident_seeded",
                extra={"incident_id": incident.id, "trace_id": "demo-seed"},
            )
    except Exception as exc:
        logger.warning(
            "demo_seed_failed",
            extra={
                "incident_id": "-",
                "trace_id": "demo-seed",
                "error_type": type(exc).__name__,
            },
        )


def create_app(
    settings: Settings | None = None,
    incident_store: IncidentStore | None = None,
    *,
    agent_task_manager: AgentTaskManager | None = None,
    execution_task_manager: AgentTaskManager | None = None,
    lifecycle_task_manager: LifecycleTaskManager | None = None,
    qwen_client: QwenClient | None = None,
    memory_service: IncidentMemoryService | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_store = incident_store or IncidentStore(
        dedup_window_seconds=active_settings.dedup_window_seconds
    )
    if agent_task_manager is not None and qwen_client is not None:
        raise ValueError("agent_task_manager and qwen_client cannot both be supplied")
    active_memory_service = memory_service or build_memory_service(
        active_settings,
        active_store,
        logger,
    )

    tool_registry = _build_runtime_tool_registry(active_settings)
    active_lifecycle_task_manager = (
        lifecycle_task_manager or LifecycleTaskManager(active_store, logger)
    )

    active_qwen_client = qwen_client
    active_task_manager = agent_task_manager
    if active_task_manager is None:
        active_qwen_client = active_qwen_client or QwenClient(
            active_settings,
            trail=active_store.trail_store,
        )
        active_task_manager = AgentTaskManager(
            TriageAgent(
                active_store,
                active_qwen_client,
                registry=tool_registry,
                logger=logger,
                memory=active_memory_service,
            ),
            logger,
            lifecycle=active_lifecycle_task_manager,
            job_kind=LifecycleJobKind.INITIAL_TRIAGE,
        )
    active_execution_task_manager = execution_task_manager or AgentTaskManager(
        ApprovedExecutionRunner(
            active_store,
            PlanExecutor(tool_registry),
            logger,
            memory=active_memory_service,
        ),
        logger,
        lifecycle=active_lifecycle_task_manager,
        job_kind=LifecycleJobKind.APPROVED_EXECUTION,
    )
    if isinstance(active_task_manager, AgentTaskManager):
        active_task_manager.bind_lifecycle(
            active_lifecycle_task_manager,
            job_kind=LifecycleJobKind.INITIAL_TRIAGE,
        )
    if isinstance(active_execution_task_manager, AgentTaskManager):
        active_execution_task_manager.bind_lifecycle(
            active_lifecycle_task_manager,
            job_kind=LifecycleJobKind.APPROVED_EXECUTION,
        )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        if active_settings.seed_demo_incident:
            _seed_demo_incident(active_store, active_task_manager)
        try:
            yield
        finally:
            try:
                await active_lifecycle_task_manager.shutdown()
            finally:
                try:
                    if (
                        getattr(active_task_manager, "lifecycle", None)
                        is not active_lifecycle_task_manager
                    ):
                        await active_task_manager.shutdown()
                finally:
                    try:
                        if (
                            active_execution_task_manager is not active_task_manager
                            and getattr(
                                active_execution_task_manager,
                                "lifecycle",
                                None,
                            )
                            is not active_lifecycle_task_manager
                        ):
                            await active_execution_task_manager.shutdown()
                    finally:
                        try:
                            await active_memory_service.aclose()
                        finally:
                            if active_qwen_client is not None:
                                await active_qwen_client.aclose()

    application = FastAPI(
        title="Praxis",
        version=active_settings.app_version,
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.incident_store = active_store
    application.state.qwen_client = active_qwen_client
    application.state.agent_task_manager = active_task_manager
    application.state.execution_task_manager = active_execution_task_manager
    application.state.lifecycle_task_manager = active_lifecycle_task_manager
    application.state.memory_service = active_memory_service
    application.state.real_restart_adapter_configured = (
        tool_registry.real_restart_configured
    )
    application.state.real_dispatch_timeout_reconciliation_ready = (
        REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY
    )
    operator_auth = build_operator_auth_dependency(
        active_settings.operator_token,
        logger,
    )
    # ADR-029: reads also accept the read-only viewer token; mutations stay
    # operator-only, so a viewer token is rejected on webhook/approve.
    reader_auth = build_reader_auth_dependency(
        active_settings.operator_token,
        active_settings.viewer_token,
        logger,
        public_demo_reads=active_settings.public_demo_reads,
    )
    application.state.viewer_configured = bool(active_settings.viewer_token)
    application.state.public_demo_reads = active_settings.public_demo_reads

    # Registered first so the trace middleware declared below is outermost and
    # every size rejection receives the same server-owned trace contract.
    application.add_middleware(
        WebhookBodyLimitMiddleware,
        max_body_bytes=active_settings.max_webhook_body_bytes,
        max_approval_body_bytes=active_settings.max_approval_body_bytes,
        operator_token=active_settings.operator_token,
        logger=logger,
    )

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)
        status_code = (
            422
            if exc.status_code == 400 and exc.detail == _BODY_PARSE_ERROR_DETAIL
            else exc.status_code
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": jsonable_encoder(exc.detail), "trace_id": trace_id},
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)
        return JSONResponse(
            status_code=422,
            content={"detail": _validation_diagnostics(exc), "trace_id": trace_id},
        )

    @application.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)
        logger.error(
            "unhandled_request_error",
            extra={
                "incident_id": getattr(request.state, "incident_id", "-"),
                "trace_id": trace_id,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "trace_id": trace_id},
        )

    @application.middleware("http")
    async def attach_trace_context(request: Request, call_next):
        trace_id = uuid.uuid4().hex
        request.state.trace_id = trace_id
        request.state.incident_id = "-"
        logger.info(
            "request_started",
            extra={"incident_id": "-", "trace_id": trace_id},
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_failed",
                extra={
                    "incident_id": request.state.incident_id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "trace_id": trace_id},
            )
        response.headers["X-Trace-Id"] = trace_id
        logger.info(
            "request_finished",
            extra={
                "incident_id": request.state.incident_id,
                "trace_id": trace_id,
                "status_code": response.status_code,
            },
        )
        return response

    @application.get("/healthz", response_model=HealthResponse, tags=["operations"])
    async def healthz(request: Request) -> HealthResponse:
        """Return the deployment-proof health contract from FR-15 [ADR-024/028]."""

        return HealthResponse(
            ok=True,
            primary_model=active_settings.resolved_primary_model,
            deployed_on=active_settings.deployed_on,
            version=active_settings.app_version,
            trace_id=request.state.trace_id,
            real_restart_adapter_configured=tool_registry.real_restart_configured,
            real_dispatch_timeout_reconciliation_ready=(
                REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY
            ),
            lifecycle=LifecycleLimits(
                max_running_jobs=active_lifecycle_task_manager.max_running_jobs,
                max_pending_jobs=active_lifecycle_task_manager.max_pending_jobs,
                pending_timeout_seconds=(
                    active_lifecycle_task_manager.pending_timeout_seconds
                ),
                job_timeout_seconds=active_lifecycle_task_manager.job_timeout_seconds,
            ),
        )

    @application.get(
        "/session",
        response_model=SessionResponse,
        tags=["operations"],
        dependencies=[Depends(reader_auth)],
    )
    async def session(request: Request) -> SessionResponse:
        """Report the authenticated role so the UI can gate controls [ADR-029]."""

        return SessionResponse(
            role=getattr(request.state, "auth_role", "viewer"),
            trace_id=request.state.trace_id,
        )

    @application.get("/", include_in_schema=False)
    async def ui_index() -> HTMLResponse:
        """Serve the dependency-free operator timeline [FR-14].

        The ADR-031 public-demo-reads flag is stamped into the served HTML so the
        UI can auto-enter a read-only view when reads are open; the token-gated
        lock flow is unchanged when the flag is off.
        """

        html = UI_INDEX.read_text(encoding="utf-8").replace(
            "__PRAXIS_PUBLIC_DEMO_READS__",
            "true" if active_settings.public_demo_reads else "false",
        )
        return HTMLResponse(html)

    application.include_router(
        build_webhook_router(
            active_settings,
            active_store,
            logger,
            active_task_manager,
            reader_auth,
        )
    )
    application.include_router(
        build_approval_router(
            active_store,
            logger,
            active_task_manager,
            active_execution_task_manager,
            operator_auth,
        )
    )
    return application


app = create_app()
