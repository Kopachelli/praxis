from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.check_recording_ui import (
    DEFAULT_UI_PATH,
    FAILURE_REASON,
    REQUIRED_MARKERS,
    SUCCESS_REASON,
    run,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, text: str, content_type: str) -> None:
        self.text = text
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


def _document() -> str:
    return "\n".join(REQUIRED_MARKERS)


def _payload(capsys: Any) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_default_mode_checks_repository_ui_without_network(capsys: Any) -> None:
    def forbidden_get(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default local mode must not perform HTTP")

    exit_code = run([], get=forbidden_get)

    assert exit_code == 0
    assert DEFAULT_UI_PATH.is_file()
    assert _payload(capsys) == {
        "ok": True,
        "reason": SUCCESS_REASON,
        "source": "local",
    }


def test_url_mode_uses_one_read_only_get_and_accepts_html(capsys: Any) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs))
        return FakeResponse(_document(), "text/html; charset=utf-8")

    exit_code = run(["--url", "https://example.invalid/ui"], get=fake_get)

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "https://example.invalid/ui"
    assert calls[0][1]["follow_redirects"] is True
    assert calls[0][1]["timeout"] == 15.0
    assert _payload(capsys) == {
        "ok": True,
        "reason": SUCCESS_REASON,
        "source": "url",
    }


def test_url_mode_rejects_non_html_before_marker_success(capsys: Any) -> None:
    def fake_get(_url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse(_document(), "application/octet-stream")

    exit_code = run(["--url", "https://example.invalid/download"], get=fake_get)

    assert exit_code == 1
    assert _payload(capsys) == {
        "ok": False,
        "reason": FAILURE_REASON,
        "source": "url",
    }


def test_each_required_marker_is_mandatory(capsys: Any, tmp_path: Path) -> None:
    for missing in REQUIRED_MARKERS:
        local_path = tmp_path / f"missing-{REQUIRED_MARKERS.index(missing)}.html"
        local_path.write_text(
            "\n".join(marker for marker in REQUIRED_MARKERS if marker != missing),
            encoding="utf-8",
        )

        exit_code = run([], local_path=local_path)

        assert exit_code == 1
        assert _payload(capsys) == {
            "ok": False,
            "reason": FAILURE_REASON,
            "source": "local",
        }


def test_failure_envelope_never_discloses_target_body_or_exception(
    capsys: Any,
) -> None:
    secret_url = "https://user:secret@example.invalid/?token=hidden"
    secret_message = "provider-secret-sentinel"

    def failing_get(_url: str, **_kwargs: object) -> FakeResponse:
        raise RuntimeError(secret_message)

    exit_code = run(["--url", secret_url], get=failing_get)
    rendered = json.dumps(_payload(capsys), sort_keys=True)

    assert exit_code == 1
    assert secret_url not in rendered
    assert secret_message not in rendered
    assert rendered == json.dumps(
        {"ok": False, "reason": FAILURE_REASON, "source": "url"},
        sort_keys=True,
    )


def test_missing_marker_still_fails_when_python_optimization_is_enabled(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "stale.html"
    local_path.write_text(REQUIRED_MARKERS[0], encoding="utf-8")
    code = (
        "from pathlib import Path; "
        "from scripts.check_recording_ui import run; "
        f"raise SystemExit(run([], local_path=Path({str(local_path)!r})))"
    )

    completed = subprocess.run(
        [sys.executable, "-O", "-c", code],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "ok": False,
        "reason": FAILURE_REASON,
        "source": "local",
    }
