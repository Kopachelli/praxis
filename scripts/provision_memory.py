"""Provision and verify the accepted Tablestore memory schema [FR-10, FR-11]."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.memory import (
    EMBEDDING_DIMENSION,
    MEMORY_INDEX,
    MEMORY_TABLE,
    MemoryError,
    TablestoreMemoryBackend,
)
from app.config import Settings


async def _provision(settings: Settings) -> None:
    if settings.memory_backend != "tablestore":
        raise ValueError("MEMORY_BACKEND must be tablestore for provisioning")
    backend = TablestoreMemoryBackend(
        settings,
        allow_schema_changes=True,
        operation_timeout_seconds=15.0,
    )
    try:
        await backend.ensure_ready()
    finally:
        await backend.aclose()


def run(settings: Settings | None = None) -> int:
    try:
        active = settings or Settings.from_env()
        asyncio.run(_provision(active))
    except Exception:
        print(json.dumps({"ok": False, "reason": "memory_schema_unavailable"}))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "backend": "tablestore",
                "table": MEMORY_TABLE,
                "index": MEMORY_INDEX,
                "embedding_dimension": EMBEDDING_DIMENSION,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
