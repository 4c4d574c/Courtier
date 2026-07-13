"""Tests for StructuredLogHandler."""

from __future__ import annotations

import json
import logging
import threading

import pytest

from courtier.agent.core.structured_log_handler import StructuredLogHandler, StructuredLogEntry
from courtier.agent.core.logging_config import reset_logging


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset logging after each test to prevent handler leakage."""
    yield
    reset_logging()


class TestStructuredLogEntry:
    """Tests for the StructuredLogEntry dataclass."""

    def test_is_frozen(self):
        entry = StructuredLogEntry(
            timestamp="2026-01-01T00:00:00+00:00",
            level="INFO",
            logger="test.module",
            message="hello",
            module="test_module",
            function="test_func",
            line=42,
            exc_text=None,
        )
        with pytest.raises(Exception):
            entry.message = "changed"  # type: ignore[misc]

    def test_all_fields_present(self):
        entry = StructuredLogEntry(
            timestamp="2026-01-01T00:00:00+00:00",
            level="ERROR",
            logger="a.b.c",
            message="oh no",
            module="c",
            function="do_stuff",
            line=99,
            exc_text="Traceback...",
        )
        d = {f: getattr(entry, f) for f in entry.__dataclass_fields__}
        assert d["timestamp"] == "2026-01-01T00:00:00+00:00"
        assert d["level"] == "ERROR"
        assert d["logger"] == "a.b.c"
        assert d["message"] == "oh no"
        assert d["module"] == "c"
        assert d["function"] == "do_stuff"
        assert d["line"] == 99
        assert d["exc_text"] == "Traceback..."


class TestStructuredLogHandler:
    """Tests for the StructuredLogHandler."""

    def test_emit_writes_json_line(self, tmp_path):
        handler = StructuredLogHandler(log_dir=str(tmp_path))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        logger = logging.getLogger("test_emit")
        logger.info("hello world")

        handler.close()
        root.removeHandler(handler)

        lines = (tmp_path / "structured_events.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["level"] == "INFO"
        assert entry["logger"] == "test_emit"
        assert entry["message"] == "hello world"
        assert entry["module"] == "test_structured_log_handler"
        assert "timestamp" in entry

    def test_emit_includes_exception(self, tmp_path):
        handler = StructuredLogHandler(log_dir=str(tmp_path))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        logger = logging.getLogger("test_exc")
        try:
            raise ValueError("bad value")
        except ValueError:
            logger.exception("something went wrong")

        handler.close()
        root.removeHandler(handler)

        lines = (tmp_path / "structured_events.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["level"] == "ERROR"
        assert "ValueError" in entry["exc_text"]
        assert "bad value" in entry["exc_text"]

    def test_emit_handles_different_levels(self, tmp_path):
        handler = StructuredLogHandler(log_dir=str(tmp_path))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        logger = logging.getLogger("test_levels")
        logger.debug("debug msg")
        logger.info("info msg")
        logger.warning("warning msg")
        logger.error("error msg")

        handler.close()
        root.removeHandler(handler)

        lines = (tmp_path / "structured_events.jsonl").read_text().strip().split("\n")
        levels = [json.loads(line)["level"] for line in lines]
        assert levels == ["DEBUG", "INFO", "WARNING", "ERROR"]

    def test_creates_parent_directory(self, tmp_path):
        log_dir = tmp_path / "nested" / "logs"
        handler = StructuredLogHandler(log_dir=str(log_dir))
        assert log_dir.exists()
        assert (log_dir / "structured_events.jsonl").exists()
        handler.close()

    def test_close_is_idempotent(self, tmp_path):
        handler = StructuredLogHandler(log_dir=str(tmp_path))
        handler.close()
        # Should not raise
        handler.close()

    def test_thread_safety(self, tmp_path):
        """Multiple threads emitting concurrently should not corrupt JSON."""
        handler = StructuredLogHandler(log_dir=str(tmp_path))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        errors: list[Exception] = []

        def log_messages(thread_id):
            try:
                logger = logging.getLogger(f"thread_{thread_id}")
                for i in range(50):
                    logger.info("message %d from thread %d", i, thread_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=log_messages, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        handler.close()
        root.removeHandler(handler)

        assert not errors

        # Every line must be valid JSON
        lines = (tmp_path / "structured_events.jsonl").read_text().strip().split("\n")
        assert len(lines) == 200  # 4 threads × 50 messages
        for line in lines:
            json.loads(line)  # should not raise

    def test_respects_log_level(self, tmp_path):
        handler = StructuredLogHandler(log_dir=str(tmp_path), level=logging.WARNING)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        logger = logging.getLogger("test_filter")
        logger.debug("debug msg")
        logger.info("info msg")
        logger.warning("warning msg")

        handler.close()
        root.removeHandler(handler)

        lines = (tmp_path / "structured_events.jsonl").read_text().strip().split("\n")
        levels = [json.loads(line)["level"] for line in lines]
        assert levels == ["WARNING"]  # DEBUG and INFO filtered by handler level
