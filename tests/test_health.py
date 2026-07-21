from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from app.main import _build_runtime_tool_registry, app


_TARGET_URL = "https://praxis-demo-target.ap-southeast-1.fcapp.run"
_TARGET_TOKEN = "runtime-target-secret-sentinel-0123456789"


def test_healthz_matches_deployment_proof_contract() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["primary_model"].startswith("qwen")
    assert body["deployed_on"] in {"local", "alibaba-fc"}
    assert body["version"]
    assert response.headers["X-Trace-Id"]
    assert body["trace_id"] == response.headers["X-Trace-Id"]


def test_healthz_reports_lifecycle_and_reconciliation_readiness_truthfully() -> None:
    """PRAXIS-146/147: readiness surfaces the ADR-024 constants and ADR-028
    dispatch guard so it is never inferred from a green liveness check alone."""

    body = TestClient(app).get("/healthz").json()

    # ADR-028 is implemented, so real dispatch is reconciliation-ready.
    assert body["real_dispatch_timeout_reconciliation_ready"] is True
    assert isinstance(body["real_restart_adapter_configured"], bool)
    assert body["lifecycle"] == {
        "max_running_jobs": 1,
        "max_pending_jobs": 3,
        "pending_timeout_seconds": 300.0,
        "job_timeout_seconds": 240.0,
    }


def test_root_serves_operator_ui_without_external_dependencies() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "content-disposition" not in response.headers
    assert "Praxis" in response.text
    assert "https://" not in response.text


def test_operator_ui_renders_bounded_memory_match_metadata_safely() -> None:
    response = TestClient(app).get("/")
    html = response.text

    assert 'makeElement("strong", "", "Incident: ")' in html
    assert 'document.createTextNode(safeString(memory.incident_id, "Unknown incident"))' in html
    assert 'makeElement("strong", "", "Similarity: ")' in html
    assert "document.createTextNode(formatSimilarity(memory.similarity))" in html
    assert 'typeof value !== "number" || !Number.isFinite(value)' in html
    assert "Math.min(1, Math.max(0, value))" in html
    assert 'maximumFractionDigits: 1' in html
    assert "elements.memorySection.hidden = !available" in html
    assert "if (!available) return;" in html


def test_final_production_constructs_real_restart_adapter_secret_safely() -> None:
    settings = replace(
        app.state.settings,
        app_env="production",
        deployed_on="alibaba-fc",
        demo_target_url=_TARGET_URL,
        demo_target_token=_TARGET_TOKEN,
    )

    registry = _build_runtime_tool_registry(settings)

    assert registry.real_restart_configured is True
    assert _TARGET_TOKEN not in repr(registry)


def test_production_registry_fails_closed_without_real_restart_adapter() -> None:
    settings = replace(
        app.state.settings,
        app_env="production",
        deployed_on="alibaba-fc",
        demo_target_url="",
        demo_target_token="",
    )

    with pytest.raises(RuntimeError, match="requires the real isolated"):
        _build_runtime_tool_registry(settings)
