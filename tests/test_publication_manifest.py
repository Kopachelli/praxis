from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "PUBLICATION_MANIFEST.md"


def _publication_paths() -> set[str]:
    text = MANIFEST.read_text(encoding="utf-8")
    powershell = text.split("```powershell", 1)[1].split("```", 1)[0]
    commands = powershell.replace("`\n", " ").splitlines()
    paths: set[str] = set()
    for command in commands:
        command = command.strip()
        if not command.startswith("git add --"):
            continue
        paths.update(command.removeprefix("git add --").split())
    return paths


def test_every_explicit_publication_path_exists() -> None:
    missing = sorted(path for path in _publication_paths() if not (ROOT / path).exists())

    assert missing == []


def test_screenshot_generator_and_tests_are_published_together() -> None:
    paths = _publication_paths()

    assert "scripts/capture_submission_screenshots.py" in paths
    assert "tests" in paths
    assert (ROOT / "tests" / "test_capture_submission_screenshots.py").is_file()


def test_recording_preflight_and_tests_are_published_together() -> None:
    paths = _publication_paths()

    assert "scripts/check_recording_ui.py" in paths
    assert "tests" in paths
    assert (ROOT / "tests" / "test_check_recording_ui.py").is_file()


def test_plan_latency_verifier_and_tests_are_published_together() -> None:
    paths = _publication_paths()

    assert "scripts/check_plan_latency.py" in paths
    assert "tests" in paths
    assert (ROOT / "tests" / "test_check_plan_latency.py").is_file()
