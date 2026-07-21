"""Strict remediation-plan parsing and validation [FR-3, FR-6]."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

_REGISTERED_TOOLS_CONTEXT_KEY = "registered_tools"
_KNOWN_LOCATION_PARTS = frozenset(
    {
        "steps",
        "seq",
        "action",
        "tool",
        "args",
        "risk_level",
        "rollback",
        # Public argument names from the fixed initial tool contract. Unknown
        # keys remain redacted as <untrusted-field> in retry diagnostics.
        "service",
        "key",
        "value",
        "replicas",
        "version",
    }
)

NonEmptyText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
ToolName = Annotated[str, StringConstraints(strict=True, min_length=1)]
PositiveSequence = Annotated[int, Field(strict=True, ge=1)]
SafeDiagnostic = dict[str, str | tuple[str | int, ...]]


class RiskLevel(str, Enum):
    """The three execution-risk levels fixed by the API contract."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


class RemediationStep(BaseModel):
    """One exact, registry-bound remediation step."""

    model_config = ConfigDict(extra="forbid", strict=True)

    seq: PositiveSequence
    action: NonEmptyText
    tool: ToolName
    args: dict[str, Any]
    risk_level: RiskLevel
    rollback: NonEmptyText

    @field_validator("tool")
    @classmethod
    def require_registered_tool(cls, value: str, info: ValidationInfo) -> str:
        """Reject tool names that are absent from caller-supplied registry context."""

        context = info.context
        if not isinstance(context, Mapping):
            raise ValueError("registered tool context is required")
        registered_tools = context.get(_REGISTERED_TOOLS_CONTEXT_KEY)
        if not _is_registered_tool_collection(registered_tools):
            raise ValueError("registered tool context is invalid")
        if value not in registered_tools:
            raise ValueError("tool is not registered")
        return value


class RemediationPlan(BaseModel):
    """The strict JSON root returned by the Qwen planning call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    steps: list[RemediationStep] = Field(min_length=1)

    @model_validator(mode="after")
    def require_contiguous_sequence(self) -> "RemediationPlan":
        expected = list(range(1, len(self.steps) + 1))
        if [step.seq for step in self.steps] != expected:
            raise ValueError("step seq values must be ordered and contiguous from 1")
        return self


class RemediationPlanValidationError(ValueError):
    """A redacted validation failure safe to use in a model retry prompt."""

    def __init__(self, diagnostics: tuple[SafeDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        rendered = "; ".join(
            f"{_format_location(item['loc'])}: {item['msg']}"
            for item in diagnostics
        )
        super().__init__(f"Remediation plan validation failed: {rendered}")


def parse_remediation_plan_json(
    payload: str | bytes | bytearray,
    *,
    registered_tools: Collection[str],
) -> RemediationPlan:
    """Parse strict JSON with the current registered tool names as context."""

    if not isinstance(payload, (str, bytes, bytearray)):
        raise TypeError("payload must be JSON text or bytes")
    tool_names = _validated_registered_tools(registered_tools)

    diagnostics: tuple[SafeDiagnostic, ...] | None = None
    try:
        return RemediationPlan.model_validate_json(
            payload,
            strict=True,
            context={_REGISTERED_TOOLS_CONTEXT_KEY: tool_names},
        )
    except ValidationError as error:
        diagnostics = safe_validation_diagnostics(error)

    # Raise outside the exception handler so the original ValidationError, which
    # retains the model input, is not attached as exception context.
    raise RemediationPlanValidationError(diagnostics)


def safe_validation_diagnostics(
    error: ValidationError,
) -> tuple[SafeDiagnostic, ...]:
    """Return retry-ready errors without input, URL, context, or untrusted keys."""

    diagnostics: list[SafeDiagnostic] = []
    for item in error.errors(
        include_input=False,
        include_url=False,
        include_context=False,
    ):
        diagnostics.append(
            {
                "type": str(item["type"]),
                "loc": _safe_location(tuple(item["loc"])),
                "msg": str(item["msg"]),
            }
        )
    return tuple(diagnostics)


def _validated_registered_tools(
    registered_tools: Collection[str],
) -> frozenset[str]:
    if not _is_registered_tool_collection(registered_tools):
        raise TypeError("registered_tools must be a collection of non-empty names")
    return frozenset(registered_tools)


def _is_registered_tool_collection(value: object) -> bool:
    return (
        isinstance(value, Collection)
        and not isinstance(value, (str, bytes, bytearray))
        and all(
            isinstance(item, str) and bool(item) and item.strip() == item
            for item in value
        )
    )


def _safe_location(location: tuple[object, ...]) -> tuple[str | int, ...]:
    return tuple(
        part
        if isinstance(part, int)
        or (isinstance(part, str) and part in _KNOWN_LOCATION_PARTS)
        else "<untrusted-field>"
        for part in location
    )


def _format_location(location: str | tuple[str | int, ...]) -> str:
    if not isinstance(location, tuple) or not location:
        return "plan"
    return ".".join(str(part) for part in location)
