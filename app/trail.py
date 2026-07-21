"""Thread-safe decision-trail storage for incident timelines [FR-4, FR-13]."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any

from pydantic import BaseModel


class TrailEntryType(str, Enum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"
    FALLBACK = "fallback"
    QWEN_ATTEMPT = "qwen_attempt"
    EXECUTION = "execution"
    LIFECYCLE = "lifecycle"


class DecisionTrailEntry(BaseModel):
    incident_id: str
    seq: int
    type: TrailEntryType
    content: Any
    model_used: str | None = None
    tokens: int | None = None
    timestamp: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionTrailStore:
    """Keep ordered trail entries behind a narrow repository interface."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._entries: dict[str, list[DecisionTrailEntry]] = defaultdict(list)
        self._lock = RLock()

    def append(
        self,
        incident_id: str,
        entry_type: TrailEntryType,
        content: Any,
        *,
        model_used: str | None = None,
        tokens: int | None = None,
    ) -> DecisionTrailEntry:
        with self._lock:
            entry = DecisionTrailEntry(
                incident_id=incident_id,
                seq=len(self._entries[incident_id]) + 1,
                type=entry_type,
                content=copy.deepcopy(content),
                model_used=model_used,
                tokens=tokens,
                timestamp=self._clock(),
            )
            result = entry.model_copy(deep=True)
            self._entries[incident_id].append(entry)
            return result

    def list_for_incident(self, incident_id: str) -> list[DecisionTrailEntry]:
        with self._lock:
            return [
                entry.model_copy(deep=True) for entry in self._entries[incident_id]
            ]
