"""Tests for logging_config module."""

from __future__ import annotations

import logging

import pytest

from courtier.agent.core.logging_config import configure_logging, reset_logging
from courtier.agent.core.structured_log_handler import StructuredLogHandler


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset logging after each test to prevent handler leakage."""
    yield
    reset_logging()


class TestConfigureLogging:
    """Tests for configure_logging()."""

    def test_configure_logging_sets_level(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_configure_logging_default_level_is_info(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        assert logging.getLogger().level == logging.INFO

    def test_invalid_level_falls_back_to_info(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), level="INVALID_LEVEL")
        assert logging.getLogger().level == logging.INFO

    def test_configure_logging_adds_structured_handler(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        root = logging.getLogger()
        handlers = [
            h for h in root.handlers if isinstance(h, StructuredLogHandler)
        ]
        assert len(handlers) == 1

    def test_configure_logging_console_true_adds_stream_handler(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), console=True)
        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers if isinstance(h, logging.StreamHandler)
        ]
        assert len(stream_handlers) == 1

    def test_configure_logging_console_false_no_stream_handler(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), console=False)
        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers if isinstance(h, logging.StreamHandler)
        ]
        assert len(stream_handlers) == 0

    def test_configure_logging_removes_duplicate_handlers(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        first_count = len(logging.getLogger().handlers)
        configure_logging(log_dir=str(tmp_path))
        second_count = len(logging.getLogger().handlers)
        assert first_count == second_count

    def test_structured_log_file_created(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        assert (tmp_path / "structured_events.jsonl").exists()


class TestResetLogging:
    """Tests for reset_logging()."""

    def test_reset_logging_removes_all_handlers(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), console=True)
        assert len(logging.getLogger().handlers) > 0

        reset_logging()
        assert len(logging.getLogger().handlers) == 0

    def test_reset_logging_resets_level(self, tmp_path):
        configure_logging(log_dir=str(tmp_path), level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

        reset_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_reset_logging_is_idempotent(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        reset_logging()
        # Should not raise
        reset_logging()
        assert len(logging.getLogger().handlers) == 0


class TestNoisyLibsSuppressed:
    """Tests that noisy third-party loggers are suppressed."""

    def test_noisy_libs_set_to_warning(self, tmp_path):
        configure_logging(log_dir=str(tmp_path))
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("openai").level == logging.WARNING
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy").level == logging.WARNING
        # uvicorn and uvicorn.error are NOT suppressed — startup banner needs them
        assert logging.getLogger("uvicorn").level == logging.NOTSET
        assert logging.getLogger("uvicorn.error").level == logging.NOTSET
