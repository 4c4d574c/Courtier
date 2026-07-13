"""ProcessManager — manages plugin subprocess lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from courtier.agent.core.context_manager import ContextManager
from courtier.agent.telemetry.metrics import PLUGIN_STATE
from courtier.config import get_settings

from .client import JSONRPCClient, PluginCrashedError
from .manifest import PluginManifest
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_ARTIFACT_STORE_GET,
    METHOD_ARTIFACT_STORE_LIST,
    METHOD_ARTIFACT_STORE_PUT,
    METHOD_CACHE_LOAD,
    METHOD_CACHE_MICRO_COMPACT,
    METHOD_CACHE_PERSIST,
    METHOD_CACHE_RESOLVE,
    METHOD_HOST_SERVICES,
    METHOD_NOT_FOUND,
    METHOD_RUNTIME_CONTEXT,
)
from .registry import ExtensionRegistry
from .scanner import PluginScanResult

logger = logging.getLogger(__name__)

# Environment variables that may be resolved from ${ENV:VAR_NAME} references in
# plugin manifests. Restricting this list prevents a plugin manifest from
# exfiltrating database/LLM/cloud credentials from the host process.
_ALLOWED_MANIFEST_ENV_VARS: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "COURTIER_UPLOAD_DIR",
    "DOCAUDIT_UPLOAD_DIR",
    "UPLOAD_DIR",
    "COURTIER_REPO_ROOT",
})


def _resolve_plugin_entry_path(plugin_dir: Path, entry: str) -> Path:
    """Resolve a plugin entry point path and validate it stays within the plugin dir."""
    p = Path(entry)
    if p.is_absolute():
        raise ValueError(f"Plugin entry path must be relative: {entry}")
    if ".." in p.parts:
        raise ValueError(f"Plugin entry path must not contain '..': {entry}")
    resolved = (plugin_dir / p).resolve()
    try:
        resolved.relative_to(plugin_dir.resolve())
    except ValueError:
        raise ValueError(f"Plugin entry path escapes plugin directory: {entry}")
    return resolved


async def _micro_compact_dict_messages(
    messages: list[dict[str, Any]], cache_store: Any
) -> list[dict[str, Any]]:
    """Compact a list of OpenAI-style message dicts using the host ContextManager.

    Converts dicts to host Message models, runs micro_compact, and converts back.
    """
    from courtier.agent.core.state import Message, ToolCall

    def _to_message(m: dict[str, Any]) -> Message:
        data = dict(m)
        if data.get("role") == "assistant" and data.get("tool_calls"):
            tool_calls = []
            for tc in data["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        arguments=args,
                    )
                )
            data["tool_calls"] = tuple(tool_calls)
        return Message(**data)

    def _to_dict(m: Message) -> dict[str, Any]:
        d: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            d["content"] = m.content
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        if m.source is not None:
            d["source"] = m.source
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        return d

    model_messages = tuple(_to_message(m) for m in messages)
    cm = ContextManager(model=None, cache_store=cache_store, recent_tool_results=5)
    compacted = await cm.micro_compact(model_messages)
    return [_to_dict(m) for m in compacted]


# Regex for resolving ${ENV:VAR_NAME} patterns in manifest env values
_ENV_REF_RE = re.compile(r"\$\{ENV:([^}]+)\}")

# Patterns that indicate a plugin stderr line is an error worth surfacing
_ERROR_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^\s*File \".+\", line \d+"),
    re.compile(r"^[A-Za-z_]\w*(?:Error|Exception|Warning|Interrupt)"),
]


def _is_error_line(line: str) -> bool:
    """Return True if *line* looks like a traceback or error message."""
    return any(p.search(line) for p in _ERROR_PATTERNS)


# Window (seconds) after reaching ACTIVE within which a crash is treated as
# a deterministic startup failure -> FATAL with no restart attempts.
_IMMEDIATE_CRASH_WINDOW = 5.0


def _resolve_env(value: str) -> str:
    """Resolve ${ENV:VAR_NAME} references in a string value.

    Only variables explicitly listed in _ALLOWED_MANIFEST_ENV_VARS are
    resolved; all other references are left unchanged so secrets cannot be
    pulled into the plugin environment via manifest configuration.
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name not in _ALLOWED_MANIFEST_ENV_VARS:
            logger.warning(
                "Blocked manifest env reference to non-whitelisted variable: %s",
                var_name,
            )
            return match.group(0)
        return os.environ.get(var_name, "")

    return _ENV_REF_RE.sub(_replace, value)


class PluginState(str, Enum):
    SCANNED = "SCANNED"
    LOADING = "LOADING"
    REGISTERING = "REGISTERING"
    ACTIVE = "ACTIVE"
    CRASHED = "CRASHED"
    RESTARTING = "RESTARTING"
    FATAL = "FATAL"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass
class PluginProcess:
    """Handle for a single plugin subprocess."""

    name: str
    manifest: "PluginManifest"
    plugin_dir: Path
    state: PluginState = PluginState.SCANNED
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _client: JSONRPCClient | None = field(default=None, repr=False)
    _restart_count: int = field(default=0, repr=False)
    _health_failures: int = field(default=0, repr=False)
    _health_task: asyncio.Task | None = field(default=None, repr=False)
    _stderr_task: asyncio.Task | None = field(default=None, repr=False)
    _started_at: float = field(default=0.0, repr=False)

    @property
    def client(self) -> JSONRPCClient:
        if self._client is None:
            raise RuntimeError(
                f"Plugin '{self.name}' is not ready (state: {self.state.value})"
            )
        return self._client


class ProcessManager:
    """Manages the lifecycle of all plugin subprocesses.

    Parameters
    ----------
    plugin_dir : Path
        Root directory containing plugin subdirectories.
    extension_registry : ExtensionRegistry
        Registry to route plugin capabilities into.
    max_restarts : int
        Maximum consecutive restart attempts before marking FATAL.
    health_interval : float
        Seconds between health-check pings (default 30s).
    """

    def __init__(
        self,
        plugin_dir: Path,
        extension_registry: "ExtensionRegistry",
        max_restarts: int = 3,
        health_interval: float = 30.0,
        cache_store: Any = None,
        artifact_store: Any = None,
        artifact_store_registry: Any = None,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._extension_registry = extension_registry
        self._max_restarts = max_restarts
        self._health_interval = health_interval
        # ArtifactStore now subsumes CacheStore — prefer artifact_store.
        self._cache_store = artifact_store or cache_store
        self._artifact_store_registry = artifact_store_registry
        self._processes: dict[str, PluginProcess] = {}

    def get_processes(self) -> dict[str, PluginProcess]:
        """Return a copy of the process map keyed by plugin name."""
        return dict(self._processes)

    async def start_all(self, results: list[PluginScanResult]) -> None:
        """Start all valid plugins from scan results."""
        for result in results:
            if result.status.value != "VALID":
                continue
            if result.manifest is None:
                continue

            proc = PluginProcess(
                name=result.name,
                manifest=result.manifest,
                plugin_dir=result.dir,
            )
            self._processes[result.name] = proc
            try:
                await self._start_one(proc)
            except PluginCrashedError:
                # _on_crash is already handling the crash recovery;
                # only set FATAL if recovery didn't change the state.
                if proc.state not in (
                    PluginState.CRASHED,
                    PluginState.RESTARTING,
                    PluginState.ACTIVE,
                ):
                    proc.state = PluginState.FATAL
                    PLUGIN_STATE.labels(
                        plugin_name=proc.name, state=PluginState.FATAL.value
                    ).set(1)
            except Exception:
                logger.error(
                    "Failed to start plugin '%s'",
                    result.name,
                    exc_info=True,
                )
                proc.state = PluginState.FATAL
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.FATAL.value
                ).set(1)

        # Validate artifact contracts after all plugins are loaded so
        # the full producer graph is available — per-tool registration-time
        # validation would fire false positives due to load ordering.
        self._extension_registry.validate_all_contracts()

    async def _start_one(self, proc: PluginProcess) -> None:
        """Start a single plugin subprocess and wait for registration."""
        proc.state = PluginState.LOADING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.LOADING.value).set(
            1
        )

        entry = proc.manifest.runtime.entry if proc.manifest else "entry.py"
        try:
            entry_path = _resolve_plugin_entry_path(proc.plugin_dir, entry)
        except ValueError as exc:
            raise FileNotFoundError(str(exc)) from exc
        if not entry_path.exists():
            raise FileNotFoundError(f"Entry point not found: {entry_path}")

        # Build environment with ${ENV:VAR_NAME} resolution.
        # Only pass whitelisted host env vars to plugins — full env inheritance
        # would leak DB/LLM/MinIO credentials to plugin subprocesses.
        env_whitelist = {
            "PATH",
            "HOME",
            "USER",
            "TMPDIR",
            "TEMP",
            "TMP",
            "PYTHONPATH",
            "PYTHONUNBUFFERED",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "COURTIER_UPLOAD_DIR",
            "DOCAUDIT_UPLOAD_DIR",  # deprecated fallback
            "UPLOAD_DIR",
        }
        env = {k: v for k, v in os.environ.items() if k in env_whitelist}
        if proc.manifest and proc.manifest.runtime.env:
            for key, value in proc.manifest.runtime.env.items():
                env[key] = _resolve_env(value)

        # Add project root to PYTHONPATH so plugins can import from
        # packages/ (courtier.*) and src/ (docmodels compat).
        # proc.plugin_dir = {project_root}/packages/domains/docaudit/plugins/{category}/{name}
        project_root = str(proc.plugin_dir.parent.parent.parent.resolve())
        existing = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{project_root}:{existing}" if existing else project_root

        # Expose the repo root so plugins can resolve relative paths correctly.
        # The plugin subprocess CWD is the plugin directory, not the project
        # root, so Path.resolve() against a relative path yields a wrong result.
        # Prefer the explicit COURTIER_REPO_ROOT env var if already set.
        repo_root = os.environ.get("COURTIER_REPO_ROOT", project_root)
        env["COURTIER_REPO_ROOT"] = repo_root

        # Expose upload directory so sandboxed tools (parse_document,
        # annotate_document) can validate paths against the safe root.
        upload_dir = str(Path(get_settings().upload_dir).resolve())
        env["COURTIER_UPLOAD_DIR"] = upload_dir
        env["DOCAUDIT_UPLOAD_DIR"] = upload_dir  # deprecated fallback

        proc._process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            entry,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(proc.plugin_dir),
            env=env,
        )

        # CRITICAL: Any failure after subprocess creation must clean up the
        # child process to prevent zombie processes.  The finally block
        # below ensures cleanup on all error paths.
        try:
            # Create client with crash callback wired to _on_crash
            client_timeout = (
                proc.manifest.timeout_ms / 1000.0
                if proc.manifest
                else 30.0
            )
            proc._client = JSONRPCClient(
                reader=proc._process.stdout,
                writer=proc._process.stdin,
                plugin_name=proc.name,
                on_disconnect=lambda: self._on_crash(proc),
                default_timeout=client_timeout,
            )

            # Wait for register notification
            proc.state = PluginState.REGISTERING
            PLUGIN_STATE.labels(
                plugin_name=proc.name, state=PluginState.REGISTERING.value
            ).set(1)
            try:
                capabilities = await proc._client.wait_for_register(timeout=10.0)
            except asyncio.TimeoutError:
                proc.state = PluginState.FATAL
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.FATAL.value
                ).set(1)
                logger.error(
                    "Plugin '%s' did not send register notification within 10s, marking FATAL",
                    proc.name,
                )
                raise

            # Route capabilities to registries
            self._extension_registry.on_register(
                proc.name,
                proc._client,
                capabilities,
                system_prompt=proc._client._register_system_prompt,
            )
            proc.state = PluginState.ACTIVE
            PLUGIN_STATE.labels(
                plugin_name=proc.name, state=PluginState.ACTIVE.value
            ).set(1)
            proc._started_at = asyncio.get_event_loop().time()
            proc._restart_count = 0
            proc._health_failures = 0

            # Start stderr reader for crash detection and log forwarding
            proc._stderr_task = asyncio.create_task(self._monitor_stderr(proc))

            # Start periodic health check loop
            proc._health_task = asyncio.create_task(self._health_loop(proc))

            # Register host service handler and tell the plugin which services it may use.
            proc._client.set_host_request_handler(
                self._create_host_request_handler(proc)
            )
            await proc._client.notify(
                METHOD_HOST_SERVICES,
                {
                    "host_services": list(
                        proc.manifest.dependencies.host_services or []
                    ),
                    "permissions": list(proc.manifest.dependencies.permissions or []),
                },
            )

            # Send runtime context (DRUDGE.md, environment) so plugins can
            # inject project-level rules into their tool system prompts.
            await proc._client.notify(
                METHOD_RUNTIME_CONTEXT,
                _build_runtime_context(),
            )
        except Exception:
            # Clean up subprocess on any failure after process creation.
            # _kill_process handles the case where the process is already dead.
            await self._kill_process(proc)
            if proc._client is not None:
                proc._client.close()
                proc._client = None
            raise

    def _create_host_request_handler(
        self, proc: PluginProcess
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        """Create a handler for plugin-to-host JSON-RPC requests.

        The handler enforces the plugin's declared host_service dependencies
        and permissions.
        """
        manifest = proc.manifest
        perms = set(manifest.dependencies.permissions or [])
        host_services = set(manifest.dependencies.host_services or [])
        cache_store = self._cache_store
        artifact_registry = self._artifact_store_registry

        def _deny(code: int, message: str) -> dict[str, Any]:
            return {"error": {"code": code, "message": message}}

        async def handler(request: dict[str, Any]) -> dict[str, Any]:
            method = request.get("method", "")
            params = request.get("params", {})
            session_id = params.get("session_id")

            if method == METHOD_CACHE_PERSIST:
                if "cache" not in host_services or "write:cache" not in perms:
                    return _deny(INVALID_PARAMS, "Missing write:cache permission")
                if cache_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                result = await cache_store.persist(
                    params.get("data"),
                    params.get("tool_name", "plugin_tool"),
                    force=bool(params.get("force", False)),
                    label=params.get("label"),
                )
                return {
                    "data": result.data,
                    "ref_id": result.ref_id,
                    "persisted": result.persisted,
                }

            if method == METHOD_CACHE_LOAD:
                if "cache" not in host_services or "read:cache" not in perms:
                    return _deny(INVALID_PARAMS, "Missing read:cache permission")
                if cache_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                return cache_store.load(params.get("ref_id"))

            if method == METHOD_CACHE_RESOLVE:
                if "cache" not in host_services or "read:cache" not in perms:
                    return _deny(INVALID_PARAMS, "Missing read:cache permission")
                if cache_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                return cache_store.resolve_refs(params.get("kwargs", {}))

            if method == METHOD_CACHE_MICRO_COMPACT:
                if "cache" not in host_services:
                    return _deny(INVALID_PARAMS, "Cache host service not declared")
                if not ({"read:cache", "write:cache"} <= perms):
                    return _deny(
                        INVALID_PARAMS,
                        "Missing read:cache or write:cache permission",
                    )
                if cache_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                messages = params.get("messages", [])
                compacted = await _micro_compact_dict_messages(messages, cache_store)
                return compacted

            if method == METHOD_ARTIFACT_STORE_PUT:
                if (
                    "artifact_store" not in host_services
                    or "write:artifacts" not in perms
                ):
                    return _deny(INVALID_PARAMS, "Missing write:artifacts permission")
                if artifact_registry is None:
                    return _deny(
                        INTERNAL_ERROR, "Host artifact store registry not available"
                    )
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(
                        INVALID_PARAMS, f"No artifact store for session {session_id}"
                    )
                from courtier.agent.artifacts.models import Artifact

                artifact = Artifact(**params.get("artifact", {}))
                stored = store.put(artifact)
                return stored.model_dump() if stored is not None else None

            if method == METHOD_ARTIFACT_STORE_GET:
                if "artifact_store" not in host_services:
                    return _deny(
                        INVALID_PARAMS, "Artifact store host service not declared"
                    )
                if not ({"read:artifacts", "read:documents"} & perms):
                    return _deny(INVALID_PARAMS, "Missing read artifact permission")
                if artifact_registry is None:
                    return _deny(
                        INTERNAL_ERROR, "Host artifact store registry not available"
                    )
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(
                        INVALID_PARAMS, f"No artifact store for session {session_id}"
                    )
                artifact = store.get(params.get("artifact_id"))
                return artifact.model_dump() if artifact is not None else None

            if method == METHOD_ARTIFACT_STORE_LIST:
                if "artifact_store" not in host_services:
                    return _deny(
                        INVALID_PARAMS, "Artifact store host service not declared"
                    )
                if not ({"read:artifacts", "read:documents"} & perms):
                    return _deny(INVALID_PARAMS, "Missing read artifact permission")
                if artifact_registry is None:
                    return _deny(
                        INTERNAL_ERROR, "Host artifact store registry not available"
                    )
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(
                        INVALID_PARAMS, f"No artifact store for session {session_id}"
                    )
                return [a.model_dump() for a in store.list_all()]

            return _deny(METHOD_NOT_FOUND, f"Unknown host service method: {method}")

        return handler

    async def health_check(self, proc: PluginProcess) -> bool:
        """Check if a plugin is responsive.

        Supports both the new health response format
        ``{"status": "ok", "dependencies": {}}`` and the legacy
        ``"ok"`` string for backward compatibility.
        """
        try:
            result = await proc.client.call("plugin.health", timeout=5.0)
            if isinstance(result, dict):
                return result.get("status") == "ok"
            return result == "ok"
        except Exception:
            return False

    async def _monitor_stderr(self, proc: PluginProcess) -> None:
        """Read stderr for logging and crash detection.

        When stderr closes (EOF), the subprocess has exited.
        If the plugin is still in ACTIVE state at that point,
        treat it as an unexpected crash.
        """
        try:
            while proc._process and proc._process.stderr:
                line = await proc._process.stderr.readline()
                if not line:  # EOF — subprocess exited
                    if proc.state == PluginState.ACTIVE:
                        logger.warning(
                            "Plugin '%s' stderr closed unexpectedly (state=%s), treating as crash",
                            proc.name,
                            proc.state.value,
                        )
                        await self._on_crash(proc)
                    break
                text = line.decode().rstrip()
                if _is_error_line(text):
                    logger.warning("[plugin:%s] %s", proc.name, text)
        except Exception:
            if proc.state == PluginState.ACTIVE:
                await self._on_crash(proc)

    async def _health_loop(self, proc: PluginProcess) -> None:
        """Periodic health check loop.

        After 3 consecutive health check failures the plugin is
        restarted via ``_on_crash``.
        """
        while proc.state == PluginState.ACTIVE:
            await asyncio.sleep(self._health_interval)
            if proc.state != PluginState.ACTIVE:
                break
            try:
                healthy = await self.health_check(proc)
            except Exception:
                healthy = False

            if healthy:
                proc._health_failures = 0
                continue

            proc._health_failures += 1
            logger.warning(
                "Plugin '%s' health check failed (%d/3)",
                proc.name,
                proc._health_failures,
            )

            if proc._health_failures >= 3:
                logger.error(
                    "Plugin '%s' failed health check 3 times, restarting",
                    proc.name,
                )
                await self._on_crash(proc)
                break

    async def _on_crash(self, proc: PluginProcess) -> None:
        """Handle a plugin subprocess crash — cleanup, then attempt restart."""
        # Guard against re-entrancy: when the process exits during _start_one,
        # on_disconnect fires immediately AND wait_for_register raises, causing
        # two concurrent _on_crash calls.  Only the first one should proceed.
        if proc.state not in (
            PluginState.ACTIVE,
            PluginState.REGISTERING,
            PluginState.LOADING,
        ):
            return  # Already handled or shutting down
        # Atomically mark CRASHED to prevent re-entrant calls from passing the
        # guard above.
        proc.state = PluginState.CRASHED
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.CRASHED.value).set(
            1
        )

        # Cancel in-flight requests BEFORE unregistering so callers get a
        # clear PluginCrashedError instead of cryptic "tool not found" or
        # "agent not found" errors after the extension entries are removed.
        if proc._client is not None:
            try:
                await proc._client.cancel_pending()
            except Exception:
                logger.debug(
                    "Error cancelling pending requests for crashed plugin '%s'",
                    proc.name,
                    exc_info=True,
                )

        # Always unregister before any crash handling so registries never
        # retain stale entries — this must run before the circuit breaker
        # return below.
        self._extension_registry.on_unregister(proc.name)

        # Circuit breaker: if the plugin crashed within seconds of reaching
        # ACTIVE, it's a deterministic startup failure -- skip restart.
        if proc._started_at > 0:
            uptime = asyncio.get_event_loop().time() - proc._started_at
            if uptime < _IMMEDIATE_CRASH_WINDOW:
                proc.state = PluginState.FATAL
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.FATAL.value
                ).set(1)
                logger.error(
                    "Plugin '%s' crashed %.1fs after startup (< %.0fs window), marking FATAL",
                    proc.name,
                    uptime,
                    _IMMEDIATE_CRASH_WINDOW,
                )
                return

        logger.error(
            "Plugin '%s' crashed (restart %d/%d)",
            proc.name,
            proc._restart_count,
            self._max_restarts,
        )

        # Cancel background tasks
        if proc._health_task:
            proc._health_task.cancel()
            proc._health_task = None
        if proc._stderr_task:
            proc._stderr_task.cancel()
            proc._stderr_task = None

        # Clean up old process
        await self._kill_process(proc)
        if proc._client:
            proc._client.close()
            proc._client = None

        await self._attempt_restart(proc)

    async def _attempt_restart(self, proc: PluginProcess) -> None:
        """Attempt to restart a crashed plugin with exponential backoff.

        Calls _start_one; on failure, re-enters _on_crash which will
        decrement the restart budget or mark FATAL.
        """
        if proc._restart_count >= self._max_restarts:
            proc.state = PluginState.FATAL
            PLUGIN_STATE.labels(
                plugin_name=proc.name, state=PluginState.FATAL.value
            ).set(1)
            logger.error(
                "Plugin '%s' exceeded max restarts (%d), marking FATAL",
                proc.name,
                self._max_restarts,
            )
            return

        proc._restart_count += 1
        delay = min(1 * (2 ** (proc._restart_count - 1)), 30)
        proc.state = PluginState.RESTARTING
        PLUGIN_STATE.labels(
            plugin_name=proc.name, state=PluginState.RESTARTING.value
        ).set(1)
        await asyncio.sleep(delay)
        try:
            await self._start_one(proc)
        except Exception:
            logger.error("Plugin '%s' restart failed", proc.name, exc_info=True)
            await self._on_crash(proc)

    async def cancel_pending(self) -> None:
        """Cancel pending requests on all active plugin connections.

        Sends ``request.cancel`` notifications so plugins stop processing
        in-flight requests.  Does NOT shut down the plugins — they remain
        alive for future sessions.
        """
        for proc in self._processes.values():
            if proc.state == PluginState.ACTIVE and proc._client is not None:
                try:
                    await proc._client.cancel_pending()
                except Exception:
                    logger.debug(
                        "Error cancelling pending requests for plugin '%s'",
                        proc.name,
                        exc_info=True,
                    )

    async def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        for proc in self._processes.values():
            if proc.state in (PluginState.ACTIVE, PluginState.REGISTERING):
                proc.state = PluginState.STOPPING
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.STOPPING.value
                ).set(1)
                self._extension_registry.on_unregister(proc.name)

                # Cancel background tasks
                if proc._health_task:
                    proc._health_task.cancel()
                    proc._health_task = None
                if proc._stderr_task:
                    proc._stderr_task.cancel()
                    proc._stderr_task = None

                if proc._client:
                    await proc._client.notify("plugin.shutdown")

                try:
                    if proc._process:
                        await asyncio.wait_for(proc._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Plugin '%s' shutdown timeout, force killing", proc.name
                    )
                    await self._kill_process(proc)

                if proc._client:
                    proc._client.close()
                    proc._client = None

                proc.state = PluginState.STOPPED
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.STOPPED.value
                ).set(1)

    async def _kill_process(self, proc: PluginProcess) -> None:
        """Force kill a plugin subprocess with a timeout."""
        if proc._process and proc._process.returncode is None:
            try:
                proc._process.kill()
                await asyncio.wait_for(proc._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Plugin '%s' did not exit after SIGKILL; sending SIGKILL again",
                    proc.name,
                )
                try:
                    proc._process.kill()
                    await asyncio.wait_for(proc._process.wait(), timeout=2.0)
                except Exception:
                    logger.debug(
                        "Error force-killing plugin '%s' after timeout",
                        proc.name,
                        exc_info=True,
                    )
            except Exception:
                logger.debug(
                    "Error force-killing plugin '%s' (pid may have already exited)",
                    proc.name,
                    exc_info=True,
                )


def _build_runtime_context() -> dict[str, str]:
    """Build the runtime context sent to plugins after registration.

    Includes the project DRUDGE.md content, environment info, and artifact
    system instructions so plugin tools have the same project-level context
    as the main orchestrator agent.
    """
    ctx: dict[str, str] = {}

    # Environment
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx["environment"] = f"- 日期: {now}\n" f"- 平台: {platform.system().lower()}"

    # DRUDGE.md — project rules at the repo root
    drudge_md_path = Path(__file__).resolve().parent.parent.parent / "DRUDGE.md"
    try:
        if drudge_md_path.exists():
            ctx["drudge_md"] = drudge_md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass

    # Artifact system instructions for plugin tools
    ctx["artifact_instructions"] = (
        "主机管理的类型化工件（artifacts）可通过 host services 访问：\n"
        "- 使用 artifact_store.list 获取当前会话的工件列表\n"
        "- 使用 artifact_store.get 按 artifact_id 获取工件数据\n"
        "- $ref 缓存引用在工具调用参数中由系统自动解析，无需手动处理"
    )

    return ctx
