"""ProcessManager — manages plugin subprocess lifecycle."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from courtier_plugin_sdk.protocol import STREAM_LIMIT_BYTES

from courtier.agent.core.context_manager import ContextManager
from courtier.agent.telemetry.metrics import PLUGIN_STATE
from courtier.config import get_settings
from courtier.storage import client as storage_client

from .client import JSONRPCClient, PluginCrashedError
from .lifecycle import PluginHandle, PluginLifecycle
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
    METHOD_STORAGE_PUT,
    METHOD_TEMPLATE_STORE_GET,
)
from .registry import ExtensionRegistry
from .scanner import PluginScanResult

# Presigned download URLs handed to plugins for stored outputs live this long
# (S3 SigV4 presigned URLs are capped at 7 days).
_STORAGE_URL_EXPIRES_SECONDS = 7 * 24 * 3600


def _find_project_root(plugin_dir: Path) -> Path:
    """Walk up from plugin_dir to find the project root.

    The project root is identified by the presence of both ``pyproject.toml``
    and a ``courtier/`` package directory. Plugin directories themselves
    contain ``pyproject.toml`` but not ``courtier/``, so the search continues
    upward past them.
    """
    path = plugin_dir.resolve()
    while path != path.parent:
        if (path / "pyproject.toml").is_file() and (path / "courtier").is_dir():
            return path
        path = path.parent
    return plugin_dir.resolve()


logger = logging.getLogger(__name__)

# Environment variables that may be resolved from ${ENV:VAR_NAME} references in
# plugin manifests. Restricting this list prevents a plugin manifest from
# exfiltrating database/cloud credentials from the host process. The LLM_* and
# DOCPARSE_OCR_* entries are required by the parse plugin's scanned-document
# pipeline, CEC_* by text_correction's correction model, and ES_* by the
# search plugin's chunk index access; manifests are first-party (shipped in
# plugins/) and plugin subprocesses already run with host OS permissions, so
# exposing endpoint credentials to declaring plugins is accepted. DB/MinIO/cloud
# credentials remain excluded.
_ALLOWED_MANIFEST_ENV_VARS: frozenset[str] = frozenset(
    {
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
        "LLM_IP",
        "LLM_API_KEY",
        "LLM_NAME",
        "DOCPARSE_OCR_API_URL",
        "DOCPARSE_OCR_LANG",
        "DOCPARSE_OCR_ENGINE",
        "DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE",
        "DOCPARSE_OCR_DESKEW",
        "CEC_API_BASE",
        "CEC_API_KEY",
        "CEC_MODEL_NAME",
        "CEC_MAX_LENGTH",
        "CEC_USER_DICT",
        "ES_HOSTS",
        "ES_INDEX_CHUNKS",
        "ES_USERNAME",
        "ES_PASSWORD",
    }
)

# ${ENV:VAR} references to these variables fall back to the host Settings
# singleton when the variable is absent from os.environ. pydantic-settings
# reads .env into the Settings object without writing os.environ, so without
# this bridge manifests could only reference shell-exported variables and
# .env-only configuration would silently resolve to empty strings.
_SETTINGS_ENV_FALLBACK: dict[str, str] = {
    "LLM_IP": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_NAME": "llm_model",
    "DOCPARSE_OCR_API_URL": "docparse_ocr_api_url",
    "DOCPARSE_OCR_LANG": "docparse_ocr_lang",
    "DOCPARSE_OCR_ENGINE": "docparse_ocr_engine",
    "DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE": "docparse_ocr_max_image_long_side",
    "DOCPARSE_OCR_DESKEW": "docparse_ocr_deskew",
    "CEC_API_BASE": "cec_api_base",
    "CEC_API_KEY": "cec_api_key",
    "CEC_MODEL_NAME": "cec_model_name",
    "CEC_MAX_LENGTH": "cec_max_length",
    "CEC_USER_DICT": "cec_user_dict",
    "ES_HOSTS": "es_hosts",
    "ES_INDEX_CHUNKS": "es_index_chunks",
    "ES_USERNAME": "es_username",
    "ES_PASSWORD": "es_password",
}


def _settings_env_fallback(var_name: str) -> str:
    """Resolve *var_name* from the host Settings singleton, or "" if unmapped/empty."""
    attr = _SETTINGS_ENV_FALLBACK.get(var_name)
    if not attr:
        return ""
    from courtier.config import get_settings

    return str(getattr(get_settings(), attr, "") or "")


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
    messages: list[dict[str, Any]], artifact_store: Any
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
    cm = ContextManager(model=None, artifact_store=artifact_store)
    # The plugin explicitly requested compaction — skip the budget gate.
    compacted = await cm.micro_compact(model_messages, force=True)
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
_MAX_STDERR_LOG_BYTES = 1_000_000


def _resolve_env(value: str) -> str:
    """Resolve ${ENV:VAR_NAME} references in a string value.

    Only variables explicitly listed in _ALLOWED_MANIFEST_ENV_VARS are
    resolved; all other references are left unchanged so secrets cannot be
    pulled into the plugin environment via manifest configuration. Allowed
    variables resolve from os.environ first, then fall back to the host
    Settings singleton for names in _SETTINGS_ENV_FALLBACK.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name not in _ALLOWED_MANIFEST_ENV_VARS:
            logger.warning(
                "Blocked manifest env reference to non-whitelisted variable: %s",
                var_name,
            )
            return match.group(0)
        # Real environment wins; fall back to the host Settings (.env) so
        # plugin manifests work without requiring shell-exported variables.
        return os.environ.get(var_name, "") or _settings_env_fallback(var_name)

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
    _crash_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def client(self) -> JSONRPCClient:
        if self._client is None:
            raise RuntimeError(f"Plugin '{self.name}' is not ready (state: {self.state.value})")
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
        artifact_store: Any = None,
        artifact_store_registry: Any = None,
        plugin_lifecycle: PluginLifecycle | None = None,
        log_dir: str | Path = ".agent_logs/plugins",
    ) -> None:
        self._plugin_dir = plugin_dir
        self._extension_registry = extension_registry
        self._max_restarts = max_restarts
        self._health_interval = health_interval
        self._artifact_store = artifact_store
        self._artifact_store_registry = artifact_store_registry
        self._log_dir = Path(log_dir)
        self._processes: dict[str, PluginProcess] = {}
        self._scan_results: dict[str, PluginScanResult] = {}

        # Use a provided lifecycle or create one wired to the capability registry.
        cap_registry = getattr(extension_registry, "_capability_registry", None)
        self._lifecycle: PluginLifecycle | None
        if plugin_lifecycle is not None:
            self._lifecycle = plugin_lifecycle
        elif cap_registry is not None:
            self._lifecycle = PluginLifecycle(
                capability_registry=cap_registry,
                max_restarts=max_restarts,
                immediate_crash_window=_IMMEDIATE_CRASH_WINDOW,
                restart_callback=self._restart_provider,
            )
        else:
            self._lifecycle = None

    def get_processes(self) -> dict[str, PluginProcess]:
        """Return a copy of the process map keyed by plugin name."""
        return dict(self._processes)

    # -- Crash diagnostics: stderr tee + exit-code reporting ---------------------

    def _stderr_log_path(self, name: str) -> Path:
        return self._log_dir / f"{name}.log"

    def _append_stderr_log(self, name: str, text: str) -> None:
        """Append a line to the plugin's stderr log (bounded file size)."""
        try:
            path = self._stderr_log_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _MAX_STDERR_LOG_BYTES:
                # Keep the tail so the log stays bounded.
                tail = path.read_bytes()[-_MAX_STDERR_LOG_BYTES // 4 :]
                path.write_bytes(tail)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")
        except Exception:
            logger.debug("Failed to write plugin stderr log", exc_info=True)

    @staticmethod
    def _describe_exit_code(returncode: int | None) -> str:
        """Human-readable exit status: code, or signal name for negative codes."""
        if returncode is None:
            return "unknown"
        if returncode >= 0:
            return str(returncode)
        try:
            import signal

            return f"{returncode} (signal {-returncode}={signal.Signals(-returncode).name})"
        except (ValueError, KeyError):
            return f"{returncode} (signal {-returncode})"

    def get_scan_results(self) -> dict[str, PluginScanResult]:
        """Return the last scan results keyed by plugin name (valid + blocked)."""
        return dict(self._scan_results)

    async def start_plugin(self, name: str) -> PluginState:
        """Start a stopped/fatal/never-started plugin by name.

        Manual starts get a fresh crash budget.  No-op when already running.
        """
        proc = self._processes.get(name)
        if proc is not None and proc.state in (
            PluginState.ACTIVE,
            PluginState.REGISTERING,
            PluginState.LOADING,
            PluginState.RESTARTING,
        ):
            return proc.state
        if proc is None:
            result = self._scan_results.get(name)
            if result is None or result.manifest is None:
                raise KeyError(f"Unknown plugin: {name}")
            proc = PluginProcess(
                name=result.name,
                manifest=result.manifest,
                plugin_dir=result.dir,
            )
            self._processes[name] = proc
        proc._restart_count = 0
        proc._health_failures = 0
        try:
            await self._start_one(proc)
        except Exception:
            logger.error("Manual start of plugin '%s' failed", name, exc_info=True)
            await self._on_crash(proc)
        return proc.state

    async def stop_plugin(self, name: str) -> PluginState:
        """Stop a running plugin and unregister its capabilities."""
        proc = self._processes.get(name)
        if proc is None:
            raise KeyError(f"Unknown plugin: {name}")
        await self._stop_one(proc)
        return proc.state

    async def restart_plugin(self, name: str) -> PluginState:
        """Stop (when running) then start a plugin."""
        proc = self._processes.get(name)
        if proc is not None and proc.state in (
            PluginState.ACTIVE,
            PluginState.REGISTERING,
        ):
            await self._stop_one(proc)
        return await self.start_plugin(name)

    async def start_all(self, results: list[PluginScanResult]) -> None:
        """Start all valid plugins from scan results."""
        self._scan_results = {result.name: result for result in results}
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
                    PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.FATAL.value).set(1)
            except Exception:
                logger.error(
                    "Failed to start plugin '%s'",
                    result.name,
                    exc_info=True,
                )
                proc.state = PluginState.FATAL
                PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.FATAL.value).set(1)

        # Validate artifact contracts after all plugins are loaded so
        # the full producer graph is available — per-tool registration-time
        # validation would fire false positives due to load ordering.
        self._extension_registry.validate_all_contracts()

    async def _start_one(self, proc: PluginProcess) -> None:
        """Start a single plugin subprocess and wait for registration."""
        proc.state = PluginState.LOADING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.LOADING.value).set(1)

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

        # Locate the project root (identified by pyproject.toml and a
        # courtier/ package directory) for COURTIER_REPO_ROOT.  Plugin
        # imports resolve from the plugin's own venv — libs and the plugin
        # SDK are installed packages, no PYTHONPATH assembly needed.
        _project_root_path = _find_project_root(proc.plugin_dir)
        project_root = str(_project_root_path)

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

        # Spawn the plugin's own venv python directly when available.
        # `uv run` wraps the real interpreter: SIGKILL then hits the wrapper
        # while the grandchild keeps the stdio pipes open, so wait() hangs
        # ("did not exit after SIGKILL").  A direct interpreter keeps the
        # process tree flat and kill/wait reliable.
        # .absolute(), NOT .resolve(): bin/python in a venv is a symlink to
        # the system interpreter — resolving it would drop the venv's
        # site-packages and the plugin would fail to start.
        venv_python = (proc.plugin_dir / ".venv" / "bin" / "python").absolute()
        if venv_python.is_file():
            cmd = [str(venv_python), entry]
        else:
            cmd = ["uv", "run", entry]
        proc._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(proc.plugin_dir),
            env=env,
            # Responses carry whole parsed documents on one JSON-RPC line —
            # the default 64 KiB stream limit made the host misdiagnose
            # healthy plugins as crashed (LimitOverrunError → kill).
            limit=STREAM_LIMIT_BYTES,
        )

        # CRITICAL: Any failure after subprocess creation must clean up the
        # child process to prevent zombie processes.  The finally block
        # below ensures cleanup on all error paths.
        try:
            # Create client with crash callback wired to _on_crash
            client_timeout = proc.manifest.timeout_ms / 1000.0 if proc.manifest else 30.0
            proc._client = JSONRPCClient(
                reader=proc._process.stdout,
                writer=proc._process.stdin,
                plugin_name=proc.name,
                on_disconnect=lambda: self._on_crash(proc),
                default_timeout=client_timeout,
            )

            # Wait for register notification
            proc.state = PluginState.REGISTERING
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.REGISTERING.value).set(1)
            try:
                capabilities = await proc._client.wait_for_register(timeout=10.0)
            except asyncio.TimeoutError:
                proc.state = PluginState.FATAL
                PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.FATAL.value).set(1)
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
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.ACTIVE.value).set(1)
            proc._started_at = asyncio.get_event_loop().time()
            proc._restart_count = 0
            proc._health_failures = 0

            if self._lifecycle is not None:
                handle = PluginHandle(
                    provider=proc.name,
                    process=proc,
                    restart_count=proc._restart_count,
                    started_at=proc._started_at,
                    state="active",
                )
                self._lifecycle.track_process(handle)
                self._lifecycle.reset_health(proc.name)

            # Start stderr reader for crash detection and log forwarding
            proc._stderr_task = asyncio.create_task(self._monitor_stderr(proc))

            # Start periodic health check loop
            proc._health_task = asyncio.create_task(self._health_loop(proc))

            # Register host service handler and tell the plugin which services it may use.
            proc._client.set_host_request_handler(self._create_host_request_handler(proc))
            await proc._client.notify(
                METHOD_HOST_SERVICES,
                {
                    "host_services": list(proc.manifest.dependencies.host_services or []),
                    "permissions": list(proc.manifest.dependencies.permissions or []),
                },
            )

            # Send runtime context (COURTIER.md, environment) so plugins can
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
    ) -> Callable[[dict[str, Any]], Awaitable[Any]]:
        """Create a handler for plugin-to-host JSON-RPC requests.

        The handler enforces the plugin's declared host_service dependencies
        and permissions.  Its return value is placed verbatim into the
        JSON-RPC ``result`` field, so it may be any JSON value (dict, list,
        or None), not just a dict.
        """
        manifest = proc.manifest
        perms = set(manifest.dependencies.permissions or [])
        host_services = set(manifest.dependencies.host_services or [])
        artifact_store = self._artifact_store
        artifact_registry = self._artifact_store_registry

        def _deny(code: int, message: str) -> dict[str, Any]:
            return {"error": {"code": code, "message": message}}

        async def handler(request: dict[str, Any]) -> Any:
            method = request.get("method", "")
            params = request.get("params", {})
            session_id = params.get("session_id")

            if method == METHOD_CACHE_PERSIST:
                if "cache" not in host_services or "write:cache" not in perms:
                    return _deny(INVALID_PARAMS, "Missing write:cache permission")
                if artifact_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                result = await artifact_store.persist(
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
                if artifact_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                return artifact_store.load(params.get("ref_id"))

            if method == METHOD_CACHE_RESOLVE:
                if "cache" not in host_services or "read:cache" not in perms:
                    return _deny(INVALID_PARAMS, "Missing read:cache permission")
                if artifact_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                return artifact_store.resolve_refs(params.get("kwargs", {}))

            if method == METHOD_CACHE_MICRO_COMPACT:
                if "cache" not in host_services:
                    return _deny(INVALID_PARAMS, "Cache host service not declared")
                if not ({"read:cache", "write:cache"} <= perms):
                    return _deny(
                        INVALID_PARAMS,
                        "Missing read:cache or write:cache permission",
                    )
                if artifact_store is None:
                    return _deny(INTERNAL_ERROR, "Host cache store not available")
                messages = params.get("messages", [])
                compacted = await _micro_compact_dict_messages(messages, artifact_store)
                return compacted

            if method == METHOD_ARTIFACT_STORE_PUT:
                if "artifact_store" not in host_services or "write:artifacts" not in perms:
                    return _deny(INVALID_PARAMS, "Missing write:artifacts permission")
                if artifact_registry is None:
                    return _deny(INTERNAL_ERROR, "Host artifact store registry not available")
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(INVALID_PARAMS, f"No artifact store for session {session_id}")
                from courtier.agent.artifacts.models import Artifact

                artifact = Artifact(**params.get("artifact", {}))
                stored = store.put(artifact)
                return stored.model_dump() if stored is not None else None

            if method == METHOD_ARTIFACT_STORE_GET:
                if "artifact_store" not in host_services:
                    return _deny(INVALID_PARAMS, "Artifact store host service not declared")
                if not ({"read:artifacts", "read:documents"} & perms):
                    return _deny(INVALID_PARAMS, "Missing read artifact permission")
                if artifact_registry is None:
                    return _deny(INTERNAL_ERROR, "Host artifact store registry not available")
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(INVALID_PARAMS, f"No artifact store for session {session_id}")
                artifact = store.get(params.get("artifact_id"))
                return artifact.model_dump() if artifact is not None else None

            if method == METHOD_ARTIFACT_STORE_LIST:
                if "artifact_store" not in host_services:
                    return _deny(INVALID_PARAMS, "Artifact store host service not declared")
                if not ({"read:artifacts", "read:documents"} & perms):
                    return _deny(INVALID_PARAMS, "Missing read artifact permission")
                if artifact_registry is None:
                    return _deny(INTERNAL_ERROR, "Host artifact store registry not available")
                store = artifact_registry.get(session_id)
                if store is None:
                    return _deny(INVALID_PARAMS, f"No artifact store for session {session_id}")
                return [a.model_dump() for a in store.list_all()]

            if method == METHOD_STORAGE_PUT:
                if "storage" not in host_services or "write:storage" not in perms:
                    return _deny(INVALID_PARAMS, "Missing write:storage permission")
                settings = get_settings()
                if not settings.minio_endpoint:
                    return _deny(INTERNAL_ERROR, "MinIO 未配置，无法存储文件")
                # basename 防路径穿越；uuid 目录避免同名文件互相覆盖。
                filename = Path(str(params.get("filename") or "output.bin")).name
                try:
                    data = base64.b64decode(params.get("data_b64") or "")
                except Exception:
                    return _deny(INVALID_PARAMS, "data_b64 不是合法的 base64 编码")
                if not data:
                    return _deny(INVALID_PARAMS, "文件内容为空")
                content_type = str(params.get("content_type") or "application/octet-stream")
                object_key = f"plugin-outputs/{uuid.uuid4().hex}/{filename}"
                bucket = settings.minio_bucket_docs
                try:
                    await asyncio.to_thread(
                        storage_client.put_object, bucket, object_key, data, content_type
                    )
                    url = await asyncio.to_thread(
                        storage_client.get_presigned_url,
                        bucket,
                        object_key,
                        _STORAGE_URL_EXPIRES_SECONDS,
                    )
                except Exception as exc:
                    logger.warning("storage.put 上传失败: %s", object_key, exc_info=True)
                    return _deny(INTERNAL_ERROR, f"文件存储失败: {exc}")
                return {
                    "bucket": bucket,
                    "object_key": object_key,
                    "download_url": url,
                    "expires_in": _STORAGE_URL_EXPIRES_SECONDS,
                    "size_bytes": len(data),
                }

            if method == METHOD_TEMPLATE_STORE_GET:
                if "template_store" not in host_services or "read:templates" not in perms:
                    return _deny(INVALID_PARAMS, "Missing read:templates permission")
                doc_type = str(params.get("doc_type") or "")
                if not doc_type:
                    return _deny(INVALID_PARAMS, "doc_type is required")
                template_id = params.get("template_id")
                try:
                    from courtier.agent.api.db import get_db
                    from courtier.db import CRUDRepository
                    from courtier.db.tables import FormatTemplateTable

                    repo = CRUDRepository(FormatTemplateTable)
                    async with get_db().session() as session:
                        if template_id is not None:
                            row = await repo.get(session, int(template_id))
                            return row.content if row else None
                        rows = await repo.list(session, doc_type=doc_type, is_default=True, limit=1)
                        return rows[0].content if rows else None
                except Exception as exc:
                    logger.warning("template_store.get 查询失败: %s", exc, exc_info=True)
                    return _deny(INTERNAL_ERROR, f"模板查询失败: {exc}")

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
            return bool(result == "ok")
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
                    self._append_stderr_log(proc.name, "<stderr closed (process exited)>")
                    if proc.state == PluginState.ACTIVE:
                        logger.warning(
                            "Plugin '%s' stderr closed unexpectedly (state=%s), treating as crash",
                            proc.name,
                            proc.state.value,
                        )
                        await self._on_crash(proc)
                    break
                text = line.decode().rstrip()
                # Tee every stderr line to the per-plugin log so crashes
                # (which often carry no Python traceback) stay diagnosable.
                self._append_stderr_log(proc.name, text)
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
        async with proc._crash_lock:
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
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.CRASHED.value).set(1)

            # Crash diagnostics first (every crash path records these):
            # exit code/signal + a marker line in the plugin's stderr log.
            returncode = proc._process.returncode if proc._process is not None else None
            if returncode is None and proc._process is not None:
                # Pipes hit EOF before the child is reaped — give it a brief
                # moment so we record the real exit code instead of "unknown".
                try:
                    await asyncio.wait_for(proc._process.wait(), timeout=0.5)
                    returncode = proc._process.returncode
                except (TimeoutError, ProcessLookupError):
                    pass
            exit_desc = self._describe_exit_code(returncode)
            self._append_stderr_log(
                proc.name,
                f"<crash detected: exit={exit_desc}, "
                f"restart={proc._restart_count}/{self._max_restarts}>",
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

            # Sync lifecycle handle state before unregistering; the unregister
            # event will be observed by PluginLifecycle and may schedule a restart.
            if self._lifecycle is not None:
                handle = self._lifecycle.get_handle(proc.name)
                if handle is not None:
                    handle.state = "crashed"
                    handle.restart_count = proc._restart_count
                    handle.started_at = proc._started_at

            # Always unregister before any crash handling so registries never
            # retain stale entries — this must run before the circuit breaker
            # return below.
            self._extension_registry.on_unregister(proc.name)

            # When a PluginLifecycle is wired to the capability registry, the
            # unregister event above already triggered the restart decision.
            # Fall back to the local restart policy only when no lifecycle is
            # available.
            if self._lifecycle is not None:
                lifecycle_fatal = self._lifecycle.health_check(proc.name) == "unhealthy"
                if lifecycle_fatal:
                    proc.state = PluginState.FATAL
                    PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.FATAL.value).set(1)
                # Restart scheduling is handled by the lifecycle listener.
                lifecycle_handled = True
            else:
                lifecycle_handled = False
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
                            "Plugin '%s' crashed %.1fs after startup "
                            "(< %.0fs window, exit=%s), marking FATAL",
                            proc.name,
                            uptime,
                            _IMMEDIATE_CRASH_WINDOW,
                            exit_desc,
                        )
                        return

        logger.error(
            "Plugin '%s' crashed (restart %d/%d, exit=%s)",
            proc.name,
            proc._restart_count,
            self._max_restarts,
            exit_desc,
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

        if not lifecycle_handled:
            await self._attempt_restart(proc)

    async def _restart_provider(self, provider: str) -> None:
        """Restart callback used by PluginLifecycle.

        Finds the tracked process and re-runs :meth:`_start_one`.  Restart
        failures are handled by :meth:`_on_crash`.
        """
        proc = self._processes.get(provider)
        if proc is None:
            logger.error("PluginLifecycle asked to restart unknown provider '%s'", provider)
            return
        proc.state = PluginState.RESTARTING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.RESTARTING.value).set(1)
        try:
            await self._start_one(proc)
        except Exception:
            logger.error("Plugin '%s' restart failed", proc.name, exc_info=True)
            await self._on_crash(proc)

    async def _attempt_restart(self, proc: PluginProcess) -> None:
        """Attempt to restart a crashed plugin with exponential backoff.

        Calls _start_one; on failure, re-enters _on_crash which will
        decrement the restart budget or mark FATAL.
        """
        if proc._restart_count >= self._max_restarts:
            proc.state = PluginState.FATAL
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.FATAL.value).set(1)
            logger.error(
                "Plugin '%s' exceeded max restarts (%d), marking FATAL",
                proc.name,
                self._max_restarts,
            )
            return

        proc._restart_count += 1
        delay = min(1 * (2 ** (proc._restart_count - 1)), 30)
        proc.state = PluginState.RESTARTING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.RESTARTING.value).set(1)
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

    async def _stop_one(self, proc: PluginProcess) -> None:
        """Stop a single plugin subprocess and unregister its capabilities."""
        if proc.state not in (PluginState.ACTIVE, PluginState.REGISTERING):
            return
        proc.state = PluginState.STOPPING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.STOPPING.value).set(1)
        if self._lifecycle is not None:
            handle = self._lifecycle.get_handle(proc.name)
            if handle is not None:
                handle.state = "stopped"
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
            logger.warning("Plugin '%s' shutdown timeout, force killing", proc.name)
            await self._kill_process(proc)

        if proc._client:
            # Detach the crash callback BEFORE closing: the intentional kill
            # closes the connection, and a stale on_disconnect racing the next
            # start would otherwise pass the _on_crash state guard (LOADING)
            # and tear down the fresh registration with a duplicate restart.
            proc._client._on_disconnect = None
            proc._client.close()
            proc._client = None

        proc.state = PluginState.STOPPED
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.STOPPED.value).set(1)

    async def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        for proc in self._processes.values():
            await self._stop_one(proc)

        if self._lifecycle is not None:
            await self._lifecycle.shutdown()

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

    Includes the project COURTIER.md content, environment info, and artifact
    system instructions so plugin tools have the same project-level context
    as the main orchestrator agent.
    """
    ctx: dict[str, str] = {}

    # Environment
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx["environment"] = f"- 日期: {now}\n" f"- 平台: {platform.system().lower()}"

    # COURTIER.md — project rules at the repo root
    courtier_md_path = Path(__file__).resolve().parent.parent.parent / "COURTIER.md"
    try:
        if courtier_md_path.exists():
            ctx["courtier_md"] = courtier_md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("Failed to read COURTIER.md at %s", courtier_md_path)

    # Artifact system instructions for plugin tools
    ctx["artifact_instructions"] = (
        "主机管理的类型化工件（artifacts）可通过 host services 访问：\n"
        "- 使用 artifact_store.list 获取当前会话的工件列表\n"
        "- 使用 artifact_store.get 按 artifact_id 获取工件数据\n"
        "- $ref 缓存引用在工具调用参数中由系统自动解析，无需手动处理"
    )

    return ctx
