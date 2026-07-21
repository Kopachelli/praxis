from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.check_plan_latency import (
    MAX_EVIDENCE_BYTES,
    PlanLatencyEvidenceError,
    evaluate_evidence,
    run,
)


START = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _incident(index: int, latency: float) -> dict[str, Any]:
    created = START + timedelta(minutes=index)
    ready = created + timedelta(seconds=latency)
    triage_trace = f"{index + 1:032x}"
    return {
        "id": f"inc_{index + 1:032x}",
        "source": "sentry",
        "service": "checkout-service",
        "severity": "high",
        "signal": "upstream_timeout",
        "title": "TimeoutError in checkout-service",
        "state": "AWAITING_APPROVAL",
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "trace_id": f"{index + 101:032x}",
        "plan": {
            "status": "proposed",
            "steps": [
                {
                    "seq": 1,
                    "action": "Restart checkout-service worker pool",
                    "tool": "restart_service",
                    "args": {"service": "checkout-service"},
                    "risk_level": "safe",
                    "rollback": "Restart the prior healthy revision",
                }
            ],
        },
        "trail": [
            {
                "seq": 1,
                "type": "thought",
                "content": {
                    "stage": "classification",
                    "classification": "upstream dependency timeout",
                    "provider": "qwencloud",
                    "model": "qwen-flash",
                    "trace_id": triage_trace,
                },
                "model_used": "qwen-flash",
                "tokens": 9,
                "timestamp": created.isoformat(),
            },
            {
                "seq": 2,
                "type": "thought",
                "content": {
                    "stage": "plan_ready",
                    "status": "ready",
                    "trace_id": triage_trace,
                },
                "timestamp": ready.isoformat(),
            }
        ],
    }


def _payload(*latencies: float) -> dict[str, Any]:
    return {
        "incidents": [
            _incident(index, latency) for index, latency in enumerate(latencies)
        ]
    }


def _written(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "latency-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_nearest_rank_p95_passes_only_below_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _written(tmp_path, _payload(1.0, 2.0, 3.0, 4.0, 29.999))

    assert run(path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "p95_seconds": 29.999,
        "reason": "within_target",
        "sample_count": 5,
        "target_seconds": 30.0,
    }


def test_threshold_is_strict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _written(tmp_path, _payload(1.0, 2.0, 3.0, 4.0, 30.0))

    assert run(path) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "p95_seconds": 30.0,
        "reason": "threshold_not_met",
        "sample_count": 5,
        "target_seconds": 30.0,
    }


def test_passing_output_preserves_microsecond_threshold_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _written(tmp_path, _payload(1.0, 2.0, 3.0, 4.0, 29.9999))

    assert run(path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "p95_seconds": 29.9999,
        "reason": "within_target",
        "sample_count": 5,
        "target_seconds": 30.0,
    }


def test_nearest_rank_p95_uses_the_ceil_rank() -> None:
    result = evaluate_evidence(_payload(*range(1, 21)))

    assert result.sample_count == 20
    assert result.p95_seconds == 19.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: [],
        lambda value: {"incidents": value["incidents"][:4]},
        lambda value: {
            "incidents": value["incidents"] + [value["incidents"][0]]
        },
        lambda value: _change(value, 0, "state", "TRIAGED"),
        lambda value: _change(value, 0, "plan", None),
        lambda value: _change(value, 0, "trail", []),
        lambda value: _change(value, 0, "created_at", "2026-07-21T12:00:00"),
        lambda value: _change(
            value,
            0,
            "created_at",
            "2026-07-21T12:00:10+00:00",
        ),
        lambda value: _change_plan_ready_event(value, "trace_id", "not-a-trace"),
        lambda value: _change_plan_ready_event(value, "extra", "unexpected"),
    ],
)
def test_invalid_evidence_matrix_is_rejected(mutate: Any) -> None:
    with pytest.raises(PlanLatencyEvidenceError):
        evaluate_evidence(mutate(_payload(1, 2, 3, 4, 5)))


def _change(
    payload: dict[str, Any],
    index: int,
    key: str,
    value: Any,
) -> dict[str, Any]:
    payload["incidents"][index][key] = value
    return payload


def _change_plan_ready_event(
    payload: dict[str, Any],
    key: str,
    value: Any,
) -> dict[str, Any]:
    payload["incidents"][0]["trail"][1]["content"][key] = value
    return payload


def test_minimal_fabricated_shape_is_rejected() -> None:
    fabricated = {
        "incidents": [
            {
                "id": f"inc_{index + 1:032x}",
                "state": "AWAITING_APPROVAL",
                "created_at": START.isoformat(),
                "plan": {"status": "proposed", "steps": [None]},
                "trail": [
                    {
                        "type": "thought",
                        "content": {
                            "stage": "plan_ready",
                            "status": "ready",
                            "trace_id": f"{index + 1:032x}",
                        },
                        "timestamp": (START + timedelta(seconds=1)).isoformat(),
                    }
                ],
            }
            for index in range(5)
        ]
    }

    with pytest.raises(PlanLatencyEvidenceError):
        evaluate_evidence(fabricated)


def test_mixed_classification_and_plan_ready_trace_is_rejected() -> None:
    payload = _payload(1, 2, 3, 4, 5)
    payload["incidents"][0]["trail"][1]["content"]["trace_id"] = "f" * 32

    with pytest.raises(PlanLatencyEvidenceError, match="mixed triage evidence"):
        evaluate_evidence(payload)


@pytest.mark.parametrize("defect", ["sequence", "timestamp"])
def test_trail_sequence_and_timestamp_inversion_is_rejected(defect: str) -> None:
    payload = _payload(1, 2, 3, 4, 5)
    plan_ready = payload["incidents"][0]["trail"][1]
    if defect == "sequence":
        plan_ready["seq"] = 1
    else:
        plan_ready["timestamp"] = (
            START - timedelta(microseconds=1)
        ).isoformat()

    with pytest.raises(PlanLatencyEvidenceError):
        evaluate_evidence(payload)


def test_duplicate_or_phantom_plan_ready_is_rejected() -> None:
    payload = _payload(1, 2, 3, 4, 5)
    duplicate = json.loads(
        json.dumps(payload["incidents"][0]["trail"][1])
    )
    duplicate["seq"] = 3
    duplicate["timestamp"] = (
        START + timedelta(seconds=2)
    ).isoformat()
    payload["incidents"][0]["trail"].append(duplicate)

    with pytest.raises(PlanLatencyEvidenceError, match="invalid plan-ready event"):
        evaluate_evidence(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda incident: incident["plan"].update(status="draft"),
        lambda incident: incident["plan"]["steps"].append(
            {
                **incident["plan"]["steps"][0],
                "seq": 3,
            }
        ),
        lambda incident: incident["plan"]["steps"][0].update(
            unexpected="field"
        ),
    ],
)
def test_malformed_plan_structure_is_rejected(mutate: Any) -> None:
    payload = _payload(1, 2, 3, 4, 5)
    mutate(payload["incidents"][0])

    with pytest.raises(PlanLatencyEvidenceError):
        evaluate_evidence(payload)


def test_failure_output_never_echoes_evidence_or_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "Bearer latency-secret-sentinel"
    path = tmp_path / "secret-name-sentinel.json"
    path.write_text(sentinel, encoding="utf-8")

    assert run(path) == 1
    output = capsys.readouterr().out
    assert sentinel not in output
    assert path.name not in output
    assert json.loads(output) == {
        "ok": False,
        "p95_seconds": None,
        "reason": "invalid_evidence",
        "sample_count": 0,
        "target_seconds": 30.0,
    }


def test_oversized_evidence_is_rejected_before_parsing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))

    assert run(path) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_evidence"


def test_pathological_json_integer_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "pathological-integer.json"
    path.write_text(
        '{"incidents":[' + "9" * 5_000 + "]}",
        encoding="utf-8",
    )

    assert run(path) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "p95_seconds": None,
        "reason": "invalid_evidence",
        "sample_count": 0,
        "target_seconds": 30.0,
    }


def test_verifier_has_no_network_client_import() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_plan_latency.py"
    ).read_text(encoding="utf-8")

    assert "httpx" not in source
    assert "requests" not in source
    assert "socket" not in source
