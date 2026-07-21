"""Single-line structured logging for Praxis and Uvicorn."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class PraxisJsonFormatter(logging.Formatter):
    """Emit every record with the incident and trace context required by NFR-4."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "incident_id": getattr(record, "incident_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for name in (
            "status_code",
            "error_type",
            "raw_body_bytes",
            "raw_body_sha256",
            "max_body_bytes",
            "observed_body_bytes",
            "limit_detection",
        ):
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        return json.dumps(payload, separators=(",", ":"))


def build_application_logger() -> logging.Logger:
    logger = logging.getLogger("praxis")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(PraxisJsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
