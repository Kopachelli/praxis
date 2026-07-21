"""Deterministic, non-mutating adapters for risky remediation tools [FR-9]."""

from __future__ import annotations

from typing import Any

DRY_RUN_LABEL = "DRY RUN"
DRY_RUN_SOURCE = "dry_run_adapter"


def _result(tool: str, would_have: str) -> dict[str, Any]:
    """Return a fixed, trail-safe result without reflecting untrusted arguments."""

    return {
        "source": DRY_RUN_SOURCE,
        "dry_run": True,
        "label": DRY_RUN_LABEL,
        "tool": tool,
        "would_have": would_have,
    }


async def update_config_dry_run(*, key: str, value: Any) -> dict[str, Any]:
    """Describe a configuration update without exposing its key or value."""

    del key, value
    return _result(
        "update_config",
        "Updated one configuration value for the incident service.",
    )


async def scale_service_dry_run(
    *,
    service: str,
    replicas: int,
) -> dict[str, Any]:
    """Describe a replica change without contacting an external system."""

    del service, replicas
    return _result(
        "scale_service",
        "Scaled the incident service to the requested replica count.",
    )


async def rollback_deploy_dry_run(
    *,
    service: str,
    version: str,
) -> dict[str, Any]:
    """Describe a rollback without exposing its target version."""

    del service, version
    return _result(
        "rollback_deploy",
        "Rolled back the incident service to the requested version.",
    )
