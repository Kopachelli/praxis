from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath

import pytest


ROOT = Path(__file__).resolve().parents[1]
FCIGNORE = ROOT / ".fcignore"


def _is_ignored(path: str) -> bool:
    """Evaluate the ordered gitignore-style rules used by this manifest."""

    candidate = PurePosixPath(path)
    ignored = False
    for raw_rule in FCIGNORE.read_text(encoding="utf-8").splitlines():
        rule = raw_rule.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if "/" in pattern:
            matches = fnmatch.fnmatchcase(candidate.as_posix(), pattern)
            if pattern.startswith("**/"):
                matches = matches or fnmatch.fnmatchcase(
                    candidate.as_posix(), pattern.removeprefix("**/")
                )
        else:
            matches = any(
                fnmatch.fnmatchcase(part, pattern) for part in candidate.parts
            )
        if matches:
            ignored = not negated
    return ignored


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".git/config",
        ".venv/Lib/site-packages/httpx/__init__.py",
        ".fc-cli-work/trace.log",
        ".browser-shot-profile/Default/Cookies",
        ".qwen/settings.json",
        ".serena/cache.json",
        ".codex/session.json",
        ".agents/state.json",
        "graphify-out/graph.json",
        ".pytest_cache/v/cache/nodeids",
        ".pytest-tmp/session/output.txt",
        "app/__pycache__/main.cpython-310.pyc",
        "local-private-key.pem",
        "docs/PRD.md",
        "tests/test_health.py",
        "scripts/fc.py",
        "deploy/s.yaml",
    ],
)
def test_fc_package_excludes_sensitive_and_development_paths(path: str) -> None:
    assert _is_ignored(path), f"expected .fcignore to exclude {path}"


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "requirements.txt",
        "app/main.py",
        "app/agent/client.py",
        "app/uvicorn_log_config.json",
        "ui/index.html",
        "python/bin/uvicorn",
        "python/lib/python3.10/site-packages/fastapi/__init__.py",
        # The certifi CA bundle must survive the *.pem rule; without it the FC
        # controller crashes at boot on ssl.load_verify_locations (PRAXIS-151).
        "python/certifi/cacert.pem",
    ],
)
def test_fc_package_retains_application_and_runtime_paths(path: str) -> None:
    assert not _is_ignored(path), f"expected .fcignore to retain {path}"
