"""Render deterministic, explicitly non-live Praxis submission screenshots.

The fixture server exercises the real ``ui/index.html`` renderer on localhost.
It never imports the application, reads ``.env``, calls a provider or cloud API,
or accepts a state-changing request. Every frame carries a capture-only label so
the generated images cannot be mistaken for deployed evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UI_PATH = REPOSITORY_ROOT / "ui" / "index.html"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "docs" / "screenshots" / "submission"
CAPTURE_SIZE = (1800, 1200)
DISCLOSURE = "LOCAL SEEDED UI · POST-DEADLINE · NOT LIVE PROOF"
FIXTURE_CONNECTION_LABEL = "Local fixture"


def _entry(
    seq: int,
    event_type: str,
    content: Any,
    minute: int,
    *,
    model: str | None = None,
    tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "type": event_type,
        "content": content,
        "model_used": model,
        "tokens": tokens,
        "timestamp": f"2026-07-21T10:{minute:02d}:00Z",
    }


def _plan() -> dict[str, Any]:
    return {
        "status": "proposed",
        "steps": [
            {
                "seq": 1,
                "action": "Restart the isolated Function Compute demo target",
                "tool": "restart_service",
                "args": {"service": "praxis-demo-target"},
                "risk_level": "safe",
                "rollback": "Verify the new boot ID; restore the prior FC revision if health checks regress.",
            },
            {
                "seq": 2,
                "action": "Propose a temporary upstream timeout increase",
                "tool": "update_config",
                "args": {"key": "upstream_timeout_seconds", "value": 45},
                "risk_level": "caution",
                "rollback": "DRY RUN only — retain the current 30-second value.",
            },
        ],
    }


def _base_incident(
    incident_id: str,
    *,
    state: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "id": incident_id,
        "source": "sentry",
        "service": "checkout-api",
        "severity": "high",
        "signal": "upstream_timeout",
        "title": "Checkout API latency exceeded SLO",
        "state": state,
        "created_at": created_at,
        "memory_match": None,
        "plan": _plan(),
        "trail": [],
        "trace_id": f"trace-{incident_id}",
    }


def build_fixtures() -> dict[str, dict[str, Any]]:
    """Return the three deterministic presentation states."""

    awaiting_id = "inc_demo_awaiting_20260721"
    resolved_id = "inc_demo_resolved_20260721"
    recurrence_id = "inc_demo_recurrence_20260721"

    awaiting = _base_incident(
        awaiting_id,
        state="AWAITING_APPROVAL",
        created_at="2026-07-21T10:20:00Z",
    )
    awaiting["trail"] = [
        _entry(
            1,
            "thought",
            {
                "stage": "classification",
                "classification": "service degradation",
                "confidence": "94%",
            },
            20,
            model="qwen-flash",
            tokens=146,
        ),
        _entry(
            2,
            "tool_call",
            {"tool": "fetch_logs", "args": {"service": "checkout-api", "minutes": 15}},
            21,
        ),
        _entry(
            3,
            "tool_result",
            {
                "tool": "fetch_logs",
                "summary": "Worker saturation followed a burst of upstream timeouts; no credential data retained.",
            },
            21,
        ),
        _entry(
            4,
            "thought",
            {
                "stage": "root_cause_reasoning",
                "hypothesis": "A saturated worker pool is amplifying upstream timeout retries.",
            },
            22,
            model="qwen3.7-max",
            tokens=684,
        ),
        _entry(
            5,
            "qwen_attempt",
            {
                "provider": "qwencloud",
                "model": "qwen3.7-max",
                "outcome": "success",
                "reason": "success",
                "trace_id": f"trace-{awaiting_id}",
            },
            22,
            model="qwen3.7-max",
        ),
        _entry(
            6,
            "thought",
            {"stage": "plan_validation", "status": "accepted", "steps": 2},
            23,
        ),
    ]

    resolved = _base_incident(
        resolved_id,
        state="RESOLVED",
        created_at="2026-07-21T10:30:00Z",
    )
    resolved["trail"] = [
        _entry(
            1,
            "thought",
            {"stage": "classification", "classification": "service degradation", "confidence": "95%"},
            30,
            model="qwen-flash",
            tokens=139,
        ),
        _entry(
            2,
            "thought",
            {
                "stage": "root_cause_reasoning",
                "hypothesis": "The isolated worker pool is unhealthy after sustained upstream timeout retries.",
            },
            31,
            model="qwen3.7-max",
            tokens=701,
        ),
        _entry(
            3,
            "qwen_attempt",
            {
                "provider": "qwencloud",
                "model": "qwen3.7-max",
                "outcome": "success",
                "reason": "success",
                "trace_id": f"trace-{resolved_id}",
            },
            31,
            model="qwen3.7-max",
        ),
        _entry(
            4,
            "approval",
            {"decision": "approve", "operator": "demo-operator", "scope": "exact proposed plan"},
            32,
        ),
        _entry(
            5,
            "execution",
            {
                "status": "attempted",
                "step": 1,
                "tool": "restart_service",
                "target": "praxis-demo-target",
            },
            32,
        ),
        _entry(
            6,
            "execution",
            {
                "status": "succeeded",
                "step": 1,
                "tool": "restart_service",
                "target": "praxis-demo-target",
                "before_boot_id": "boot-7c21",
                "after_boot_id": "boot-a84f",
                "http_status": 202,
            },
            33,
        ),
        _entry(
            7,
            "execution",
            {
                "status": "succeeded",
                "step": 2,
                "tool": "update_config",
                "dry_run": True,
                "result": "DRY RUN — no configuration changed",
            },
            33,
        ),
        _entry(
            8,
            "thought",
            {"stage": "memory_write", "status": "succeeded", "backend": "tablestore"},
            34,
        ),
    ]

    recurrence = _base_incident(
        recurrence_id,
        state="AWAITING_APPROVAL",
        created_at="2026-07-21T10:40:00Z",
    )
    recurrence["memory_match"] = {
        "incident_id": resolved_id,
        "similarity": 0.92,
        "summary": "Checkout API worker saturation after an upstream timeout burst.",
        "resolution": "Owner-approved restart of the isolated demo target restored a healthy boot ID.",
    }
    recurrence["trail"] = [
        _entry(
            1,
            "thought",
            {"stage": "classification", "classification": "recurrent service degradation", "confidence": "96%"},
            40,
            model="qwen-flash",
            tokens=151,
        ),
        _entry(
            2,
            "thought",
            {
                "stage": "memory_recall",
                "status": "matched",
                "incident_id": resolved_id,
                "similarity": "92%",
                "backend": "tablestore",
            },
            41,
        ),
        _entry(
            3,
            "thought",
            {
                "stage": "root_cause_reasoning",
                "hypothesis": "The signal matches the prior worker-saturation incident; verify before repeating its approved remedy.",
            },
            42,
            model="qwen3.7-max",
            tokens=612,
        ),
        _entry(
            4,
            "qwen_attempt",
            {
                "provider": "qwencloud",
                "model": "qwen3.7-max",
                "outcome": "success",
                "reason": "success",
                "trace_id": f"trace-{recurrence_id}",
            },
            42,
            model="qwen3.7-max",
        ),
        _entry(
            5,
            "thought",
            {"stage": "plan_validation", "status": "accepted", "steps": 2},
            43,
        ),
    ]

    return {
        awaiting_id: awaiting,
        resolved_id: resolved,
        recurrence_id: recurrence,
    }


def inject_capture_disclosure(html: str) -> str:
    """Add an unmistakable capture-only label without changing product source."""

    style = """
    <style id="capture-disclosure-style">
      *, *::before, *::after {
        animation-duration: 0s !important; animation-delay: 0s !important;
        transition: none !important;
      }
      #capture-disclosure {
        position: fixed; right: 18px; bottom: 18px; z-index: 9999;
        padding: 9px 13px; border: 1px solid rgba(251, 191, 36, .55);
        border-radius: 999px; background: rgba(17, 24, 39, .94);
        color: #fcd34d; font: 700 11px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace;
        letter-spacing: .08em; box-shadow: 0 10px 30px rgba(0, 0, 0, .35);
      }
    </style>
    """
    capture_script = '<script id="capture-static-clock">window.setInterval = () => 0;</script>'
    banner = f'<div id="capture-disclosure" role="note">{DISCLOSURE}</div>'
    if "</head>" not in html or "</body>" not in html:
        raise ValueError("UI document is missing expected head/body closing tags")
    capture_html = html.replace(
        '<span id="connection-text">Connecting</span>',
        f'<span id="connection-text">{FIXTURE_CONNECTION_LABEL}</span>',
    ).replace(
        'setConnection("online", "Live");',
        f'setConnection("online", "{FIXTURE_CONNECTION_LABEL}");',
    )
    return capture_html.replace("</head>", f"{style}{capture_script}</head>", 1).replace(
        "</body>", f"{banner}</body>", 1
    )


def _summaries(fixtures: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": incident["id"],
            "title": incident["title"],
            "service": incident["service"],
            "severity": incident["severity"],
            "state": incident["state"],
            "created_at": incident["created_at"],
        }
        for incident in reversed(fixtures.values())
    ]


def make_handler(
    html_bytes: bytes,
    fixtures: dict[str, dict[str, Any]],
) -> type[BaseHTTPRequestHandler]:
    """Build a read-only localhost handler bound to immutable fixture data."""

    summaries = _summaries(fixtures)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, html_bytes, "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path == "/incidents":
                self._json(200, {"incidents": summaries, "trace_id": "trace-local-capture"})
                return
            if path.startswith("/incidents/"):
                incident_id = unquote(path.removeprefix("/incidents/"))
                incident = fixtures.get(incident_id)
                if incident is None:
                    self._json(404, {"detail": "Incident not found", "trace_id": "trace-local-capture"})
                    return
                self._json(200, incident)
                return
            self._json(404, {"detail": "Not found", "trace_id": "trace-local-capture"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            self._json(
                405,
                {
                    "detail": "State-changing requests are disabled in the screenshot fixture",
                    "trace_id": "trace-local-capture",
                },
            )

        def log_message(self, _format: str, *args: object) -> None:
            return

    return Handler


def find_browser() -> Path:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for command in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Chrome or Edge is required for deterministic screenshot rendering")


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG: {path}")
    return struct.unpack(">II", data[16:24])


def _parse_tesseract_tsv(payload: str) -> list[dict[str, str]]:
    """Parse TSV literally so OCR quote characters cannot consume later rows."""

    lines = payload.splitlines()
    if not lines:
        return []
    columns = lines[0].split("\t")
    return [
        dict(zip(columns, line.split("\t", len(columns) - 1), strict=False))
        for line in lines[1:]
        if line
    ]


def _missing_ocr_markers(
    path: Path,
    markers: tuple[str, ...],
    positioned_markers: tuple[tuple[str, int, int, int, int], ...],
) -> tuple[str, ...]:
    """Optionally reject Chromium frames captured before their text painted."""

    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ()
    completed = subprocess.run(
        [tesseract, str(path), "stdout", "tsv"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return markers
    rows = _parse_tesseract_tsv(completed.stdout)
    if not rows:
        return markers
    normalized = re.sub(
        r"[^A-Z0-9]+",
        "",
        " ".join(row.get("text", "") for row in rows).upper(),
    )
    missing = [
        marker
        for marker in markers
        if re.sub(r"[^A-Z0-9]+", "", marker.upper()) not in normalized
    ]
    for word, minimum_left, maximum_left, minimum_top, maximum_top in positioned_markers:
        expected = re.sub(r"[^A-Z0-9]+", "", word.upper())
        found = False
        for row in rows:
            actual = re.sub(r"[^A-Z0-9]+", "", row.get("text", "").upper())
            try:
                left = int(row.get("left", "-1"))
                top = int(row.get("top", "-1"))
            except ValueError:
                continue
            if (
                actual == expected
                and minimum_left <= left <= maximum_left
                and minimum_top <= top <= maximum_top
            ):
                found = True
                break
        if not found:
            missing.append(
                f"{word}@x={minimum_left}-{maximum_left},y={minimum_top}-{maximum_top}"
            )
    return tuple(missing)


def capture_screenshots(output_dir: Path) -> list[Path]:
    fixtures = build_fixtures()
    html = inject_capture_disclosure(UI_PATH.read_text(encoding="utf-8"))
    handler = make_handler(html.encode("utf-8"), fixtures)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output_dir.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    host, port = server.server_address
    captures = [
        (
            "03-local-awaiting-approval.png",
            "inc_demo_awaiting_20260721",
            (
                "Praxis",
                "Remediation plan",
                "Human approval checkpoint",
                "Decision trail",
            ),
            (
                ("Praxis", 0, 180, 0, 100),
                ("Checkout", 550, 900, 120, 260),
                ("Human", 550, 900, 850, 1050),
                ("Decision", 550, 900, 1050, 1190),
            ),
        ),
        (
            "04-local-approved-resolution.png",
            "inc_demo_resolved_20260721",
            ("Praxis", "Resolved", "Remediation plan", "Decision trail"),
            (
                ("Praxis", 0, 180, 0, 100),
                ("Checkout", 550, 900, 120, 260),
                ("Remediation", 550, 900, 330, 470),
                ("Decision", 550, 900, 850, 1050),
            ),
        ),
        (
            "05-local-memory-recurrence.png",
            "inc_demo_recurrence_20260721",
            (
                "Praxis",
                "Prior incident memory",
                "Similar resolved incident",
                "Remediation plan",
            ),
            (
                ("Praxis", 0, 180, 0, 100),
                ("Checkout", 550, 900, 120, 260),
                ("Prior", 550, 900, 330, 470),
                ("Similar", 550, 900, 430, 560),
            ),
        ),
    ]
    generated: list[Path] = []
    try:
        for filename, incident_id, ocr_markers, positioned_markers in captures:
            destination = (output_dir / filename).resolve()
            diagnostics: list[str] = []
            for attempt in range(1, 7):
                with tempfile.TemporaryDirectory(prefix="praxis-capture-") as profile:
                    attempt_destination = Path(profile) / filename
                    command = [
                        str(browser),
                        "--headless=new",
                        "--disable-gpu",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-extensions",
                        "--disable-sync",
                        "--hide-scrollbars",
                        "--metrics-recording-only",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--run-all-compositor-stages-before-draw",
                        "--safebrowsing-disable-auto-update",
                        f"--user-data-dir={profile}",
                        f"--window-size={CAPTURE_SIZE[0]},{CAPTURE_SIZE[1]}",
                        "--force-device-scale-factor=1",
                        "--virtual-time-budget=5000",
                        f"--screenshot={attempt_destination}",
                        f"http://{host}:{port}/?incident={incident_id}",
                    ]
                    completed = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=45,
                    )
                    if completed.returncode != 0 or not attempt_destination.is_file():
                        diagnostic = (
                            completed.stderr or completed.stdout or "unknown browser failure"
                        ).strip()
                        diagnostics.append(f"attempt {attempt}: {diagnostic[:250]}")
                        continue
                    dimensions = png_dimensions(attempt_destination)
                    if dimensions != CAPTURE_SIZE:
                        diagnostics.append(
                            f"attempt {attempt}: dimensions={dimensions[0]}x{dimensions[1]}"
                        )
                        continue
                    missing_markers = _missing_ocr_markers(
                        attempt_destination,
                        ocr_markers,
                        positioned_markers,
                    )
                    if missing_markers:
                        diagnostics.append(
                            f"attempt {attempt}: missing OCR markers={','.join(missing_markers)}"
                        )
                        continue
                    shutil.copyfile(attempt_destination, destination)
                    break
            else:
                raise RuntimeError(
                    f"Screenshot render never stabilized for {filename}: {'; '.join(diagnostics)}"
                )
            generated.append(destination)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for the three generated PNG files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for path in capture_screenshots(args.output_dir):
        width, height = png_dimensions(path)
        print(f"generated={path} dimensions={width}x{height} label={DISCLOSURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
