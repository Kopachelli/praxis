"""Approved real-adapter and deterministic dry-run tests [FR-8, FR-9]."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agent.tools.dryrun import DRY_RUN_LABEL, DRY_RUN_SOURCE
from app.agent.tools.registry import (
    IncidentToolContext,
    ToolExecutionMode,
    ToolExecutionError,
    ToolPolicyError,
    build_tool_registry,
)

_OLD_BOOT_ID = "a" * 32
_NEW_BOOT_ID = "b" * 32


def _restart_proof() -> dict[str, Any]:
    return {
        "source": "alibaba_function_compute",
        "dry_run": False,
        "target": "praxis-demo-target",
        "status": "restarted",
        "previous_boot_id": _OLD_BOOT_ID,
        "current_boot_id": _NEW_BOOT_ID,
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _context() -> IncidentToolContext:
    return IncidentToolContext(
        incident_id="inc-1",
        source="sentry",
        service="checkout-service",
        severity="high",
        signal="upstream_timeout",
        title="TimeoutError in checkout-service",
        raw_payload={},
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "update_config",
            {"key": "api_key", "value": "config-secret-sentinel"},
        ),
        (
            "scale_service",
            {"service": "checkout-service", "replicas": 3},
        ),
        (
            "rollback_deploy",
            {
                "service": "checkout-service",
                "version": "version-secret-sentinel",
            },
        ),
    ],
)
def test_risky_tools_are_clearly_labeled_deterministic_dry_runs(
    tool: str,
    arguments: dict[str, Any],
) -> None:
    registry = build_tool_registry()

    first = _run(
        registry.execute(
            tool,
            arguments,
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
        )
    )
    second = _run(
        registry.execute(
            tool,
            arguments,
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
        )
    )
    rendered = json.dumps(first.output)

    assert first == second
    assert first.source == DRY_RUN_SOURCE
    assert first.output["dry_run"] is True
    assert first.output["label"] == DRY_RUN_LABEL
    assert first.output["tool"] == tool
    assert first.output["would_have"]
    assert "config-secret-sentinel" not in rendered
    assert "version-secret-sentinel" not in rendered


@pytest.mark.parametrize(
    ("handler", "target"),
    [
        (None, "praxis-demo-target"),
        (lambda context, service: None, None),
    ],
)
def test_real_restart_configuration_fails_closed_when_partial(
    handler: Any,
    target: str | None,
) -> None:
    with pytest.raises(ValueError, match="configured together"):
        build_tool_registry(restart_handler=handler, restart_target=target)


def test_real_restart_uses_only_the_injected_allowlisted_target() -> None:
    calls: list[tuple[str, str]] = []

    async def restart(context: IncidentToolContext, target: str) -> dict[str, Any]:
        calls.append((context.service, target))
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )

    result = _run(
        registry.execute(
            "restart_service",
            {"service": "checkout-service"},
            context=_context(),
            mode=ToolExecutionMode.APPROVED_EXECUTION,
        )
    )
    rendered = json.dumps(result.output)

    assert calls == [("checkout-service", "praxis-demo-target")]
    assert result.source == "alibaba_function_compute"
    assert result.output == {
        "source": "alibaba_function_compute",
        "dry_run": False,
        "label": "REAL ACTION",
        "tool": "restart_service",
        "target": "praxis-demo-target",
        "status": "restarted",
        "previous_boot_id": _OLD_BOOT_ID,
        "current_boot_id": _NEW_BOOT_ID,
    }
    assert "secret" not in rendered


def test_real_restart_is_blocked_before_dispatch_when_reconciliation_unready() -> None:
    """PRAXIS-146: the installed real adapter must not cross its external
    boundary while ADR-028 reconciliation is unimplemented [ADR-024, ADR-028].

    The adapter stays configured, but a default (fail-closed) registry refuses
    the real dispatch before the handler runs, so no external call ever starts.
    """

    calls: list[str] = []

    async def restart(context: IncidentToolContext, target: str) -> dict[str, Any]:
        calls.append(target)
        return _restart_proof()

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
    )

    assert registry.real_restart_configured is True
    assert registry.real_dispatch_enabled is False

    with pytest.raises(ToolPolicyError, match="pending ADR-028"):
        _run(
            registry.execute(
                "restart_service",
                {"service": "checkout-service"},
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
            )
        )

    assert calls == []


def test_real_restart_rejects_malformed_adapter_proof() -> None:
    async def restart(
        context: IncidentToolContext,
        target: str,
    ) -> dict[str, Any]:
        del context, target
        return {**_restart_proof(), "status": "restart_accepted"}

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )

    with pytest.raises(ToolExecutionError, match="execution failed"):
        _run(
            registry.execute(
                "restart_service",
                {"service": "checkout-service"},
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
            )
        )


def test_real_restart_rejects_non_demo_configured_target() -> None:
    async def restart(
        context: IncidentToolContext,
        target: str,
    ) -> dict[str, Any]:
        del context, target
        return _restart_proof()

    with pytest.raises(ValueError, match="exact isolated demo target"):
        build_tool_registry(
            restart_handler=restart,
            restart_target="production-database",
        )


def test_restart_rejects_a_model_supplied_service_outside_incident_context() -> None:
    calls: list[str] = []

    async def restart(context: IncidentToolContext, target: str) -> None:
        del context
        calls.append(target)

    registry = build_tool_registry(
        restart_handler=restart,
        restart_target="praxis-demo-target",
        real_dispatch_enabled=True,
    )

    with pytest.raises(ToolPolicyError, match="outside the incident context"):
        _run(
            registry.execute(
                "restart_service",
                {"service": "attacker-selected-service"},
                context=_context(),
                mode=ToolExecutionMode.APPROVED_EXECUTION,
            )
        )

    assert calls == []
