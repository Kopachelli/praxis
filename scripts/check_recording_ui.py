"""Verify that the Praxis recording UI contains the required feature markers.

The default check reads ``ui/index.html`` from the repository and performs no
network access. Passing ``--url`` opts into one read-only HTTP GET. Output is a
fixed, credential-safe JSON envelope; response content, target URLs, headers,
and exception details are never printed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UI_PATH = REPOSITORY_ROOT / "ui" / "index.html"
REQUIRED_MARKERS = ("incident-announcement", "function formatSimilarity")
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
SUCCESS_REASON = "recording_ui_ready"
FAILURE_REASON = "recording_ui_not_ready"


class RecordingUiCheckError(RuntimeError):
    """An expected, deliberately non-disclosing preflight failure."""


def has_required_markers(document: str) -> bool:
    """Return whether every recording feature marker is present."""

    return all(marker in document for marker in REQUIRED_MARKERS)


def _load_local(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_url(
    url: str,
    *,
    get: Callable[..., Any] | None = None,
) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RecordingUiCheckError("invalid_url")

    if get is None:
        import httpx

        get = httpx.get

    response = get(
        url,
        headers={"Accept": "text/html, application/xhtml+xml"},
        follow_redirects=True,
        timeout=15.0,
    )
    response.raise_for_status()
    headers: Mapping[str, str] = response.headers
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in HTML_MEDIA_TYPES:
        raise RecordingUiCheckError("non_html_response")
    return str(response.text)


def _envelope(*, ok: bool, source: str) -> str:
    payload = {
        "ok": ok,
        "reason": SUCCESS_REASON if ok else FAILURE_REASON,
        "source": source,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        help="Opt into one read-only GET instead of checking local ui/index.html",
    )
    return parser.parse_args(argv)


def run(
    argv: Sequence[str] | None = None,
    *,
    local_path: Path = DEFAULT_UI_PATH,
    get: Callable[..., Any] | None = None,
) -> int:
    args = parse_args(argv)
    source = "url" if args.url is not None else "local"
    try:
        document = (
            _load_url(args.url, get=get)
            if args.url is not None
            else _load_local(local_path)
        )
        ok = has_required_markers(document)
    except Exception:
        ok = False
    print(_envelope(ok=ok, source=source))
    return 0 if ok else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
