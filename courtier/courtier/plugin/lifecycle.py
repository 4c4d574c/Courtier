"""PluginLifecycle — high-level lifecycle orchestration for plugin processes.

This module decouples plugin crash/recovery policy from the low-level
subprocess management in :class:`ProcessManager`.  It listens to
:class:`CapabilityRegistry` events so that capability unregistration
(e.g. caused by a crash) can trigger an automatic restart, and it tracks
restart budgets per plugin provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from courtier.agent.core.capability import Capability, CapabilityRegistry
from courtier.agent.telemetry.metrics import record_plugin_lifecycle_restart

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    """Health status reported by :meth:`PluginLifecycle.health_check`."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class PluginHandle:
    """Lightweight handle tracking a running plugin process."""

    provider: str
    process: Any  # PluginProcess, kept as Any to avoid a circular import
    restart_count: int = 0
    started_at: float = 0.0
    state: str = "active"


class PluginLifecycle:
    """Orchestrate plugin process lifecycle using CapabilityRegistry listeners.

    The lifecycle object maintains a mapping from *provider* (plugin name) to
    :class:`PluginHandle`.  When the registry fires an ``unregister`` event for
    a capability owned by a tracked provider, the lifecycle schedules a restart
    unless the provider has exceeded its restart budget or crashed immediately
    after startup.

    Parameters
    ----------
    capability_registry:
        Registry to subscribe to.  When provided, ``register``/``unregister``
        events are observed automatically.
    max_restarts:
        Maximum consecutive restart attempts before a provider is marked fatal.
    immediate_crash_window:
        Seconds after startup within which a crash is treated as a deterministic
        startup failure and not restarted.
    restart_callback:
        Async callable invoked as ``restart_callback(provider)`` when a restart
        should be attempted.  The caller is responsible for actually starting
        the subprocess and calling :meth:`track_process` with the new handle.
    """

    def __init__(
        self,
        capability_registry: CapabilityRegistry | None = None,
        max_restarts: int = 3,
        immediate_crash_window: float = 5.0,
        restart_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._max_restarts = max_restarts
        self._immediate_crash_window = immediate_crash_window
        self._restart_callback = restart_callback
        self._handles: dict[str, PluginHandle] = {}
        self._restart_tasks: dict[str, asyncio.Task] = {}
        self._registry = capability_registry

        if capability_registry is not None:
            capability_registry.add_listener(self._on_capability_event)

    def track_process(self, handle: PluginHandle) -> None:
        """Start tracking a plugin process handle."""
        self._handles[handle.provider] = handle
        logger.debug("PluginLifecycle tracking provider '%s'", handle.provider)

    def untrack_process(self, provider: str) -> PluginHandle | None:
        """Stop tracking a provider and return the previous handle if any."""
        handle = self._handles.pop(provider, None)
        if handle is None:
            return None
        self._cancel_pending_restart(provider)
        return handle

    def get_handle(self, provider: str) -> PluginHandle | None:
        """Return the tracked handle for *provider*, if any."""
        return self._handles.get(provider)

    def reset_health(self, provider: str) -> None:
        """Reset the restart budget for a provider after a healthy start."""
        handle = self._handles.get(provider)
        if handle is not None:
            handle.restart_count = 0
            handle.state = "active"
            logger.debug("PluginLifecycle reset health for provider '%s'", provider)

    def health_check(self, provider: str) -> HealthStatus:
        """Return the tracked health status for a provider."""
        handle = self._handles.get(provider)
        if handle is None:
            return HealthStatus.UNKNOWN
        if handle.state == "fatal":
            return HealthStatus.UNHEALTHY
        return HealthStatus.HEALTHY

    def should_restart(self, provider: str, started_at: float, restart_count: int) -> bool:
        """Return True if *provider* should be restarted after a crash."""
        if restart_count >= self._max_restarts:
            logger.error(
                "Plugin '%s' exceeded max restarts (%d), marking fatal",
                provider,
                self._max_restarts,
            )
            return False

        uptime = asyncio.get_event_loop().time() - started_at
        if started_at > 0 and uptime < self._immediate_crash_window:
            logger.error(
                "Plugin '%s' crashed %.1fs after startup (< %.0fs window), "
                "marking fatal",
                provider,
                uptime,
                self._immediate_crash_window,
            )
            return False

        return True

    async def schedule_restart(
        self,
        provider: str,
        *,
        restart_count: int = 0,
        delay: float | None = None,
    ) -> None:
        """Schedule a restart attempt for *provider* after an optional delay."""
        self._cancel_pending_restart(provider)

        handle = self._handles.get(provider)
        if handle is not None:
            handle.state = "restarting"
            handle.restart_count = restart_count

        async def _do_restart() -> None:
            try:
                if delay:
                    await asyncio.sleep(delay)
                if self._restart_callback is not None:
                    await self._restart_callback(provider)
                    record_plugin_lifecycle_restart(provider, "success")
            except asyncio.CancelledError:
                logger.debug("Restart task for provider '%s' cancelled", provider)
                raise
            except Exception:
                logger.exception("Restart failed for provider '%s'", provider)
                record_plugin_lifecycle_restart(provider, "failed")

        self._restart_tasks[provider] = asyncio.create_task(_do_restart())
        record_plugin_lifecycle_restart(provider, "scheduled")

    def mark_fatal(self, provider: str) -> None:
        """Mark a provider as fatal and cancel any pending restart."""
        handle = self._handles.get(provider)
        if handle is not None:
            handle.state = "fatal"
        record_plugin_lifecycle_restart(provider, "fatal")
        self._cancel_pending_restart(provider)

    def _on_capability_event(self, capability: Capability, event: str) -> None:
        """CapabilityRegistry listener callback."""
        provider = capability.provider
        if event == "unregister":
            # A capability was unregistered.  If the provider is still tracked
            # and active, treat this as a crash signal and schedule a restart.
            handle = self._handles.get(provider)
            if handle is None:
                return
            if handle.state not in ("active", "restarting", "crashed"):
                return
            if not self.should_restart(
                provider,
                started_at=handle.started_at,
                restart_count=handle.restart_count,
            ):
                self.mark_fatal(provider)
                return
            handle.restart_count += 1
            delay = min(1 * (2 ** (handle.restart_count - 1)), 30)
            logger.warning(
                "Plugin '%s' capability unregistered, scheduling restart %d/%d "
                "in %.1fs",
                provider,
                handle.restart_count,
                self._max_restarts,
                delay,
            )
            asyncio.create_task(self.schedule_restart(provider, delay=delay))

    def _cancel_pending_restart(self, provider: str) -> None:
        task = self._restart_tasks.pop(provider, None)
        if task is not None and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel all pending restart tasks and untrack every provider."""
        for provider in list(self._restart_tasks.keys()):
            self._cancel_pending_restart(provider)
        self._handles.clear()
