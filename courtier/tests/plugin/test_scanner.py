"""Tests for PluginScanner."""

from pathlib import Path

import pytest

from courtier.plugin.scanner import PluginScanner, ScanStatus

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


class TestPluginScanner:
    def test_scan_empty_directory(self, tmp_path):
        scanner = PluginScanner()
        results = scanner.scan(tmp_path)
        assert results == []

    def test_scan_finds_valid_plugin(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        echo_result = next((r for r in results if r.name == "echo_plugin"), None)
        assert echo_result is not None
        assert echo_result.status == ScanStatus.VALID
        assert echo_result.manifest is not None
        assert echo_result.manifest.name == "echo_plugin"
        assert echo_result.manifest.version == "0.1.0"
        assert echo_result.manifest.api == "2.0"
        assert echo_result.error is None

    def test_legacy_capabilities_block_is_blocked(self):
        """A manifest with a capabilities block fails validation outright.

        The manifest model no longer declares capabilities (dead config
        since runtime registration); ``extra="forbid"`` turns any leftover
        block into a scan-time BLOCKED instead of silently tolerated data.
        """
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.name == "bad_capabilities_block"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert bad.manifest is None
        assert "capabilities" in bad.error

    def test_no_manifest_directory_is_skipped(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        names = {r.name for r in results}
        assert "bad_no_manifest" not in names

    def test_name_mismatch_is_blocked(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.dir.name == "bad_name_mismatch"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert "directory name" in bad.error.lower()
        # Directory is the stable identity even when validation fails:
        # admin restart keeps addressing the plugin by its directory name.
        assert bad.name == bad.dir.name == "bad_name_mismatch"

    def test_api_mismatch_is_blocked(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.name == "bad_api_mismatch"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert "API version incompatible" in bad.error
        assert bad.name == "bad_api_mismatch"

    def test_invalid_yaml_is_blocked(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.name == "bad_invalid_yaml"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert bad.error is not None

    def test_scanner_finds_nested_plugins(self, tmp_path):
        """Plugins nested under common/ and audit/ subdirs are discovered."""
        (tmp_path / "common" / "parse").mkdir(parents=True)
        (tmp_path / "common" / "parse" / "plugin.yaml").write_text(
            'name: parse\nversion: "0.1.0"\napi: "2.0"\n', encoding="utf-8"
        )
        (tmp_path / "audit" / "format_audit").mkdir(parents=True)
        (tmp_path / "audit" / "format_audit" / "plugin.yaml").write_text(
            'name: format_audit\nversion: "0.1.0"\napi: "2.0"\n', encoding="utf-8"
        )

        scanner = PluginScanner()
        results = scanner.scan(tmp_path)

        names = {r.name for r in results if r.status == ScanStatus.VALID}
        assert names == {"parse", "format_audit"}

    def test_scanner_finds_deeply_nested_plugins(self, tmp_path):
        """Arbitrarily nested plugin.yaml files are discovered via rglob."""
        (tmp_path / "common" / "group" / "deep").mkdir(parents=True)
        (tmp_path / "common" / "group" / "deep" / "plugin.yaml").write_text(
            'name: deep\nversion: "0.1.0"\napi: "2.0"\n', encoding="utf-8"
        )

        scanner = PluginScanner()
        results = scanner.scan(tmp_path)

        names = {r.name for r in results if r.status == ScanStatus.VALID}
        assert "deep" in names

    def test_nonexistent_directory_raises(self):
        scanner = PluginScanner()
        with pytest.raises(FileNotFoundError):
            scanner.scan(Path("/nonexistent/path/12345"))

    def test_scan_result_repr(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)
        echo = next(r for r in results if r.name == "echo_plugin")
        repr_str = repr(echo)
        assert "echo_plugin" in repr_str
        assert "VALID" in repr_str
