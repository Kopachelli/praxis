from __future__ import annotations

from scripts.capture_submission_screenshots import (
    DISCLOSURE,
    FIXTURE_CONNECTION_LABEL,
    _parse_tesseract_tsv,
    build_fixtures,
    inject_capture_disclosure,
)


def test_submission_fixtures_cover_three_distinct_evidence_states() -> None:
    fixtures = build_fixtures()

    assert set(fixtures) == {
        "inc_demo_awaiting_20260721",
        "inc_demo_resolved_20260721",
        "inc_demo_recurrence_20260721",
    }
    assert fixtures["inc_demo_awaiting_20260721"]["state"] == "AWAITING_APPROVAL"
    assert fixtures["inc_demo_resolved_20260721"]["state"] == "RESOLVED"
    recurrence = fixtures["inc_demo_recurrence_20260721"]
    assert recurrence["state"] == "AWAITING_APPROVAL"
    assert recurrence["memory_match"]["incident_id"] == "inc_demo_resolved_20260721"
    assert recurrence["memory_match"]["similarity"] == 0.92


def test_submission_fixture_has_approval_execution_and_dry_run_evidence() -> None:
    trail = build_fixtures()["inc_demo_resolved_20260721"]["trail"]

    assert any(entry["type"] == "approval" for entry in trail)
    assert any(
        entry["type"] == "execution"
        and entry["content"].get("status") == "succeeded"
        and entry["content"].get("before_boot_id") != entry["content"].get("after_boot_id")
        for entry in trail
    )
    assert any(
        entry["type"] == "execution" and entry["content"].get("dry_run") is True
        for entry in trail
    )


def test_submission_fixture_is_qwen_only_and_contains_no_credential_fields() -> None:
    rendered = repr(build_fixtures()).lower()

    assert "qwen" in rendered
    assert all(name not in rendered for name in ("gpt", "claude", "gemini"))
    assert all(
        key not in rendered
        for key in ("api_key", "apikey", "access_key", "secret_key", "authorization")
    )


def test_capture_disclosure_is_injected_without_mutating_source_contract() -> None:
    source = (
        "<html><head><title>Praxis</title></head><body>"
        '<span id="connection-text">Connecting</span><main>UI</main>'
        '<script>setConnection("online", "Live");</script></body></html>'
    )

    rendered = inject_capture_disclosure(source)

    assert DISCLOSURE in rendered
    assert 'id="capture-disclosure"' in rendered
    assert "<main>UI</main>" in rendered
    assert f'<span id="connection-text">{FIXTURE_CONNECTION_LABEL}</span>' in rendered
    assert '<span id="connection-text">Connecting</span>' not in rendered
    assert f'setConnection("online", "{FIXTURE_CONNECTION_LABEL}");' in rendered
    assert 'setConnection("online", "Live");' not in rendered


def test_tesseract_tsv_parser_treats_quote_text_as_literal() -> None:
    payload = (
        "level\tleft\ttop\ttext\n"
        '5\t10\t20\t"\n'
        "5\t30\t40\tDecision\n"
    )

    rows = _parse_tesseract_tsv(payload)

    assert rows == [
        {"level": "5", "left": "10", "top": "20", "text": '"'},
        {"level": "5", "left": "30", "top": "40", "text": "Decision"},
    ]
