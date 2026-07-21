"""Strict, planning-safe registry for Qwen native function calls [FR-5]."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.agent.plans import (
    RemediationPlan,
    RemediationPlanValidationError,
    RiskLevel,
    SafeDiagnostic,
    safe_validation_diagnostics,
)
from app.agent.tools.dryrun import (
    rollback_deploy_dry_run,
    scale_service_dry_run,
    update_config_dry_run,
)
from app.agent.tools.real import fetch_logs, service_status
from app.redaction import redact_structure

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RESTART_BOOT_ID = re.compile(r"^[0-9a-f]{32}$")
_DEMO_TARGET_NAME = "praxis-demo-target"


class ToolRegistryError(RuntimeError):
    """Base class for intentionally secret-safe registry failures."""


class UnknownToolError(ToolRegistryError):
    """The model requested a tool outside the registered allowlist."""


class ToolArgumentError(ToolRegistryError):
    """Tool-call arguments were malformed or failed strict validation."""


class ToolPolicyError(ToolRegistryError):
    """The requested call violates the planning/HITL safety boundary."""


class ToolUnavailableError(ToolRegistryError):
    """A registered write tool has no accepted implementation yet."""


class ToolExecutionError(ToolRegistryError):
    """A registered handler failed; its raw exception is deliberately hidden."""


class ToolKind(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolExecutionMode(str, Enum):
    PLANNING = "planning"
    APPROVED_EXECUTION = "approved_execution"


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ServiceArguments(_StrictArguments):
    service: str = Field(min_length=1, max_length=128, pattern=_SERVICE_NAME.pattern)


class _UpdateConfigArguments(_StrictArguments):
    key: str = Field(min_length=1, max_length=128)
    value: Any


class _ScaleServiceArguments(_ServiceArguments):
    replicas: int = Field(ge=0, le=100)


class _RollbackDeployArguments(_ServiceArguments):
    version: str = Field(min_length=1, max_length=128)


class _FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME.pattern)
    arguments: str | dict[str, Any]


class _ToolCallEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str | None = Field(default=None, min_length=1, max_length=256)
    index: StrictInt | None = Field(default=None, ge=0)
    type: Literal["function"] = "function"
    function: _FunctionCall


@dataclass(frozen=True, slots=True)
class IncidentToolContext:
    """Secret-safe boundary passed to real incident-context read tools."""

    incident_id: str
    source: str
    service: str
    severity: str
    signal: str
    title: str
    raw_payload: Any = field(repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "incident_id",
            "source",
            "service",
            "severity",
            "signal",
            "title",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        object.__setattr__(self, "raw_payload", copy.deepcopy(self.raw_payload))

    @classmethod
    def from_incident(cls, incident: Any) -> "IncidentToolContext":
        """Copy the fields tools may inspect from an Incident-like object."""

        severity = getattr(incident, "severity")
        severity_value = getattr(severity, "value", severity)
        return cls(
            incident_id=getattr(incident, "id"),
            source=getattr(incident, "source"),
            service=getattr(incident, "service"),
            severity=str(severity_value),
            signal=getattr(incident, "signal"),
            title=getattr(incident, "title"),
            raw_payload=getattr(incident, "raw_payload"),
        )


class ToolCallResult(BaseModel):
    """JSON-serializable result with explicit provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str | None = None
    name: str
    source: str
    output: dict[str, Any]

    def as_tool_message(self) -> dict[str, str]:
        """Render the validated tool result for the next Qwen turn."""

        message = {
            "role": "tool",
            "name": self.name,
            "content": json.dumps(
                self.output,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
        }
        if self.call_id is not None:
            message["tool_call_id"] = self.call_id
        return message


ToolHandler = Callable[
    [IncidentToolContext, BaseModel],
    Awaitable[dict[str, Any]],
]
RestartServiceHandler = Callable[
    [IncidentToolContext, str],
    Awaitable[Mapping[str, Any]],
]
ExternalDispatchCallback = Callable[[], None]
# ADR-028: called by the real adapter with the exact pre-action boot-id baseline,
# immediately before the external boundary is crossed, to durably record intent.
RestartIntentCallback = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class _ToolDefinition:
    name: str
    description: str
    kind: ToolKind
    risk: str | None
    arguments_model: type[BaseModel]
    handler: ToolHandler | None = field(default=None, repr=False)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    call_id: str | None
    definition: _ToolDefinition
    arguments: BaseModel


async def _fetch_logs_handler(
    context: IncidentToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    validated = _ServiceArguments.model_validate(arguments)
    return await fetch_logs(context, service=validated.service)


async def _service_status_handler(
    context: IncidentToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    validated = _ServiceArguments.model_validate(arguments)
    return await service_status(context, service=validated.service)


async def _update_config_dry_run_handler(
    context: IncidentToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    del context
    validated = _UpdateConfigArguments.model_validate(arguments)
    return await update_config_dry_run(key=validated.key, value=validated.value)


async def _scale_service_dry_run_handler(
    context: IncidentToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    del context
    validated = _ScaleServiceArguments.model_validate(arguments)
    return await scale_service_dry_run(
        service=validated.service,
        replicas=validated.replicas,
    )


async def _rollback_deploy_dry_run_handler(
    context: IncidentToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    del context
    validated = _RollbackDeployArguments.model_validate(arguments)
    return await rollback_deploy_dry_run(
        service=validated.service,
        version=validated.version,
    )


_INITIAL_TOOLS = (
    _ToolDefinition(
        name="fetch_logs",
        description="Read bounded, redacted alert log evidence for a service.",
        kind=ToolKind.READ,
        risk=None,
        arguments_model=_ServiceArguments,
        handler=_fetch_logs_handler,
    ),
    _ToolDefinition(
        name="service_status",
        description="Read the service status observed in the current alert.",
        kind=ToolKind.READ,
        risk=None,
        arguments_model=_ServiceArguments,
        handler=_service_status_handler,
    ),
    _ToolDefinition(
        name="restart_service",
        description="Restart the approved isolated demo service.",
        kind=ToolKind.WRITE,
        risk="safe",
        arguments_model=_ServiceArguments,
    ),
    _ToolDefinition(
        name="update_config",
        description="Propose a dry-run configuration update.",
        kind=ToolKind.WRITE,
        risk="caution",
        arguments_model=_UpdateConfigArguments,
        handler=_update_config_dry_run_handler,
    ),
    _ToolDefinition(
        name="scale_service",
        description="Propose a dry-run service replica change.",
        kind=ToolKind.WRITE,
        risk="caution",
        arguments_model=_ScaleServiceArguments,
        handler=_scale_service_dry_run_handler,
    ),
    _ToolDefinition(
        name="rollback_deploy",
        description="Propose a dry-run rollback to a named version.",
        kind=ToolKind.WRITE,
        risk="dangerous",
        arguments_model=_RollbackDeployArguments,
        handler=_rollback_deploy_dry_run_handler,
    ),
)


class ToolRegistry:
    """Allowlist, validate, and execute registered tool calls in order."""

    def __init__(
        self,
        definitions: Sequence[_ToolDefinition] = _INITIAL_TOOLS,
        *,
        real_dispatch_enabled: bool = False,
    ) -> None:
        if not isinstance(real_dispatch_enabled, bool):
            raise TypeError("real_dispatch_enabled must be a bool")
        self._definitions: dict[str, _ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"duplicate tool registration: {definition.name}")
            self._definitions[definition.name] = definition
        self._real_dispatch_enabled = real_dispatch_enabled

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @property
    def real_restart_configured(self) -> bool:
        """Return secret-free evidence that ADR-010's real handler is installed."""

        definition = self._definitions.get("restart_service")
        return bool(
            definition is not None
            and definition.kind is ToolKind.WRITE
            and definition.risk == "safe"
            and definition.handler is not None
        )

    @property
    def real_dispatch_enabled(self) -> bool:
        """Whether the installed real adapter may perform its external call.

        Fail-closed by default. ADR-024 forbids real-execution readiness until
        ADR-028's uncertain-outcome reconciliation is accepted and implemented,
        so production wires this to REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY.
        An installed adapter (real_restart_configured) is not permission to
        dispatch; both must be true before any real external boundary is crossed.
        """

        return self._real_dispatch_enabled

    def planning_schemas(self) -> list[dict[str, Any]]:
        """Return only non-mutating schemas safe for a planning model turn."""

        return [
            copy.deepcopy(definition.schema())
            for definition in self._definitions.values()
            if definition.kind is ToolKind.READ
        ]

    def registered_schemas(self) -> list[dict[str, Any]]:
        """Describe the full contract; callers must not send these for planning."""

        return [
            copy.deepcopy(definition.schema())
            for definition in self._definitions.values()
        ]

    def validate_remediation_plan(
        self,
        plan: RemediationPlan,
    ) -> RemediationPlan:
        """Validate plan arguments and risk policy without executing any tool.

        Structural JSON validation deliberately remains in ``plans.py``. This
        second, synchronous pass binds every structurally valid step to the exact
        strict argument model and risk default of this registry. Diagnostics use
        the existing retry-safe plan exception so callers can re-prompt Qwen
        without exposing argument values or untrusted field names.
        """

        if not isinstance(plan, RemediationPlan):
            raise TypeError("plan must be a validated RemediationPlan")

        diagnostics: list[SafeDiagnostic] = []
        has_registered_write_step = False

        for index, step in enumerate(plan.steps):
            definition = self._definitions.get(step.tool)
            if definition is None:
                diagnostics.append(
                    {
                        "type": "unknown_tool",
                        "loc": ("steps", index, "tool"),
                        "msg": "Tool is not registered in the active registry",
                    }
                )
                continue

            try:
                definition.arguments_model.model_validate(step.args, strict=True)
            except ValidationError as error:
                for item in safe_validation_diagnostics(error):
                    location = item["loc"]
                    diagnostics.append(
                        {
                            "type": str(item["type"]),
                            "loc": (
                                "steps",
                                index,
                                "args",
                                *(location if isinstance(location, tuple) else ()),
                            ),
                            "msg": str(item["msg"]),
                        }
                    )

            if definition.kind is ToolKind.READ:
                if step.risk_level is not RiskLevel.SAFE:
                    diagnostics.append(
                        {
                            "type": "risk_policy",
                            "loc": ("steps", index, "risk_level"),
                            "msg": (
                                f"Read tool {definition.name} must use risk level safe"
                            ),
                        }
                    )
                continue

            has_registered_write_step = True
            expected_risk = definition.risk
            if expected_risk is None or step.risk_level.value != expected_risk:
                diagnostics.append(
                    {
                        "type": "risk_policy",
                        "loc": ("steps", index, "risk_level"),
                        "msg": (
                            f"Write tool {definition.name} must use its registered "
                            f"risk level {expected_risk or '<invalid-registration>'}"
                        ),
                    }
                )

        if not has_registered_write_step:
            diagnostics.append(
                {
                    "type": "missing_write_step",
                    "loc": ("steps",),
                    "msg": (
                        "Plan must include at least one registered write "
                        "remediation step"
                    ),
                }
            )

        if diagnostics:
            # This raise is outside every ValidationError handler so no exception
            # retaining model input can become context on the retry-safe error.
            raise RemediationPlanValidationError(tuple(diagnostics))
        return plan

    async def execute(
        self,
        name: str,
        arguments: str | Mapping[str, Any],
        *,
        context: IncidentToolContext,
        call_id: str | None = None,
        mode: ToolExecutionMode = ToolExecutionMode.PLANNING,
        on_external_dispatch: ExternalDispatchCallback | None = None,
        record_intent: RestartIntentCallback | None = None,
    ) -> ToolCallResult:
        """Validate and execute one call through the same batch-safe path."""

        if on_external_dispatch is not None and not callable(on_external_dispatch):
            raise TypeError("on_external_dispatch must be callable")
        if record_intent is not None and not callable(record_intent):
            raise TypeError("record_intent must be callable")
        prepared = self._prepare(name, arguments, call_id=call_id, mode=mode)
        return await self._execute_prepared(
            prepared,
            context=context,
            on_external_dispatch=on_external_dispatch,
            record_intent=record_intent,
        )

    async def execute_tool_calls(
        self,
        tool_calls: Sequence[Mapping[str, Any]],
        *,
        context: IncidentToolContext,
        mode: ToolExecutionMode = ToolExecutionMode.PLANNING,
    ) -> list[ToolCallResult]:
        """Preflight every Qwen tool call, then execute all in supplied order."""

        if isinstance(tool_calls, (str, bytes)) or not isinstance(tool_calls, Sequence):
            raise ToolArgumentError("tool_calls must be a list")

        prepared: list[_PreparedCall] = []
        for raw_call in tool_calls:
            try:
                envelope = _ToolCallEnvelope.model_validate(raw_call, strict=True)
            except (TypeError, ValidationError):
                raise ToolArgumentError("malformed tool-call envelope") from None
            prepared.append(
                self._prepare(
                    envelope.function.name,
                    envelope.function.arguments,
                    call_id=envelope.id,
                    mode=mode,
                )
            )

        # Context policy is part of batch preflight. Validate every prepared
        # call before the first handler runs so a later out-of-scope call cannot
        # erase evidence for an earlier call that already executed.
        for item in prepared:
            self._validate_prepared_context(item, context=context)

        results: list[ToolCallResult] = []
        for item in prepared:
            results.append(await self._execute_prepared(item, context=context))
        return results

    def _prepare(
        self,
        name: str,
        arguments: str | Mapping[str, Any],
        *,
        call_id: str | None,
        mode: ToolExecutionMode,
    ) -> _PreparedCall:
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise UnknownToolError("unknown registered tool")
        definition = self._definitions.get(name)
        if definition is None:
            raise UnknownToolError(f"unknown registered tool: {name}")
        if not isinstance(mode, ToolExecutionMode):
            raise ToolPolicyError("invalid tool execution mode")
        if definition.kind is ToolKind.WRITE and mode is ToolExecutionMode.PLANNING:
            raise ToolPolicyError(
                f"state-changing tool blocked during planning: {definition.name}"
            )
        if definition.handler is None:
            raise ToolUnavailableError(
                f"registered tool is not executable in this milestone: {definition.name}"
            )

        payload = self._parse_arguments(arguments)
        try:
            validated = definition.arguments_model.model_validate(payload, strict=True)
        except ValidationError:
            raise ToolArgumentError(
                f"invalid arguments for registered tool: {definition.name}"
            ) from None
        return _PreparedCall(
            call_id=call_id,
            definition=definition,
            arguments=validated,
        )

    @staticmethod
    def _parse_arguments(arguments: str | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(arguments, str):
            try:
                payload = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError):
                raise ToolArgumentError("tool arguments must be a JSON object") from None
        elif isinstance(arguments, Mapping):
            payload = dict(arguments)
        else:
            raise ToolArgumentError("tool arguments must be a JSON object")
        if not isinstance(payload, dict):
            raise ToolArgumentError("tool arguments must be a JSON object")
        return payload

    async def _execute_prepared(
        self,
        prepared: _PreparedCall,
        *,
        context: IncidentToolContext,
        on_external_dispatch: ExternalDispatchCallback | None = None,
        record_intent: RestartIntentCallback | None = None,
    ) -> ToolCallResult:
        self._validate_prepared_context(prepared, context=context)

        handler = prepared.definition.handler
        if handler is None:  # Defensive: _prepare already rejects unavailable tools.
            raise ToolUnavailableError("registered tool is not executable")
        is_real_external = self._is_real_external(prepared)
        if is_real_external:
            if not self._real_dispatch_enabled:
                # ADR-024/ADR-028: never cross a real external boundary while the
                # uncertain-outcome reconciliation is disabled. Fail closed before
                # the dispatch marker and before the adapter runs.
                raise ToolPolicyError(
                    "real external dispatch is blocked pending ADR-028 reconciliation"
                )
            if on_external_dispatch is not None:
                outcome = on_external_dispatch()
                if outcome is not None:
                    raise TypeError("on_external_dispatch must return None")
        try:
            if is_real_external:
                # ADR-028: the real adapter records durable intent (with baseline)
                # before its external boundary via record_intent.
                output = await handler(
                    context,
                    prepared.arguments,
                    record_intent=record_intent,
                )
            else:
                output = await handler(context, prepared.arguments)
        except Exception:
            raise ToolExecutionError("registered tool execution failed") from None
        serializable = redact_structure(_json_round_trip(output))
        if not isinstance(serializable, dict):
            raise ToolRegistryError("tool result must remain a JSON object")
        source = serializable.get("source")
        if not isinstance(source, str) or not source:
            raise ToolRegistryError("tool result is missing its source label")
        return ToolCallResult(
            call_id=prepared.call_id,
            name=prepared.definition.name,
            source=source,
            output=serializable,
        )

    @staticmethod
    def _is_real_external(prepared: _PreparedCall) -> bool:
        """Identify the one accepted real state-changing adapter boundary."""

        definition = prepared.definition
        return bool(
            definition.name == "restart_service"
            and definition.kind is ToolKind.WRITE
            and definition.risk == "safe"
            and definition.handler is not None
        )

    @staticmethod
    def _validate_prepared_context(
        prepared: _PreparedCall,
        *,
        context: IncidentToolContext,
    ) -> None:
        service = getattr(prepared.arguments, "service", context.service)
        if service != context.service:
            raise ToolPolicyError("tool service is outside the incident context")


def _json_round_trip(value: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise ToolRegistryError("tool result is not JSON-serializable") from None
    if not isinstance(decoded, dict):
        raise ToolRegistryError("tool result must be a JSON object")
    return decoded


def build_tool_registry(
    *,
    restart_handler: RestartServiceHandler | None = None,
    restart_target: str | None = None,
    real_dispatch_enabled: bool = False,
) -> ToolRegistry:
    """Build the fixed allowlist, optionally injecting the one real adapter.

    The adapter never receives the model/operator-supplied service as its cloud
    target. It receives only ``restart_target``, which is validated once here and
    captured in the handler closure. Both values are required together so a
    partial configuration fails closed.

    ``real_dispatch_enabled`` is fail-closed by default (ADR-024/ADR-028): even
    when the real adapter is installed, the registry refuses to cross its
    external boundary unless the caller explicitly permits it. Production keys
    this to REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY.
    """

    if (restart_handler is None) != (restart_target is None):
        raise ValueError("restart_handler and restart_target must be configured together")
    if restart_handler is None:
        return ToolRegistry(real_dispatch_enabled=real_dispatch_enabled)
    if not callable(restart_handler):
        raise TypeError("restart_handler must be callable")
    if restart_target != _DEMO_TARGET_NAME:
        raise ValueError("restart_target must be the exact isolated demo target")

    async def restart_service_handler(
        context: IncidentToolContext,
        arguments: BaseModel,
        *,
        record_intent: RestartIntentCallback | None = None,
    ) -> dict[str, Any]:
        validated = _ServiceArguments.model_validate(arguments)
        # The logical service remains incident-bound by _execute_prepared. The
        # injected cloud operation receives only this trusted, configured target.
        del validated
        if record_intent is not None:
            proof = await restart_handler(
                context, restart_target, record_intent=record_intent
            )
        else:
            proof = await restart_handler(context, restart_target)
        expected_keys = {
            "source",
            "dry_run",
            "target",
            "status",
            "previous_boot_id",
            "current_boot_id",
        }
        if not isinstance(proof, Mapping) or set(proof) != expected_keys:
            raise ToolPolicyError("restart adapter returned an invalid proof")
        previous_boot_id = proof.get("previous_boot_id")
        current_boot_id = proof.get("current_boot_id")
        if (
            proof.get("source") != "alibaba_function_compute"
            or proof.get("dry_run") is not False
            or proof.get("target") != _DEMO_TARGET_NAME
            or proof.get("status") != "restarted"
            or not isinstance(previous_boot_id, str)
            or _RESTART_BOOT_ID.fullmatch(previous_boot_id) is None
            or not isinstance(current_boot_id, str)
            or _RESTART_BOOT_ID.fullmatch(current_boot_id) is None
            or previous_boot_id == current_boot_id
        ):
            raise ToolPolicyError("restart adapter returned an invalid proof")
        return {
            "source": "alibaba_function_compute",
            "dry_run": False,
            "label": "REAL ACTION",
            "tool": "restart_service",
            "target": _DEMO_TARGET_NAME,
            "status": "restarted",
            "previous_boot_id": previous_boot_id,
            "current_boot_id": current_boot_id,
        }

    definitions = tuple(
        _ToolDefinition(
            name=definition.name,
            description=definition.description,
            kind=definition.kind,
            risk=definition.risk,
            arguments_model=definition.arguments_model,
            handler=(
                restart_service_handler
                if definition.name == "restart_service"
                else definition.handler
            ),
        )
        for definition in _INITIAL_TOOLS
    )
    return ToolRegistry(definitions, real_dispatch_enabled=real_dispatch_enabled)
