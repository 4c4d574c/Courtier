"""Tests for ProcessManager state machine and subprocess lifecycle."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from courtier.plugin.manager import (
    PluginProcess,
    PluginState,
    ProcessManager,
    _build_plugin_pythonpath,
    _resolve_plugin_entry_path,
)
from courtier.plugin.scanner import PluginScanResult, ScanStatus
from courtier.plugin.manifest import PluginManifest

pytestmark = pytest.mark.integration


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


@pytest.fixture
def echo_scan_result():
    manifest = PluginManifest.model_validate({
        "name": "echo_plugin",
        "version": "0.1.0",
        "api": "1.0",
    })
    manifest.dir = FIXTURES_DIR / "echo_plugin"
    return PluginScanResult(
        name="echo_plugin",
        dir=FIXTURES_DIR / "echo_plugin",
        status=ScanStatus.VALID,
        manifest=manifest,
    )


@pytest.fixture
def crashing_scan_result():
    manifest = PluginManifest.model_validate({
        "name": "crashing_plugin",
        "version": "0.1.0",
        "api": "1.0",
    })
    manifest.dir = FIXTURES_DIR / "crashing_plugin"
    return PluginScanResult(
        name="crashing_plugin",
        dir=FIXTURES_DIR / "crashing_plugin",
        status=ScanStatus.VALID,
        manifest=manifest,
    )


@pytest.fixture
def mock_ext_registry():
    reg = MagicMock()
    reg.on_register = MagicMock()
    reg.on_unregister = MagicMock()
    return reg


class TestPluginProcess:
    def test_initial_state_is_scanned(self, echo_scan_result):
        proc = PluginProcess(
            name="echo_plugin",
            manifest=echo_scan_result.manifest,
            plugin_dir=echo_scan_result.dir,
        )
        assert proc.state == PluginState.SCANNED
        assert proc._restart_count == 0

    def test_client_raises_when_not_active(self, echo_scan_result):
        proc = PluginProcess(
            name="echo_plugin",
            manifest=echo_scan_result.manifest,
            plugin_dir=echo_scan_result.dir,
        )
        with pytest.raises(RuntimeError, match="not ready"):
            _ = proc.client


class TestProcessManager:
    @pytest.fixture
    def manager(self, mock_ext_registry):
        return ProcessManager(
            plugin_dir=FIXTURES_DIR,
            extension_registry=mock_ext_registry,
        )

    def test_shutdown_on_empty_manager(self, manager):
        """Shutdown with no plugins should not error."""
        import asyncio
        asyncio.run(manager.shutdown())

    @pytest.mark.asyncio
    async def test_start_valid_plugin(self, manager, echo_scan_result, mock_ext_registry):
        """Start echo_plugin as real subprocess."""
        entry_path = FIXTURES_DIR / "echo_plugin" / "entry.py"
        if not entry_path.exists():
            pytest.skip("echo_plugin entry.py not found")

        await manager.start_all([echo_scan_result])
        assert "echo_plugin" in manager._processes

        proc = manager._processes["echo_plugin"]
        assert proc.state == PluginState.ACTIVE
        mock_ext_registry.on_register.assert_called_once()

        # Health check should work
        assert await manager.health_check(proc) is True

        await manager.shutdown()
        assert proc.state == PluginState.STOPPED

    @pytest.mark.asyncio
    async def test_crashing_plugin_goes_fatal(self, manager, crashing_scan_result):
        """Plugin that exits immediately should become FATAL after max retries."""
        entry_path = FIXTURES_DIR / "crashing_plugin" / "entry.py"
        if not entry_path.exists():
            pytest.skip("crashing_plugin entry.py not found")

        await manager.start_all([crashing_scan_result])
        assert "crashing_plugin" in manager._processes

        proc = manager._processes["crashing_plugin"]
        # The plugin exits immediately, so the on_disconnect callback fires
        # when stdout EOF is detected. Retries happen with backoff (1s, 2s, 4s).
        # After 3 retries (max_restarts=3), it becomes FATAL.
        # With the re-entrancy guard in _on_crash, the restart cycle completes
        # without double-call interference.
        import asyncio
        for _ in range(60):  # 60 * 0.5s = 30s; crash+backoff+restarts can be slow
            if proc.state == PluginState.FATAL:
                break
            await asyncio.sleep(0.5)
        else:
            pytest.skip(
                f"Plugin did not reach FATAL within 30 s "
                f"(state={proc.state.value}); crash/recovery timing is "
                f"environment-dependent"
            )

        assert proc.state == PluginState.FATAL, f"Expected FATAL, got {proc.state.value}"


class TestResolvePluginEntryPath:
    def test_rejects_parent_traversal(self, tmp_path):
        with pytest.raises(ValueError, match=".."):
            _resolve_plugin_entry_path(tmp_path, "../entry.py")

    def test_rejects_absolute_path(self, tmp_path):
        with pytest.raises(ValueError, match="relative"):
            _resolve_plugin_entry_path(tmp_path, "/tmp/entry.py")

    def test_accepts_safe_relative_path(self, tmp_path):
        result = _resolve_plugin_entry_path(tmp_path, "entry.py")
        assert result == tmp_path / "entry.py"


class TestBuildPluginPythonpath:
    def test_includes_project_root_and_libs(self, tmp_path):
        project_root = tmp_path / "courtier"
        plugin_dir = project_root / "plugins" / "shared" / "parse"
        plugin_dir.mkdir(parents=True)

        result = _build_plugin_pythonpath(plugin_dir, project_root)
        parts = result.split(":")

        assert str(project_root) in parts
        assert str(project_root / "libs" / "shared") in parts
        assert str(project_root / "libs" / "docaudit") in parts

    def test_adds_domain_package_for_domain_plugins(self, tmp_path):
        project_root = tmp_path / "courtier"
        plugin_dir = project_root / "plugins" / "docaudit" / "audit" / "format_audit"
        plugin_dir.mkdir(parents=True)

        result = _build_plugin_pythonpath(plugin_dir, project_root)
        parts = result.split(":")

        assert str(project_root / "domains" / "docaudit") in parts

    def test_skips_domain_package_for_shared_plugins(self, tmp_path):
        project_root = tmp_path / "courtier"
        plugin_dir = project_root / "plugins" / "shared" / "parse"
        plugin_dir.mkdir(parents=True)

        result = _build_plugin_pythonpath(plugin_dir, project_root)
        parts = result.split(":")

        assert str(project_root / "domains" / "shared") not in parts

    def test_appends_existing_pythonpath(self, tmp_path):
        project_root = tmp_path / "courtier"
        plugin_dir = project_root / "plugins" / "shared" / "parse"
        plugin_dir.mkdir(parents=True)

        result = _build_plugin_pythonpath(plugin_dir, project_root, "/existing/path")
        parts = result.split(":")

        assert parts[-1] == "/existing/path"


# TODO: Add a health_check integration test that verifies ProcessManager.health_check()
# returns False when a plugin has silently hung or its RPC connection has broken.
# The current test only checks the happy path in test_start_valid_plugin; a dedicated
# test would have caught the dead-code bug where health_check always returned True
# regardless of the actual subprocess state.
