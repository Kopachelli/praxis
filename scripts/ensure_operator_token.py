"""Create the ignored local operator bearer token without printing its value."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

TOKEN_NAME = "PRAXIS_OPERATOR_TOKEN"
TOKEN_BYTES = 48
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def ensure_operator_token(path: Path) -> bool:
    """Return True only when a new token was written to ``path``."""

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if line.startswith(f"{TOKEN_NAME}=")
    ]
    if len(matches) > 1:
        raise RuntimeError(f"{TOKEN_NAME} appears more than once")
    if matches and lines[matches[0]].split("=", 1)[1]:
        return False

    token = secrets.token_urlsafe(TOKEN_BYTES)
    token_line = f"{TOKEN_NAME}={token}"
    if matches:
        lines[matches[0]] = token_line
    else:
        if lines and lines[-1]:
            lines.append("")
        lines.append(token_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def run() -> int:
    created = ensure_operator_token(REPOSITORY_ROOT / ".env")
    print(json.dumps({"created": created, "ok": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
