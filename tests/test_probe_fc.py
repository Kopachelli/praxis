from types import SimpleNamespace

import pytest

from scripts.probe_fc import (
    _build_lifecycle_summary,
    _capacity_summary,
    _lifecycle_summary,
    _require_controller_adapter_environment,
    _summary,
)


_TARGET_URL = "https://praxis-demo-target.ap-southeast-1.fcapp.run"
_TARGET_TOKEN = "target-token-secret-sentinel-0123456789"
_TABLESTORE_ENDPOINT = "https://praxis.ap-southeast-1.ots.aliyuncs.com"
_TABLESTORE_INSTANCE = "praxis"
_ROLE = "acs:ram::1234567890123456:role/praxis-fc-tablestore-role"
_OPERATOR_TOKEN = "operator-token-secret-sentinel-0123456789"


def _expected_environment() -> dict[str, str]:
    return {
        "FC_EXECUTION_ROLE_ARN": _ROLE,
        "PRAXIS_OPERATOR_TOKEN": _OPERATOR_TOKEN,
        "PRAXIS_DEMO_TARGET_URL": _TARGET_URL,
        "PRAXIS_DEMO_TARGET_TOKEN": _TARGET_TOKEN,
        "TABLESTORE_ENDPOINT": _TABLESTORE_ENDPOINT,
        "TABLESTORE_INSTANCE": _TABLESTORE_INSTANCE,
    }


def _function(**overrides):
    values = {
        "function_name": "praxis-api",
        "last_modified_time": "2026-07-21T01:23:00Z",
        "last_update_status": "Successful",
        "last_update_status_reason_code": None,
        "state": "Active",
        "role": _ROLE,
        "disable_inject_credentials": None,
        "environment_variables": {
            "MEMORY_BACKEND": "tablestore",
            "EMBEDDING_MODEL": "text-embedding-v4",
            "EMBEDDING_DIM": "1024",
            "TABLESTORE_ENDPOINT": _TABLESTORE_ENDPOINT,
            "TABLESTORE_INSTANCE": _TABLESTORE_INSTANCE,
            "PRAXIS_DEMO_TARGET_URL": _TARGET_URL,
            "PRAXIS_DEMO_TARGET_TOKEN": _TARGET_TOKEN,
            "PRAXIS_OPERATOR_TOKEN": _OPERATOR_TOKEN,
            "DASHSCOPE_API_KEY": "must-not-escape",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _provision(**overrides):
    values = {
        "always_allocate_cpu": True,
        "always_allocate_gpu": False,
        "default_target": 1,
        "current": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _concurrency(**overrides):
    values = {"reserved_concurrency": 1}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_summary_allows_only_configuration_evidence() -> None:
    result = _summary(_function(), _expected_environment())

    assert result["configuration_matches"] is True
    assert result["function_state_accepted"] is True
    assert result["last_update_status_accepted"] is True
    assert result["last_modified_time_valid"] is True
    assert result["tablestore_endpoint_matches"] is True
    assert result["tablestore_instance_matches"] is True
    assert result["real_restart_adapter_configured"] is True
    assert result["operator_auth_configured"] is True
    assert result["last_update_status"] == "Successful"
    assert "environment_variables" not in result
    assert "must-not-escape" not in repr(result)
    assert _TARGET_TOKEN not in repr(result)
    assert _OPERATOR_TOKEN not in repr(result)


def test_capacity_summary_requires_two_active_single_instance_functions() -> None:
    result = _capacity_summary(
        _provision(),
        _concurrency(),
        _provision(),
        _concurrency(),
    )

    assert result["active_capacity_matches"] is True
    assert result["controller"] == {
        "always_allocate_cpu": True,
        "default_target_one": True,
        "current_one": True,
        "reserved_concurrency_one": True,
    }
    assert result["target"] == result["controller"]


@pytest.mark.parametrize(
    ("component", "field", "value", "failed_check"),
    (
        ("controller_provision", "always_allocate_cpu", False, "always_allocate_cpu"),
        ("controller_provision", "default_target", 0, "default_target_one"),
        ("controller_provision", "current", 0, "current_one"),
        ("controller_concurrency", "reserved_concurrency", 2, "reserved_concurrency_one"),
        ("target_provision", "always_allocate_cpu", None, "always_allocate_cpu"),
        ("target_concurrency", "reserved_concurrency", None, "reserved_concurrency_one"),
    ),
)
def test_capacity_summary_fails_closed_on_lifecycle_or_scale_drift(
    component: str,
    field: str,
    value: object,
    failed_check: str,
) -> None:
    inputs = {
        "controller_provision": _provision(),
        "controller_concurrency": _concurrency(),
        "target_provision": _provision(),
        "target_concurrency": _concurrency(),
    }
    setattr(inputs[component], field, value)

    result = _capacity_summary(
        inputs["controller_provision"],
        inputs["controller_concurrency"],
        inputs["target_provision"],
        inputs["target_concurrency"],
    )

    assert result["active_capacity_matches"] is False
    side = "target" if component.startswith("target") else "controller"
    assert result[side][failed_check] is False


def test_summary_rejects_long_lived_credentials_and_role_drift() -> None:
    function = _function(
        role="acs:ram::1234567890123456:role/wrong-role",
        environment_variables={
            **_function().environment_variables,
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "must-not-escape",
        },
    )
    result = _summary(
        function,
        _expected_environment(),
    )

    assert result["configuration_matches"] is False
    assert result["role_matches"] is False
    assert result["long_lived_alibaba_credentials_configured"] is True
    assert "must-not-escape" not in repr(result)


@pytest.mark.parametrize(
    "missing_variable",
    (
        "FC_EXECUTION_ROLE_ARN",
        "PRAXIS_OPERATOR_TOKEN",
        "PRAXIS_DEMO_TARGET_URL",
        "PRAXIS_DEMO_TARGET_TOKEN",
        "TABLESTORE_ENDPOINT",
        "TABLESTORE_INSTANCE",
    ),
)
def test_probe_fails_locally_without_final_adapter_configuration(
    missing_variable: str,
) -> None:
    environment = _expected_environment()
    environment[missing_variable] = ""

    with pytest.raises(RuntimeError, match=missing_variable) as captured:
        _require_controller_adapter_environment(environment)

    assert _TARGET_TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    ("actual_role", "expected_role"),
    (
        (None, None),
        (None, "acs:ram::1234567890123456:role/praxis-fc-tablestore-role"),
        ("", "acs:ram::1234567890123456:role/praxis-fc-tablestore-role"),
    ),
)
def test_summary_rejects_missing_expected_or_actual_role(
    actual_role: str | None,
    expected_role: str | None,
) -> None:
    expected = {
        "PRAXIS_OPERATOR_TOKEN": _OPERATOR_TOKEN,
        "PRAXIS_DEMO_TARGET_URL": _TARGET_URL,
        "PRAXIS_DEMO_TARGET_TOKEN": _TARGET_TOKEN,
        "TABLESTORE_ENDPOINT": _TABLESTORE_ENDPOINT,
        "TABLESTORE_INSTANCE": _TABLESTORE_INSTANCE,
    }
    if expected_role is not None:
        expected["FC_EXECUTION_ROLE_ARN"] = expected_role

    result = _summary(_function(role=actual_role), expected)

    assert result["role_matches"] is False
    assert result["configuration_matches"] is False


@pytest.mark.parametrize(
    ("variable", "deployed_value"),
    (
        (
            "PRAXIS_DEMO_TARGET_URL",
            "https://other-target.ap-southeast-1.fcapp.run",
        ),
        ("PRAXIS_DEMO_TARGET_TOKEN", "different-secret-token-0123456789"),
    ),
)
def test_summary_rejects_deployed_restart_adapter_drift(
    variable: str,
    deployed_value: str,
) -> None:
    environment = {**_function().environment_variables, variable: deployed_value}
    result = _summary(
        _function(environment_variables=environment),
        _expected_environment(),
    )

    assert result["configuration_matches"] is False
    assert result["real_restart_adapter_configured"] is False
    assert _TARGET_TOKEN not in repr(result)
    assert deployed_value not in repr(result)


def test_summary_rejects_deployed_operator_token_drift() -> None:
    sentinel = "different-operator-token-0123456789"
    environment = {
        **_function().environment_variables,
        "PRAXIS_OPERATOR_TOKEN": sentinel,
    }

    result = _summary(
        _function(environment_variables=environment),
        _expected_environment(),
    )

    assert result["operator_auth_configured"] is False
    assert result["configuration_matches"] is False
    assert sentinel not in repr(result)
    assert _OPERATOR_TOKEN not in repr(result)


@pytest.mark.parametrize(
    ("variable", "deployed_value", "failed_check"),
    (
        (
            "TABLESTORE_ENDPOINT",
            "https://wrong.ap-southeast-1.ots.aliyuncs.com",
            "tablestore_endpoint_matches",
        ),
        ("TABLESTORE_INSTANCE", "wrong-instance", "tablestore_instance_matches"),
    ),
)
def test_summary_rejects_deployed_tablestore_target_drift(
    variable: str,
    deployed_value: str,
    failed_check: str,
) -> None:
    environment = {**_function().environment_variables, variable: deployed_value}

    result = _summary(
        _function(environment_variables=environment),
        _expected_environment(),
    )

    assert result[failed_check] is False
    assert result["configuration_matches"] is False
    assert deployed_value not in repr(result)


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    (
        ({"state": "Pending"}, "function_state_accepted"),
        ({"state": "Failed"}, "function_state_accepted"),
        ({"last_update_status": "InProgress"}, "last_update_status_accepted"),
        ({"last_update_status": "Failed"}, "last_update_status_accepted"),
    ),
)
def test_summary_rejects_non_ready_function_lifecycle(
    overrides: dict[str, str],
    failed_check: str,
) -> None:
    result = _summary(
        _function(**overrides),
        _expected_environment(),
    )

    assert result[failed_check] is False
    assert result["configuration_matches"] is False


def test_build_lifecycle_summary_matches_the_deployed_constants() -> None:
    """PRAXIS-147: the probe proves ADR-024's exact 1/3/300/240 constants and
    the ADR-028 real-dispatch guard from the build that deploy bundles."""

    result = _build_lifecycle_summary()

    assert result == {
        "build_max_running_jobs_one": True,
        "build_max_pending_jobs_three": True,
        "build_pending_timeout_300s": True,
        "build_job_timeout_240s": True,
        "build_real_dispatch_reconciliation_ready": True,
        "build_lifecycle_matches": True,
    }


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    (
        ({"max_running_jobs": 2}, "build_max_running_jobs_one"),
        ({"max_pending_jobs": 4}, "build_max_pending_jobs_three"),
        ({"pending_timeout_seconds": 200.0}, "build_pending_timeout_300s"),
        ({"job_timeout_seconds": 120.0}, "build_job_timeout_240s"),
        (
            {"real_dispatch_timeout_reconciliation_ready": False},
            "build_real_dispatch_reconciliation_ready",
        ),
    ),
)
def test_lifecycle_summary_fails_closed_on_constant_drift(
    overrides: dict[str, object],
    failed_check: str,
) -> None:
    """Any drifted lifecycle constant or unready reconciliation fails the probe."""

    values: dict[str, object] = {
        "max_running_jobs": 1,
        "max_pending_jobs": 3,
        "pending_timeout_seconds": 300.0,
        "job_timeout_seconds": 240.0,
        "real_dispatch_timeout_reconciliation_ready": True,
    }
    values.update(overrides)

    result = _lifecycle_summary(
        values["max_running_jobs"],
        values["max_pending_jobs"],
        values["pending_timeout_seconds"],
        values["job_timeout_seconds"],
        values["real_dispatch_timeout_reconciliation_ready"],
    )

    assert result[failed_check] is False
    assert result["build_lifecycle_matches"] is False


@pytest.mark.parametrize("spoof", (False, 1, "true", 0, None, "True"))
def test_lifecycle_summary_rejects_non_true_reconciliation_ready_spoof(
    spoof: object,
) -> None:
    """Only a real ``True`` proves reconciliation readiness; substitutes fail."""

    result = _lifecycle_summary(1, 3, 300.0, 240.0, spoof)

    assert result["build_real_dispatch_reconciliation_ready"] is False
    assert result["build_lifecycle_matches"] is False


def test_summary_accepts_both_omitted_lifecycle_fields() -> None:
    """FC3 may omit steady-state state/lastUpdateStatus [PRAXIS-144, ADR-024].

    Absent values are unavailable evidence, not a failure; the exact
    environment, role, timestamp, and capacity proofs remain mandatory.
    """

    result = _summary(
        _function(state=None, last_update_status=None),
        _expected_environment(),
    )

    assert result["function_state_reported"] is False
    assert result["last_update_status_reported"] is False
    assert result["state"] is None
    assert result["last_update_status"] is None
    assert result["function_state_accepted"] is True
    assert result["last_update_status_accepted"] is True
    assert result["configuration_matches"] is True


@pytest.mark.parametrize(
    ("overrides", "reported_absent"),
    (
        ({"state": None}, "function_state_reported"),
        ({"last_update_status": None}, "last_update_status_reported"),
    ),
)
def test_summary_accepts_partial_omitted_with_valid_present(
    overrides: dict[str, object],
    reported_absent: str,
) -> None:
    """One omitted field with the other explicitly ready still passes."""

    result = _summary(
        _function(**overrides),
        _expected_environment(),
    )

    assert result[reported_absent] is False
    assert result["function_state_accepted"] is True
    assert result["last_update_status_accepted"] is True
    assert result["configuration_matches"] is True


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    (
        ({"state": None, "last_update_status": "Failed"}, "last_update_status_accepted"),
        ({"state": "Failed", "last_update_status": None}, "function_state_accepted"),
    ),
)
def test_summary_rejects_partial_omitted_with_explicit_failure(
    overrides: dict[str, object],
    failed_check: str,
) -> None:
    """An explicit non-ready value fails even when the other field is omitted."""

    result = _summary(
        _function(**overrides),
        _expected_environment(),
    )

    assert result[failed_check] is False
    assert result["configuration_matches"] is False


def test_summary_fails_closed_and_redacts_invalid_lifecycle_values() -> None:
    sentinel = "bad status with credential-and-signature-must-not-escape"

    result = _summary(
        _function(state=sentinel, last_update_status=sentinel),
        _expected_environment(),
    )

    assert result["state"] is None
    assert result["last_update_status"] is None
    assert result["function_state_accepted"] is False
    assert result["last_update_status_accepted"] is False
    assert result["configuration_matches"] is False
    assert sentinel not in repr(result)


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-07-21T01:23:00Z",
        "2026-07-21t01:23:00z",
        "2026-07-21T01:23:00.123456789Z",
        "2026-07-21T03:23:00+02:00",
        "2026-07-20T23:23:00-02:00",
    ),
)
def test_summary_preserves_only_bounded_valid_rfc3339_timestamps(
    timestamp: str,
) -> None:
    result = _summary(
        _function(last_modified_time=timestamp),
        _expected_environment(),
    )

    assert result["last_modified_time"] == timestamp
    assert result["last_modified_time_valid"] is True
    assert result["configuration_matches"] is True


@pytest.mark.parametrize(
    "timestamp",
    (
        None,
        1_721_525_380,
        "",
        "2026-07-21 01:23:00Z",
        "2026-07-21T01:23:00",
        "2026-02-30T01:23:00Z",
        "2026-07-21T24:00:00Z",
        "2026-07-21T01:23:00+24:00",
        "2026-07-21T01:23:00.1234567890Z",
        "2026-07-21T01:23:00Zcredential-must-not-escape",
    ),
)
def test_summary_redacts_absent_malformed_or_unbounded_timestamps(
    timestamp: object,
) -> None:
    result = _summary(
        _function(last_modified_time=timestamp),
        _expected_environment(),
    )

    assert result["last_modified_time"] is None
    assert result["last_modified_time_valid"] is False
    assert result["configuration_matches"] is False
    assert "credential-must-not-escape" not in repr(result)
