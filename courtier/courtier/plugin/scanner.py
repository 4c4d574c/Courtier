"""PluginScanner — scan plugins/ directory and parse plugin.yaml manifests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from .manifest import KNOWN_HOST_SERVICES, KNOWN_PERMISSIONS, PluginManifest

logger = logging.getLogger(__name__)


class ScanStatus(str, Enum):
    VALID = "VALID"
    BLOCKED = "BLOCKED"


@dataclass
class PluginScanResult:
    """Result of scanning a single plugin directory."""

    name: str
    dir: Path
    status: ScanStatus
    manifest: PluginManifest | None = None
    error: str | None = None

    def __repr__(self) -> str:
        return f"PluginScanResult(name={self.name!r}, status={self.status.value})"


class PluginScanner:
    """Scans a directory for plugin subdirectories with valid plugin.yaml manifests."""

    MANIFEST_FILE = "plugin.yaml"
    # API 2.0: standalone TCP plugins with token-authenticated handshake.
    # (1.0 was the stdio subprocess protocol, removed with the spawn path.)
    HOST_API_VERSION = "2.0"

    @staticmethod
    def _is_api_compatible(plugin_api: str, host_api: str) -> bool:
        """Check semver compatibility: same major, plugin minor <= host minor."""
        if plugin_api == host_api:
            return True
        try:
            p_major, p_minor = map(int, plugin_api.split("."))
            h_major, h_minor = map(int, host_api.split("."))
        except (ValueError, AttributeError):
            return plugin_api == host_api
        return p_major == h_major and p_minor <= h_minor

    def scan(self, plugins_dir: Path) -> list[PluginScanResult]:
        """Scan plugins_dir recursively for valid plugins.

        Returns results for ALL detected plugin directories,
        including those that failed validation (marked BLOCKED).
        Directories without a plugin.yaml are silently skipped.
        """
        if not plugins_dir.exists():
            raise FileNotFoundError(f"Plugins directory not found: {plugins_dir}")
        if not plugins_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {plugins_dir}")

        results: list[PluginScanResult] = []
        for manifest_path in sorted(plugins_dir.rglob(self.MANIFEST_FILE)):
            plugin_dir = manifest_path.parent
            results.append(self._scan_one(plugin_dir, manifest_path))
        return results

    def _scan_one(self, plugin_dir: Path, manifest_path: Path) -> PluginScanResult:
        """Parse and validate a single plugin's manifest."""
        name = plugin_dir.name

        # Parse YAML
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Invalid YAML in {self.MANIFEST_FILE}: {e}",
            )

        if raw is None:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Empty {self.MANIFEST_FILE}",
            )

        if not isinstance(raw, dict):
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"{self.MANIFEST_FILE} must contain a YAML mapping, got {type(raw).__name__}",
            )

        # Validate with Pydantic
        try:
            manifest = PluginManifest.model_validate(raw)
        except Exception as e:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Invalid manifest: {e}",
            )

        # Set directory reference
        manifest.dir = plugin_dir

        # Validate: host_services are known
        for svc in manifest.dependencies.host_services:
            if svc not in KNOWN_HOST_SERVICES:
                logger.warning(
                    "Plugin '%s' declares unknown host_service '%s'. Known: %s",
                    manifest.name,
                    svc,
                    sorted(KNOWN_HOST_SERVICES),
                )

        # Validate: permissions are known
        for perm in manifest.dependencies.permissions:
            if perm not in KNOWN_PERMISSIONS:
                logger.warning(
                    "Plugin '%s' declares unknown permission '%s'. Known: %s",
                    manifest.name,
                    perm,
                    sorted(KNOWN_PERMISSIONS),
                )

        # Validate: name matches directory
        if manifest.name != name:
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Manifest name '{manifest.name}' does not match directory name '{name}'",
            )

        # Validate: API version compatible
        if not self._is_api_compatible(manifest.api, self.HOST_API_VERSION):
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=(
                    f"API version incompatible: plugin requires '{manifest.api}', "
                    f"host is '{self.HOST_API_VERSION}'"
                ),
            )

        return PluginScanResult(
            name=manifest.name,
            dir=plugin_dir,
            status=ScanStatus.VALID,
            manifest=manifest,
        )
