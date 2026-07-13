"""StructuredLogHandler — JSON Lines handler for Python's logging framework.

Writes every log event as one JSON object (one line) to a ``.jsonl`` file
under ``.agent_logs/``.  This bridges the 37+ modules that already use
``logger = logging.getLogger(__name__)`` into the structured audit-logging
directory without any code changes in those modules.
"""

from __future__ import annotations

import atexit
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StructuredLogEntry:
    """A single structured log event written to the JSON Lines file."""

    timestamp: str
    level: str
    logger: str
    message: str
    module: str
    function: str
    line: int
    exc_text: str | None


class StructuredLogHandler(logging.Handler):
    """A logging handler that writes JSON Lines to a file.

    One JSON object per line — easy to tail, grep, and ingest into log
    aggregators.  Thread safety is provided by the stdlib ``Handler.lock``.

    Usage::

        handler = StructuredLogHandler(log_dir=".agent_logs")
        logging.getLogger().addHandler(handler)
    """

    def __init__(
        self,
        log_dir: str | Path = ".agent_logs",
        *,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._log_dir / "structured_events.jsonl"
        self._file = self._file_path.open("a", encoding="utf-8")
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        """Serialize *record* as one JSON line and append to the file."""
        lock = self.lock
        if lock is None:
            return
        try:
            entry = StructuredLogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                level=record.levelname,
                logger=record.name,
                message=self.format(record),
                module=record.module,
                function=record.funcName,
                line=record.lineno,
                exc_text=self._format_exc(record),
            )
            with lock:
                self._file.write(
                    json.dumps(
                        _serialize_dataclass(entry),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self._file.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Flush and close the underlying file."""
        lock = self.lock
        if lock is None:
            super().close()
            return
        with lock:
            if not self._file.closed:
                self._file.flush()
                self._file.close()
        super().close()

    @staticmethod
    def _format_exc(record: logging.LogRecord) -> str | None:
        """Extract formatted exception text, if any."""
        if record.exc_info:
            # Do NOT pass record so we avoid recursion in format() calls
            return logging.Formatter().formatException(record.exc_info)
        return None


def _serialize_dataclass(obj: Any) -> dict[str, Any]:
    """Serialize a frozen dataclass to a plain dict."""
    result: dict[str, Any] = {}
    for field_name in obj.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(obj, field_name)
        result[field_name] = value
    return result
