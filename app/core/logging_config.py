import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any, Self

_RESERVED_LOG_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JSONFormatter(logging.Formatter):
    """JSON formatter with the severity/message/timestamp fields Cloud Logging expects."""

    def format(self, record: logging.LogRecord) -> str:
        # time.strftime() (used by the base formatTime) doesn't support %f - build manually.
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime(
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
    """Configure the root logger once, for both the app and any script."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    root.handlers.clear()  # idempotent under --reload

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
    """Context manager measuring elapsed time in milliseconds."""

    def __enter__(self) -> Self:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_args: object) -> None:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 2)
