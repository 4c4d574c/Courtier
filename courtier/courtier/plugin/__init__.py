"""Courtier Plugin System.

Plugins are standalone TCP services with their own lifecycle (compose,
systemd, dev runner); the host dials them over newline-delimited JSON-RPC
with a mutual token handshake.  See
docs/architecture/plugin-skill-boundary.md for Skill vs Plugin guidance.

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
from .manager import PluginBlockedError, PluginProcess, PluginState, ProcessManager
from .protocol import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse
from .proxies import ProxyChecker, ProxyRoute, ProxyTool
from .registry import ExtensionRegistry
from .scanner import PluginScanner, PluginScanResult, ScanStatus

logger = logging.getLogger(__name__)

__all__ = [
    "PluginSystem",
    "PluginManifest",
    "PluginScanner",
    "PluginScanResult",
    "ScanStatus",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "JSONRPCClient",
    "PluginRPCError",
    "PluginCrashedError",
    "PluginBlockedError",
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
        artifact_store: Any = None,
        artifact_store_registry: Any = None,
        endpoints: dict[str, tuple[str, int]] | None = None,
        token: str | None = None,
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
            artifact_store=artifact_store,
            artifact_store_registry=artifact_store_registry,
            endpoints=endpoints,
            token=token,
            scanner=self._scanner,
        )
        self._started = False

    async def start(self) -> dict[str, str]:
        """Scan plugin manifests and dial all endpoints (non-blocking).

        Tools register as each plugin connects; startup never blocks on
        plugin availability.
        """
        if self._started:
            raise RuntimeError("PluginSystem already started")

        if not self._plugins_dir.exists():
            logger.info("Plugins directory '%s' does not exist, creating it", self._plugins_dir)
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
        """Cancel pending requests on all active plugin connections.

        Called when a session is stopped via /stop.  Plugins stay connected
        for future sessions.
        """
        await self._manager.cancel_pending()

    async def notify_plugin(self, name: str, method: str, params: dict | None = None) -> None:
        """Send a fire-and-forget notification to one live plugin."""
        await self._manager.notify(name, method, params)

    async def broadcast(self, method: str, params: dict | None = None) -> None:
        """Notify every live plugin; single-plugin failures are logged and
        skipped (notifications are advisory, e.g. cache invalidation)."""
        from .manager import PluginState

        for name, proc in self._manager.get_processes().items():
            if proc.state == PluginState.ACTIVE:
                await self._manager.notify(name, method, params)

    async def shutdown(self) -> None:
        """Disconnect all plugin channels (the plugins keep running)."""
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
                "restart_count": proc._reconnect_count,
            }
        return status

    def get_system_prompts(self) -> dict[str, str]:
        """Return plugin_name → system_prompt for all live plugins.

        Delegates to ExtensionRegistry; used to inject plugin-provided tool
        usage guidance into agent system prompts.
        """
        return self._registry.get_system_prompts()

    def get_tool_summaries(self) -> dict[str, list[dict[str, str]]]:
        """Return plugin_name → tool summaries for all live plugins.

        Delegates to ExtensionRegistry; consumed by the admin extensions
        listing (tools are runtime-registered, not manifest-declared).
        """
        return self._registry.get_plugin_tool_summaries()

    def plugin_has_endpoint(self, name: str) -> bool:
        """Whether COURTIER_PLUGIN_ENDPOINTS / injected config covers the plugin.

        Used by admin surfaces to explain a runtime-BLOCKED state (a valid
        manifest that simply has no dial target).
        """
        return self._manager.has_endpoint(name)

    def get_scan_results(self):
        """Return the last scan results (valid + blocked) keyed by name."""
        return self._manager.get_scan_results()

    def plugin_domain(self, name: str) -> str | None:
        """Return the domain a plugin belongs to, or None for shared plugins.

        Derived from the plugin's directory layout: ``plugins/shared/<name>``
        belongs to no domain; ``plugins/<domain>/<name>`` and
        ``plugins/<domain>/<subdir>/<name>`` (e.g. ``plugins/docaudit/audit/``)
        belong to ``<domain>``.
        """
        result = self._manager.get_scan_results().get(name)
        if result is None:
            return None
        try:
            rel = result.dir.relative_to(self._plugins_dir)
        except ValueError:
            # Scanning may have used a differently-anchored base path.
            logger.debug("Plugin '%s' dir %s is outside plugins root", name, result.dir)
            return None
        if not rel.parts:
            return None
        first = rel.parts[0]
        return None if first == "shared" else first

    async def start_plugin(self, name: str) -> str:
        """(Re)connect a stopped/blocked plugin; return its current state."""
        state = await self._manager.start_plugin(name)
        return state.value

    async def stop_plugin(self, name: str) -> str:
        """Disconnect a plugin; return its final state."""
        state = await self._manager.stop_plugin(name)
        return state.value

    async def restart_plugin(self, name: str) -> str:
        """Drop the connection and redial; return the current state."""
        state = await self._manager.restart_plugin(name)
        return state.value
