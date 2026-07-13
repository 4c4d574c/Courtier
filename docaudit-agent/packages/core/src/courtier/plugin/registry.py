"""ExtensionRegistry — routes plugin capabilities to existing registries."""

from __future__ import annotations

import logging
from typing import Any

from courtier.agent.tools.registry import ToolRegistry
from courtier.plugin.types import CheckerRegistryLike

from .client import JSONRPCClient
from .proxies import ProxyChecker, ProxyRoute, ProxyTool

logger = logging.getLogger(__name__)


class ExtensionRegistry:
    """Receives plugin.register notifications and injects proxy objects
    into the corresponding existing registries (ToolRegistry, CheckerRegistry).

    Tracks which capabilities were registered by which plugin so they
    can be cleanly removed on unregister (crash/shutdown).

    Route proxies are stored internally and exposed via :meth:`get_routes`
    for FastAPI mounting.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        checker_registry: CheckerRegistryLike | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._checker_registry = checker_registry
        # plugin_name → {cap_type: [names]}
        self._registrations: dict[str, dict[str, list[str]]] = {}
        # Internal registry for route proxies
        self._routes: dict[str, ProxyRoute] = {}
        self._system_prompts: dict[str, str] = {}

    def on_register(
        self,
        plugin_name: str,
        client: JSONRPCClient,
        capabilities: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> None:
        """Process capabilities from a plugin.register notification."""
        registrations: dict[str, list[str]] = {}

        for cap in capabilities:
            cap_type = cap.get("type", "")

            if cap_type == "tool":
                self._register_tool(plugin_name, client, cap)
                registrations.setdefault("tool", []).append(cap["name"])

            elif cap_type == "checker":
                self._register_checker(plugin_name, client, cap)
                registrations.setdefault("checker", []).append(cap["doc_type"])

            elif cap_type == "route":
                self._register_route(plugin_name, cap)
                registrations.setdefault("route", []).append(cap["prefix"])

            else:
                logger.warning(
                    "Plugin '%s' registered unknown capability type: %s",
                    plugin_name,
                    cap_type,
                )

        self._registrations[plugin_name] = registrations
        if system_prompt:
            self._system_prompts[plugin_name] = system_prompt
            logger.info(
                "Plugin '%s' provided system_prompt (%d chars)",
                plugin_name,
                len(system_prompt),
            )

        logger.info(
            "Plugin '%s' registered: %s",
            plugin_name,
            {k: len(v) for k, v in registrations.items()},
        )

    def validate_all_contracts(self) -> list[str]:
        """Validate artifact contracts across all registered plugin tools.

        Must be called after all plugins have been started so the full
        producer graph is available.  Returns warning messages for any
        contracts that cannot be satisfied.
        """
        if self._tool_registry is None:
            return []
        return self._tool_registry.validate_all_contracts()

    def on_unregister(self, plugin_name: str) -> None:
        """Remove all proxy objects registered by a plugin."""
        regs = self._registrations.pop(plugin_name, {})
        self._system_prompts.pop(plugin_name, None)
        if not regs:
            return

        for cap_type, names in regs.items():
            if cap_type == "tool" and self._tool_registry:
                for name in names:
                    try:
                        self._tool_registry.unregister(name)
                    except KeyError:
                        pass
            elif cap_type == "checker" and self._checker_registry:
                for doc_type in names:
                    try:
                        self._checker_registry.unregister(doc_type)
                    except KeyError:
                        pass
            elif cap_type == "route":
                for prefix in names:
                    self._routes.pop(prefix, None)

    def get_routes(self) -> dict[str, ProxyRoute]:
        """Return all plugin-provided route proxies for FastAPI mounting."""
        return dict(self._routes)

    def get_system_prompts(self) -> dict[str, str]:
        """Return all plugin system_prompts, keyed by plugin name.

        .. deprecated::
            Plugin system prompts are now consumed through
            ``register_capabilities()`` return values (plugin SDK path)
            or via ``METHOD_RUNTIME_CONTEXT`` notification (host path).
            The manifest ``system_prompt`` field is documentation-only.
            This method is retained for backward compatibility but has
            no active callers.
        """
        return dict(self._system_prompts)

    def _register_tool(
        self, plugin_name: str, client: JSONRPCClient, cap: dict
    ) -> None:
        if self._tool_registry is None:
            return
        proxy = ProxyTool(client, cap)
        try:
            self._tool_registry.register(proxy)
        except ValueError:
            logger.error(
                "Plugin '%s' tool '%s' conflicts with an existing tool; "
                "skipping registration",
                plugin_name,
                cap["name"],
            )

    def _register_checker(
        self, plugin_name: str, client: JSONRPCClient, cap: dict
    ) -> None:
        if self._checker_registry is None:
            return
        proxy = ProxyChecker(client, cap)
        self._checker_registry.register(proxy)

    def _register_route(self, plugin_name: str, cap: dict) -> None:
        prefix = cap["prefix"]
        if prefix in self._routes:
            logger.warning(
                "Duplicate route prefix '%s' from plugin '%s', overwriting",
                prefix,
                plugin_name,
            )
        proxy = ProxyRoute(cap)
        self._routes[prefix] = proxy
