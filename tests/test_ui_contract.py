"""First-party UI contract checks [FR-7, FR-14, ADR-014]."""

from __future__ import annotations

import re
from pathlib import Path

from app.approval import MAX_APPROVAL_NOTE_CHARS
from app.incidents import IncidentState


UI_PATH = Path(__file__).parents[1] / "ui" / "index.html"


def test_reject_note_limit_matches_the_api_contract() -> None:
    html = UI_PATH.read_text(encoding="utf-8")
    textarea = re.search(r'<textarea\s+id="reject-note"[^>]*>', html)

    assert textarea is not None
    assert f'maxlength="{MAX_APPROVAL_NOTE_CHARS}"' in textarea.group(0)


def test_removed_rejected_state_is_absent_from_runtime_and_ui() -> None:
    html = UI_PATH.read_text(encoding="utf-8")

    assert "REJECTED" not in IncidentState.__members__
    assert "REJECTED" not in html


def test_operator_token_is_memory_only_and_never_rendered_or_persisted() -> None:
    html = UI_PATH.read_text(encoding="utf-8")
    token_input = re.search(r'<input\s+id="operator-token-input"[\s\S]*?>', html)

    assert token_input is not None
    assert 'type="password"' in token_input.group(0)
    assert 'autocomplete="off"' in token_input.group(0)
    assert " value=" not in token_input.group(0)
    assert '<div id="operator-shell" class="shell" hidden>' in html
    assert 'headers.set("Authorization", `Bearer ${operatorToken}`);' in html
    assert 'elements.operatorTokenInput.value = "";' in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "document.cookie" not in html
    assert not re.search(r"searchParams\.(?:set|append)\([^)]*token", html, re.I)
    assert not re.search(r"textContent\s*=\s*operatorToken", html)
    assert not re.search(r"setAttribute\([^)]*operatorToken", html)


def test_operator_token_is_discarded_on_pagehide_and_bfcache_restore() -> None:
    """PRAXIS-148: the lexical token must not survive navigation or a BFCache
    restore. pagehide clears it before the heap is frozen; a persisted pageshow
    forces the locked view [NFR-5, ADR-025]."""

    html = UI_PATH.read_text(encoding="utf-8")

    assert 'window.addEventListener("pagehide", () => lockOperatorView());' in html
    assert re.search(
        r'addEventListener\(\s*"pageshow",\s*\(event\)\s*=>\s*\{\s*'
        r'if\s*\(event\.persisted\)\s*lockOperatorView\(\);\s*\}\s*\);',
        html,
    )
    # lockOperatorView nulls the in-memory token and clears the input field.
    assert re.search(
        r"function lockOperatorView\([^)]*\)\s*\{[\s\S]*?operatorToken = null;",
        html,
    )
    # The BFCache fix must not introduce storage, cookie, URL, or DOM echo.
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "document.cookie" not in html


def test_reconciliation_required_state_is_rendered_distinctly() -> None:
    """ADR-028: the uncertain reconciliation state is shown distinctly (not a
    plain failure) and offers no approve/reject controls."""

    html = UI_PATH.read_text(encoding="utf-8")

    assert "RECONCILIATION_REQUIRED" in IncidentState.__members__
    assert 'RECONCILIATION_REQUIRED: "Reconciliation required"' in html
    assert 'if (state === "RECONCILIATION_REQUIRED")' in html
    assert "requires manual reconciliation" in html
    # Approve/reject controls stay gated to AWAITING_APPROVAL (and operator role).
    assert (
        'const isAwaitingApproval = state === "AWAITING_APPROVAL" && !viewState.readOnly;'
        in html
    )


def test_public_demo_reads_ui_is_flag_gated_and_tokenless() -> None:
    """ADR-031: the UI carries the server-stamped flag, exposes a tokenless
    read-only entry, and only sends a bearer header when a token is present."""

    html = UI_PATH.read_text(encoding="utf-8")

    # Server-stamped flag placeholder and its reader.
    assert 'id="praxis-public-demo-reads"' in html
    assert "__PRAXIS_PUBLIC_DEMO_READS__" in html
    assert "const publicReadsEnabled" in html
    assert 'getAttribute("content") === "true"' in html
    # Tokenless entry point and its lock-screen affordance.
    assert "async function enterPublicDemoView()" in html
    assert 'id="demo-view-button"' in html
    # fetchJson only refuses when there is no token AND reads are not public, and
    # only attaches the bearer header when a token is present.
    assert "if (!hasToken && !publicReadsEnabled)" in html
    assert "if (hasToken) headers.set(" in html
