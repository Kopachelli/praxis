"""Read the live Praxis FC controller through a credential-safe output envelope."""

from __future__ import annotations

import hmac
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.fc_role import (  # noqa: E402
    Credentials,
    REGION,
    _environment,
    _safe_execute,
)

FUNCTION_NAME = "praxis-api"
TARGET_FUNCTION_NAME = "praxis-demo-target"
ACCEPTED_FUNCTION_STATES = frozenset({"Active"})
ACCEPTED_LAST_UPDATE_STATUSES = frozenset({"Successful"})
_SAFE_STATUS = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_MAX_LAST_MODIFIED_TIME_CHARS = 35
_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _safe_last_modified_time(value: Any) -> str | None:
    """Return one bounded RFC 3339 timestamp or redact it completely."""

    if (
        not isinstance(value, str)
        or len(value) > _MAX_LAST_MODIFIED_TIME_CHARS
        or _RFC3339_TIMESTAMP.fullmatch(value) is None
    ):
        return None
    try:
        datetime.strptime(
            f"{value[:10]}T{value[11:19]}",
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError:
        return None
    if value[-1] not in "Zz":
        offset_hour = int(value[-5:-3])
        offset_minute = int(value[-2:])
        if offset_hour > 23 or offset_minute > 59:
            return None
    return value


def _require_controller_adapter_environment(
    environment: Mapping[str, str],
) -> Mapping[str, str]:
    """Fail locally before probing a controller that cannot satisfy ADR-010."""

    required = (
        "FC_EXECUTION_ROLE_ARN",
        "PRAXIS_OPERATOR_TOKEN",
        "PRAXIS_DEMO_TARGET_URL",
        "PRAXIS_DEMO_TARGET_TOKEN",
        "TABLESTORE_ENDPOINT",
        "TABLESTORE_INSTANCE",
    )
    missing = [name for name in required if not environment.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required final controller variables: " + ", ".join(missing)
        )
    return environment


def _summary(body: Any, expected: Mapping[str, str]) -> dict[str, Any]:
    environment = getattr(body, "environment_variables", None)
    if not isinstance(environment, Mapping):
        environment = {}
    status = getattr(body, "last_update_status", None)
    state = getattr(body, "state", None)
    reason_code = getattr(body, "last_update_status_reason_code", None)
    last_modified_time = _safe_last_modified_time(
        getattr(body, "last_modified_time", None)
    )
    safe_status = (
        status
        if isinstance(status, str) and _SAFE_STATUS.fullmatch(status)
        else None
    )
    safe_state = (
        state if isinstance(state, str) and _SAFE_STATUS.fullmatch(state) else None
    )
    status_reported = status is not None
    state_reported = state is not None
    expected_target_url = (
        expected.get("PRAXIS_DEMO_TARGET_URL", "").strip().rstrip("/")
    )
    actual_target_url = (
        str(environment.get("PRAXIS_DEMO_TARGET_URL", "")).strip().rstrip("/")
    )
    expected_target_token = expected.get("PRAXIS_DEMO_TARGET_TOKEN", "")
    actual_target_token = environment.get("PRAXIS_DEMO_TARGET_TOKEN", "")
    expected_operator_token = expected.get("PRAXIS_OPERATOR_TOKEN", "")
    actual_operator_token = environment.get("PRAXIS_OPERATOR_TOKEN", "")
    expected_role = expected.get("FC_EXECUTION_ROLE_ARN", "").strip()
    actual_role = getattr(body, "role", None)
    expected_tablestore_endpoint = expected.get("TABLESTORE_ENDPOINT", "").strip()
    actual_tablestore_endpoint = environment.get("TABLESTORE_ENDPOINT")
    expected_tablestore_instance = expected.get("TABLESTORE_INSTANCE", "").strip()
    actual_tablestore_instance = environment.get("TABLESTORE_INSTANCE")
    target_url_matches = bool(expected_target_url) and hmac.compare_digest(
        actual_target_url,
        expected_target_url,
    )
    target_token_matches = (
        isinstance(actual_target_token, str)
        and bool(expected_target_token)
        and hmac.compare_digest(actual_target_token, expected_target_token)
    )
    operator_token_matches = (
        isinstance(actual_operator_token, str)
        and bool(expected_operator_token)
        and hmac.compare_digest(actual_operator_token, expected_operator_token)
    )
    tablestore_endpoint_matches = (
        isinstance(actual_tablestore_endpoint, str)
        and bool(expected_tablestore_endpoint)
        and hmac.compare_digest(
            actual_tablestore_endpoint,
            expected_tablestore_endpoint,
        )
    )
    tablestore_instance_matches = (
        isinstance(actual_tablestore_instance, str)
        and bool(expected_tablestore_instance)
        and hmac.compare_digest(
            actual_tablestore_instance,
            expected_tablestore_instance,
        )
    )
    result: dict[str, Any] = {
        "function_name": getattr(body, "function_name", None) == FUNCTION_NAME,
        "last_modified_time": last_modified_time,
        "last_modified_time_valid": last_modified_time is not None,
        "last_update_status": safe_status,
        "last_update_status_reported": status_reported,
        "last_update_status_accepted": (
            not status_reported
            or safe_status in ACCEPTED_LAST_UPDATE_STATUSES
        ),
        "state": safe_state,
        "function_state_reported": state_reported,
        "function_state_accepted": (
            not state_reported or safe_state in ACCEPTED_FUNCTION_STATES
        ),
        "role_matches": (
            bool(expected_role)
            and isinstance(actual_role, str)
            and actual_role == expected_role
        ),
        "temporary_credentials_enabled": getattr(
            body, "disable_inject_credentials", None
        )
        in (None, "None"),
        "memory_backend_tablestore": environment.get("MEMORY_BACKEND")
        == "tablestore",
        "embedding_contract_matches": (
            environment.get("EMBEDDING_MODEL") == "text-embedding-v4"
            and environment.get("EMBEDDING_DIM") == "1024"
        ),
        "tablestore_config_present": bool(
            environment.get("TABLESTORE_ENDPOINT")
            and environment.get("TABLESTORE_INSTANCE")
        ),
        "tablestore_endpoint_matches": tablestore_endpoint_matches,
        "tablestore_instance_matches": tablestore_instance_matches,
        "demo_target_url_matches": target_url_matches,
        "demo_target_token_matches": target_token_matches,
        "operator_token_matches": operator_token_matches,
        "operator_auth_configured": operator_token_matches,
        "real_restart_adapter_configured": (
            target_url_matches and target_token_matches
        ),
        "long_lived_alibaba_credentials_configured": any(
            name in environment
            for name in (
                "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "ALIBABA_CLOUD_SECURITY_TOKEN",
            )
        ),
    }
    if isinstance(reason_code, str) and _SAFE_STATUS.fullmatch(reason_code):
        result["last_update_status_reason_code"] = reason_code
    result["configuration_matches"] = all(
        result[name]
        for name in (
            "function_name",
            "last_modified_time_valid",
            "function_state_accepted",
            "last_update_status_accepted",
            "role_matches",
            "temporary_credentials_enabled",
            "memory_backend_tablestore",
            "embedding_contract_matches",
            "tablestore_config_present",
            "tablestore_endpoint_matches",
            "tablestore_instance_matches",
            "operator_auth_configured",
            "real_restart_adapter_configured",
        )
    ) and not result["long_lived_alibaba_credentials_configured"]
    return result


EXPECTED_MAX_RUNNING_JOBS = 1
EXPECTED_MAX_PENDING_JOBS = 3
EXPECTED_PENDING_TIMEOUT_SECONDS = 300.0
EXPECTED_JOB_TIMEOUT_SECONDS = 240.0


def _lifecycle_summary(
    max_running_jobs: Any,
    max_pending_jobs: Any,
    pending_timeout_seconds: Any,
    job_timeout_seconds: Any,
    real_dispatch_timeout_reconciliation_ready: Any,
) -> dict[str, Any]:
    """Prove ADR-024's exact application constants and the ADR-028 guard.

    These are compiled-in module constants, not environment flags, so verifying
    them here fails closed on any drift or spoofed value (ADR-024 clause 4).
    Booleans use ``is`` comparisons so a truthy substitute cannot satisfy the
    real-dispatch guard.
    """

    checks: dict[str, Any] = {
        "build_max_running_jobs_one": max_running_jobs == EXPECTED_MAX_RUNNING_JOBS,
        "build_max_pending_jobs_three": (
            max_pending_jobs == EXPECTED_MAX_PENDING_JOBS
        ),
        "build_pending_timeout_300s": (
            pending_timeout_seconds == EXPECTED_PENDING_TIMEOUT_SECONDS
        ),
        "build_job_timeout_240s": (
            job_timeout_seconds == EXPECTED_JOB_TIMEOUT_SECONDS
        ),
        # ADR-028 is accepted and implemented, so real dispatch is
        # reconciliation-ready. Only an exact True is accepted (a truthy
        # substitute must not satisfy the readiness proof).
        "build_real_dispatch_reconciliation_ready": (
            real_dispatch_timeout_reconciliation_ready is True
        ),
    }
    checks["build_lifecycle_matches"] = all(checks.values())
    return checks


def _build_lifecycle_summary() -> dict[str, Any]:
    """Read the constants from the exact build that deploy bundles."""

    from app.agent.runtime import (
        LIFECYCLE_JOB_TIMEOUT_SECONDS,
        LIFECYCLE_MAX_PENDING_JOBS,
        LIFECYCLE_MAX_RUNNING_JOBS,
        LIFECYCLE_PENDING_TIMEOUT_SECONDS,
        REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY,
    )

    return _lifecycle_summary(
        LIFECYCLE_MAX_RUNNING_JOBS,
        LIFECYCLE_MAX_PENDING_JOBS,
        LIFECYCLE_PENDING_TIMEOUT_SECONDS,
        LIFECYCLE_JOB_TIMEOUT_SECONDS,
        REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY,
    )


def _capacity_summary(
    controller_provision: Any,
    controller_concurrency: Any,
    target_provision: Any,
    target_concurrency: Any,
) -> dict[str, Any]:
    """Return only bounded proof of the two accepted active FC instances."""

    def one_function(provision: Any, concurrency: Any) -> dict[str, bool]:
        # ADR-024 single-instance non-idle invariants. always_allocate_gpu is
        # deliberately not gated here: FC 3.0 returns it True for non-idle CPU
        # functions regardless of the manifest's alwaysAllocateGPU:false, and with
        # no gpuConfig it is inert (no GPU is ever allocated). The manifest
        # declares false and `fc.py verify` covers that intent; gating on the live
        # API value is a confirmed false-negative (PRAXIS-151).
        return {
            "always_allocate_cpu": getattr(provision, "always_allocate_cpu", None)
            is True,
            "default_target_one": getattr(provision, "default_target", None) == 1,
            "current_one": getattr(provision, "current", None) == 1,
            "reserved_concurrency_one": getattr(
                concurrency, "reserved_concurrency", None
            )
            == 1,
        }

    controller = one_function(controller_provision, controller_concurrency)
    target = one_function(target_provision, target_concurrency)
    result: dict[str, Any] = {
        "controller": controller,
        "target": target,
    }
    result["active_capacity_matches"] = all(controller.values()) and all(
        target.values()
    )
    return result


def _probe() -> dict[str, Any]:
    from alibabacloud_fc20230330.client import Client
    from alibabacloud_fc20230330.models import (
        GetFunctionRequest,
        GetProvisionConfigRequest,
    )
    from alibabacloud_tea_openapi.models import Config
    from darabonba.runtime import RuntimeOptions

    env = dict(_require_controller_adapter_environment(_environment()))
    credentials = Credentials(
        env["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        env["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        env.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
    )
    client = Client(
        Config(
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token or None,
            protocol="HTTPS",
            region_id=REGION,
            connect_timeout=5_000,
            read_timeout=10_000,
        )
    )
    runtime = RuntimeOptions(
        autoretry=False,
        max_attempts=1,
        connect_timeout=5_000,
        read_timeout=10_000,
    )
    response = client.get_function_with_options(
        FUNCTION_NAME,
        GetFunctionRequest(qualifier="LATEST"),
        {},
        runtime,
    )
    summary = _summary(response.body, env)
    controller_provision = client.get_provision_config_with_options(
        FUNCTION_NAME,
        GetProvisionConfigRequest(qualifier="LATEST"),
        {},
        runtime,
    ).body
    controller_concurrency = client.get_concurrency_config_with_options(
        FUNCTION_NAME,
        {},
        runtime,
    ).body
    target_provision = client.get_provision_config_with_options(
        TARGET_FUNCTION_NAME,
        GetProvisionConfigRequest(qualifier="LATEST"),
        {},
        runtime,
    ).body
    target_concurrency = client.get_concurrency_config_with_options(
        TARGET_FUNCTION_NAME,
        {},
        runtime,
    ).body
    capacity = _capacity_summary(
        controller_provision,
        controller_concurrency,
        target_provision,
        target_concurrency,
    )
    lifecycle = _build_lifecycle_summary()
    return {
        "ok": summary["configuration_matches"] is True
        and capacity["active_capacity_matches"] is True
        and lifecycle["build_lifecycle_matches"] is True,
        **summary,
        **capacity,
        **lifecycle,
    }


def run() -> int:
    output = _safe_execute(_probe, failure_reason="fc_probe_failed")
    print(json.dumps(output, sort_keys=True))
    return 0 if output.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(run())
