"""Tests for plugin crash diagnostics: stderr tee and exit-code reporting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from courtier.plugin.manager import (
    _MAX_STDERR_LOG_BYTES,
    PluginState,
    ProcessManager,
)


def _manager(tmp_path):
    mgr = ProcessManager.__new__(ProcessManager)
    mgr._plugin_dir = tmp_path
    mgr._log_dir = tmp_path / "logs"
    mgr._processes = {}
    mgr._scan_results = {}
    mgr._lifecycle = None
    mgr._max_restarts = 3
    mgr._health_interval = 30.0
    mgr._artifact_store = None
    mgr._artifact_store_registry = None
    mgr._extension_registry = SimpleNamespace(on_unregister=lambda name: None)
    return mgr


class TestAppendStderrLog:
    def test_appends_timestamped_lines(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr._append_stderr_log("search", "first line")
        mgr._append_stderr_log("search", "second line")
        text = (tmp_path / "logs" / "search.log").read_text(encoding="utf-8")
        assert "first line" in text
        assert "second line" in text
        assert text.count("[") >= 2  # timestamps present

    def test_truncates_oversized_log(self, tmp_path):
        mgr = _manager(tmp_path)
        path = tmp_path / "logs" / "search.log"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * (_MAX_STDERR_LOG_BYTES + 1000))
        mgr._append_stderr_log("search", "tail-marker")
        size = path.stat().st_size
        assert size < _MAX_STDERR_LOG_BYTES
        assert "tail-marker" in path.read_text(encoding="utf-8")

    def test_never_raises_on_io_error(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr._log_dir = tmp_path / "blocked" / "nope"
        (tmp_path / "blocked").write_text("file, not a dir")
        mgr._append_stderr_log("search", "ignored")  # must not raise


class TestDescribeExitCode:
    def test_none_is_unknown(self):
        assert ProcessManager._describe_exit_code(None) == "unknown"

    def test_zero_and_positive(self):
        assert ProcessManager._describe_exit_code(0) == "0"
        assert ProcessManager._describe_exit_code(137) == "137"

    def test_negative_maps_to_signal(self):
        desc = ProcessManager._describe_exit_code(-9)
        assert "SIGKILL" in desc
        desc = ProcessManager._describe_exit_code(-15)
        assert "SIGTERM" in desc


class TestCrashDiagnostics:
    @pytest.mark.asyncio
    async def test_on_crash_records_exit_code_and_writes_marker(self, tmp_path, caplog):
        import asyncio

        mgr = _manager(tmp_path)
        mgr._max_restarts = 0  # FATAL path: no restart attempt, no spawn needed
        proc = SimpleNamespace(
            name="search",
            state=PluginState.ACTIVE,
            _process=SimpleNamespace(returncode=-9),
            _client=None,
            _health_task=None,
            _stderr_task=None,
            _restart_count=0,
            _started_at=1.0,
            _crash_lock=asyncio.Lock(),
        )
        mgr._processes["search"] = proc
        mgr._kill_process = lambda p: asyncio.sleep(0)

        with caplog.at_level("ERROR"):
            await mgr._on_crash(proc)

        assert proc.state == PluginState.FATAL
        assert any("exit=-9" in r.message and "SIGKILL" in r.message for r in caplog.records)
        marker = (tmp_path / "logs" / "search.log").read_text(encoding="utf-8")
        assert "crash detected" in marker
        assert "SIGKILL" in marker
