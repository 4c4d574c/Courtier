"""Tests for admin restart semantics: re-scan before redial.

A blocked plugin (invalid plugin.yaml) must revive from an admin restart
once the manifest is fixed on disk — without restarting the host — and
restart must refuse with a clear reason while the manifest is still
broken.
"""

from pathlib import Path

import pytest

from courtier.plugin.manager import PluginBlockedError, PluginState, ProcessManager

VALID_MANIFEST = """\
name: demo
version: "0.1.0"
api: "2.0"
"""

# Missing required field → pydantic validation failure at scan time
# (BLOCKED, manifest=None; keyed by the directory name).
BROKEN_MANIFEST = """\
name: demo
api: "2.0"
"""


def _make_manager(plugin_dir: Path) -> ProcessManager:
    return ProcessManager(plugin_dir=plugin_dir, extension_registry=object())


def _write_manifest(plugin_dir: Path, content: str) -> None:
    plugin = plugin_dir / "demo"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text(content, encoding="utf-8")


class TestRestartRescan:
    async def test_blocked_plugin_stays_blocked_while_manifest_broken(self, tmp_path):
        _write_manifest(tmp_path, BROKEN_MANIFEST)
        manager = _make_manager(tmp_path)

        with pytest.raises(PluginBlockedError) as exc:
            await manager.restart_plugin("demo")

        # The refusal carries the scan error so admins see the cause.
        assert "Invalid manifest" in str(exc.value)

    async def test_fixed_manifest_revives_from_restart(self, tmp_path):
        _write_manifest(tmp_path, BROKEN_MANIFEST)
        manager = _make_manager(tmp_path)
        with pytest.raises(PluginBlockedError):
            await manager.restart_plugin("demo")

        _write_manifest(tmp_path, VALID_MANIFEST)
        state = await manager.restart_plugin("demo")

        # Manifest gate passed; the plugin is only BLOCKED for its missing
        # endpoint (none configured in this test), not for a broken manifest.
        assert state == PluginState.BLOCKED
        result = manager.get_scan_results()["demo"]
        assert result.manifest is not None
        assert result.status.value == "VALID"

    async def test_rescan_refreshes_version_of_known_plugin(self, tmp_path):
        _write_manifest(tmp_path, VALID_MANIFEST)
        manager = _make_manager(tmp_path)
        await manager.restart_plugin("demo")

        _write_manifest(tmp_path, VALID_MANIFEST.replace('version: "0.1.0"', 'version: "0.2.0"'))
        await manager.restart_plugin("demo")

        assert manager.get_scan_results()["demo"].manifest.version == "0.2.0"

    async def test_name_mismatch_plugin_revives_under_directory_name(self, tmp_path):
        """A mismatched manifest name must not fork the plugin's identity.

        Scan failures are keyed by the directory name, so the admin entry
        keeps its name across fix → restart and no ghost record survives
        under the previously declared (wrong) name.
        """
        _write_manifest(tmp_path, 'name: demo2\nversion: "0.1.0"\napi: "2.0"\n')
        manager = _make_manager(tmp_path)

        with pytest.raises(PluginBlockedError):
            await manager.restart_plugin("demo")
        assert manager.get_scan_results()["demo"].manifest is None

        _write_manifest(tmp_path, VALID_MANIFEST)
        state = await manager.restart_plugin("demo")

        assert state == PluginState.BLOCKED  # endpoint-missing only
        results = manager.get_scan_results()
        assert set(results) == {"demo"}  # no ghost under "demo2"
        assert results["demo"].status.value == "VALID"

    async def test_vanished_plugin_keeps_last_scan_record(self, tmp_path):
        _write_manifest(tmp_path, VALID_MANIFEST)
        manager = _make_manager(tmp_path)
        await manager.restart_plugin("demo")

        (tmp_path / "demo" / "plugin.yaml").unlink()
        await manager.restart_plugin("demo")

        # Merge-only rescan: no record is dropped, so the listing stays stable.
        assert "demo" in manager.get_scan_results()
