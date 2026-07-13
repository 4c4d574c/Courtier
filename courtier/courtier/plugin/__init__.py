"""Courtier Plugin System.

Provides a full-stack plugin system with subprocess isolation and
JSON-RPC over stdio communication.

See docs/architecture/plugin-skill-boundary.md for Skill vs Plugin guidance.

Usage:
    from courtier.plugin import PluginSystem

    plugin_system = PluginSystem(
        plugins_dir="plugins",
        tool_registry=tool_registry,
        checker_registry=checker_registry,
    )
    await plugin_system.start()
    # ... app runs ...
    await plugin_system.shutdown()
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .client import JSONRPCClient, PluginCrashedError, PluginRPCError
from .manager import PluginProcess, PluginState, ProcessManager
from .manifest import Capabilities, PluginManifest
from .protocol import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse
from .proxies import ProxyChecker, ProxyRoute, ProxyTool
from .registry import ExtensionRegistry
from .scanner import PluginScanner, PluginScanResult, ScanStatus

logger = logging.getLogger(__name__)

__all__ = [
    "PluginSystem",
    "PluginManifest",
    "Capabilities",
    "PluginScanner",
    "PluginScanResult",
    "ScanStatus",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "JSONRPCClient",
    "PluginRPCError",
    "PluginCrashedError",
    "ProcessManager",
    "PluginProcess",
    "PluginState",
    "ExtensionRegistry",
    "ProxyTool",
    "ProxyChecker",
    "ProxyRoute",
]


class PluginSystem:
    """Top-level orchestrator for the plugin system."""

    def __init__(
        self,
        plugins_dir: str | Path = "plugins",
        tool_registry: Any = None,
        checker_registry: Any = None,
        cache_store: Any = None,
        artifact_store: Any = None,
        artifact_store_registry: Any = None,
    ) -> None:
        self._plugins_dir = Path(plugins_dir)
        self._scanner = PluginScanner()
        self._registry = ExtensionRegistry(
            tool_registry=tool_registry,
            checker_registry=checker_registry,
        )
        self._manager = ProcessManager(
            plugin_dir=self._plugins_dir,
            extension_registry=self._registry,
            cache_store=cache_store,
            artifact_store=artifact_store,
            artifact_store_registry=artifact_store_registry,
        )
        self._started = False

    async def start(self) -> dict[str, str]:
        """Scan plugins directory and start all valid plugins."""
        if self._started:
            raise RuntimeError("PluginSystem already started")

        if not self._plugins_dir.exists():
            logger.info(
                "Plugins directory '%s' does not exist, creating it", self._plugins_dir
            )
            self._plugins_dir.mkdir(parents=True, exist_ok=True)

        results = self._scanner.scan(self._plugins_dir)
        logger.info(
            "Found %d plugin(s): %d valid, %d blocked",
            len(results),
            sum(1 for r in results if r.status == ScanStatus.VALID),
            sum(1 for r in results if r.status == ScanStatus.BLOCKED),
        )

        for r in results:
            if r.status == ScanStatus.BLOCKED:
                logger.warning("Plugin '%s' BLOCKED: %s", r.name, r.error)

        await self._manager.start_all(results)
        self._started = True

        status = {}
        for name, proc in self._manager.get_processes().items():
            status[name] = proc.state.value
        return status

    async def cancel_pending(self) -> None:
        """Cancel pending requests on all plugin connections.

        Called when a session is stopped via /stop.  Plugins stay alive
        for future sessions.
        """
        await self._manager.cancel_pending()

    async def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        if not self._started:
            return
        await self._manager.shutdown()
        self._started = False

    def get_status(self) -> dict[str, dict]:
        """Return current status of all plugins."""
        status = {}
        for name, proc in self._manager.get_processes().items():
            status[name] = {
                "state": proc.state.value,
                "version": proc.manifest.version if proc.manifest else "unknown",
                "restart_count": proc._restart_count,
            }
        return status

    def get_system_prompts(self) -> dict[str, str]:
        """Return all plugin system_prompts, keyed by plugin name."""
        return self._registry.get_system_prompts()
