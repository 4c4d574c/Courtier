"""Centralized logging configuration for Courtier.

Configures the root logger with:

* A ``StructuredLogHandler`` that writes JSON Lines to the audit log directory.
* An optional ``StreamHandler`` for human-readable console output during
  development.

Must be called once at application startup, before any agent runs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .structured_log_handler import StructuredLogHandler

# Third-party libraries that are noisy at DEBUG/INFO — keep them at WARNING.
# NOTE: uvicorn.error is intentionally NOT suppressed — it prints the startup
# banner and shutdown messages, which are essential for operational visibility.
_NOISY_LIBS = (
    "httpx",
    "httpcore",
    "openai",
    "elasticsearch",
    "elastic_transport",
    "aiomysql",
    "asyncmy",
    "minio",
    "uvicorn.access",
    "urllib3",
    "asyncio",
    "sqlalchemy",
    "sqlalchemy.engine",
)


def configure_logging(
    log_dir: str | Path,
    level: str = "INFO",
    console: bool = False,
) -> None:
    """Configure the Python logging system.

    Args:
        log_dir: Directory for structured log output (e.g. ``.agent_logs``).
        level: Log level string — ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``,
            or ``CRITICAL``.
        console: If ``True``, also emit human-readable logs to stderr.
    """
    log_level = _resolve_level(level)

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove any pre-existing handlers to avoid duplicates when called
    # multiple times (e.g. test setup/teardown).
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)

    # Always add the structured JSON Lines handler.
    root.addHandler(
        StructuredLogHandler(log_dir=Path(log_dir), level=log_level)
    )

    # Optionally add a human-readable console handler.
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(console_handler)

    # Suppress noisy third-party loggers.
    for lib_name in _NOISY_LIBS:
        logging.getLogger(lib_name).setLevel(logging.WARNING)


def reset_logging() -> None:
    """Remove all handlers and reset the root logger to default.

    Intended for test teardown to prevent handler leakage between tests.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.setLevel(logging.WARNING)


def _resolve_level(level: str) -> int:
    """Convert a level string to a ``logging`` constant.

    Invalid strings fall back to ``INFO`` with a warning.
    """
    try:
        level_value: int = getattr(logging, level.upper())
        return level_value
    except AttributeError:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Invalid log level '%s', defaulting to INFO", level
        )
        return logging.INFO
