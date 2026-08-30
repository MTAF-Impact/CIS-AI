import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

_RESERVED_LOG_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter compatible with Google Cloud Logging's expected
    fields (`severity`, `message`, `timestamp`) so Cloud Run picks up log level and
    text correctly without extra configuration."""

    def format(self, record: logging.LogRecord) -> str:
        # Note: logging.Formatter.formatTime() delegates to time.strftime(), which does
        # NOT support %f (microseconds) - only datetime.strftime() does. Build it manually.
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "timestamp": timestamp,
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and key not in payload:
                try:
                    json.dumps(value)
                except TypeError:
                    value = str(value)
                payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Configure the root logger once, for both the app and any script (e.g. seeding)."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Idempotent: avoid duplicate handlers if called more than once (e.g. under --reload).
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
    root.addHandler(handler)

    # Quiet noisy third-party loggers unless we're at DEBUG.
    if level.upper() != "DEBUG":
        for noisy_logger in ("httpx", "httpcore", "sqlalchemy.engine", "urllib3"):
            logging.getLogger(noisy_logger).setLevel(logging.WARNING)


class Timer:
    """Small context manager for measuring elapsed time in milliseconds for log fields."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_args: object) -> None:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 2)
