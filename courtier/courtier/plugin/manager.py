"""ProcessManager — connection manager for standalone plugin servers.

Plugins are independent TCP services (started and supervised outside this
process — docker-compose, systemd, or the dev runner).  The manager dials
each plugin's configured endpoint, performs the mutual token handshake,
routes registered capabilities into the ExtensionRegistry, and keeps the
channel alive with health checks plus an infinite exponential-backoff
reconnect loop.  There is no subprocess management here anymore.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import platform
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from courtier_plugin_sdk.protocol import STREAM_LIMIT_BYTES

from courtier.agent.core.context_manager import ContextManager
from courtier.agent.telemetry.metrics import PLUGIN_STATE
from courtier.config import get_settings
from courtier.storage import client as storage_client

from .client import JSONRPCClient, PluginRPCError
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
    METHOD_PLUGIN_AUTH,
    METHOD_RUNTIME_CONTEXT,
    METHOD_STORAGE_PRESIGN_GET,
    METHOD_STORAGE_PUT,
    METHOD_TEMPLATE_STORE_GET,
)
from .registry import ExtensionRegistry
from .scanner import PluginScanner, PluginScanResult

# Presigned download URLs handed to plugins for stored outputs live this long
# (S3 SigV4 presigned URLs are capped at 7 days).
_STORAGE_URL_EXPIRES_SECONDS = 7 * 24 * 3600

# Reconnect backoff: 1s doubling to this cap, forever — a remote plugin being
# down is transient by definition, and tools re-register on reconnect.
_RECONNECT_MAX_DELAY = 30.0
_RECONNECT_INITIAL_DELAY = 1.0

logger = logging.getLogger(__name__)


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


class PluginState(str, Enum):
    SCANNED = "SCANNED"
    CONNECTING = "CONNECTING"
    REGISTERING = "REGISTERING"
    ACTIVE = "ACTIVE"
    DISCONNECTED = "DISCONNECTED"
    # Terminal config/auth error (missing endpoint, token mismatch, API
    # mismatch); an admin ``start`` action re-enters the reconnect loop.
    BLOCKED = "BLOCKED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class PluginAuthError(Exception):
    """The mutual token handshake failed (deterministic — no auto-retry)."""


class PluginBlockedError(Exception):
    """The plugin's last manifest scan failed; it cannot be connected.

    Carries the scan error so admin surfaces can explain why restart is
    refused (fix plugin.yaml, then retry — no host restart needed).
    """

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Plugin '{name}' is blocked: {detail}")
        self.name = name
        self.detail = detail


@dataclass
class PluginProcess:
    """Handle for one plugin connection (kept across reconnects)."""

    name: str
    manifest: "PluginManifest"
    plugin_dir: Path
    endpoint: tuple[str, int] | None = None
    state: PluginState = PluginState.SCANNED
    _client: JSONRPCClient | None = field(default=None, repr=False)
    _reconnect_count: int = field(default=0, repr=False)
    _health_failures: int = field(default=0, repr=False)
    _health_task: asyncio.Task | None = field(default=None, repr=False)
    _connect_task: asyncio.Task | None = field(default=None, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def client(self) -> JSONRPCClient:
        if self._client is None:
            raise RuntimeError(f"Plugin '{self.name}' is not ready (state: {self.state.value})")
        return self._client


class ProcessManager:
    """Keeps a live channel to every configured plugin server.

    Parameters
    ----------
    plugin_dir : Path
        Root directory containing plugin manifests (policy root: timeouts,
        host-service declarations, permissions are read from plugin.yaml).
    extension_registry : ExtensionRegistry
        Registry to route plugin capabilities into.
    health_interval : float
        Seconds between health-check pings (default 30s).
    endpoints : dict[str, tuple[str, int]], optional
        Plugin name → (host, port).  Defaults to
        ``Settings.plugin_endpoints()`` at start time.
    token : str, optional
        Shared handshake secret.  Defaults to
        ``Settings.courtier_plugin_token`` at start time.
    scanner : PluginScanner, optional
        Manifest scanner used at start and by restart-time re-scans.
        Defaults to a private instance; inject to share one cache with
        the PluginSystem.
    """

    def __init__(
        self,
        plugin_dir: Path,
        extension_registry: "ExtensionRegistry",
        health_interval: float = 30.0,
        artifact_store: Any = None,
        artifact_store_registry: Any = None,
        endpoints: dict[str, tuple[str, int]] | None = None,
        token: str | None = None,
        scanner: PluginScanner | None = None,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._extension_registry = extension_registry
        self._health_interval = health_interval
        self._artifact_store = artifact_store
        self._artifact_store_registry = artifact_store_registry
        self._endpoints = endpoints
        self._token = token
        self._scanner = scanner if scanner is not None else PluginScanner()
        self._processes: dict[str, PluginProcess] = {}
        self._scan_results: dict[str, PluginScanResult] = {}

    def get_processes(self) -> dict[str, PluginProcess]:
        """Return a copy of the process map keyed by plugin name."""
        return dict(self._processes)

    def get_scan_results(self) -> dict[str, PluginScanResult]:
        """Return the last scan results keyed by plugin name (valid + blocked)."""
        return dict(self._scan_results)

    def has_endpoint(self, name: str) -> bool:
        """Whether the connection config covers the plugin (admin diagnostics)."""
        self._resolve_connection_config()
        return name in (self._endpoints or {})

    def _resolve_connection_config(self) -> None:
        """Fill endpoints/token from Settings when not injected (tests inject)."""
        if self._endpoints is None or self._token is None:
            settings = get_settings()
            if self._endpoints is None:
                self._endpoints = settings.plugin_endpoints()
            if self._token is None:
                self._token = settings.courtier_plugin_token

    # ------------------------------------------------------------------ start

    async def start_all(self, results: list[PluginScanResult]) -> None:
        """Kick off connection loops for all valid plugins (non-blocking).

        Tools appear as each plugin connects and registers; the application
        startup never blocks on plugin availability.
        """
        self._resolve_connection_config()
        self._scan_results = {result.name: result for result in results}

        for name in self._endpoints or {}:
            if name not in self._scan_results:
                logger.warning(
                    "COURTIER_PLUGIN_ENDPOINTS lists '%s', but no such plugin was scanned",
                    name,
                )

        for result in results:
            if result.status.value != "VALID" or result.manifest is None:
                continue
            endpoint = (self._endpoints or {}).get(result.name)
            proc = PluginProcess(
                name=result.name,
                manifest=result.manifest,
                plugin_dir=result.dir,
                endpoint=endpoint,
            )
            self._processes[result.name] = proc
            if endpoint is None:
                proc.state = PluginState.BLOCKED
                PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.BLOCKED.value).set(1)
                logger.error(
                    "Plugin '%s' has no endpoint in COURTIER_PLUGIN_ENDPOINTS — BLOCKED",
                    proc.name,
                )
                continue
            self._spawn_connection_loop(proc)

    def _spawn_connection_loop(self, proc: PluginProcess) -> None:
        proc._stop_event.clear()
        proc._connect_task = asyncio.create_task(self._connection_loop(proc))

    # --------------------------------------------------------- connection loop

    async def _connection_loop(self, proc: PluginProcess) -> None:
        """Connect → handshake → serve → reconnect, until stopped."""
        backoff = _RECONNECT_INITIAL_DELAY
        while not proc._stop_event.is_set():
            assert proc.endpoint is not None
            proc.state = PluginState.CONNECTING
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.CONNECTING.value).set(1)
            try:
                reader, writer = await asyncio.open_connection(
                    proc.endpoint[0], proc.endpoint[1], limit=STREAM_LIMIT_BYTES
                )
            except OSError as exc:
                proc._reconnect_count += 1
                proc.state = PluginState.DISCONNECTED
                PLUGIN_STATE.labels(
                    plugin_name=proc.name, state=PluginState.DISCONNECTED.value
                ).set(1)
                logger.warning(
                    "Plugin '%s' unreachable at %s:%d (%s); retry in %.0fs",
                    proc.name,
                    proc.endpoint[0],
                    proc.endpoint[1],
                    exc,
                    backoff,
                )
                if await self._sleep_or_stopped(proc, backoff):
                    break
                backoff = min(backoff * 2, _RECONNECT_MAX_DELAY)
                continue

            disconnected = asyncio.Event()

            async def _on_disconnect() -> None:
                disconnected.set()

            client = JSONRPCClient(
                reader=reader,
                writer=writer,
                plugin_name=proc.name,
                on_disconnect=_on_disconnect,
                default_timeout=(proc.manifest.timeout_ms / 1000.0 if proc.manifest else 30.0),
            )
            proc._client = client
            try:
                await self._handshake(proc, client)
            except PluginAuthError as exc:
                logger.error("Plugin '%s' blocked: %s", proc.name, exc)
                proc.state = PluginState.BLOCKED
                PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.BLOCKED.value).set(1)
                client.close()
                proc._client = None
                return  # terminal until an admin start re-enters the loop
            except Exception:
                logger.warning(
                    "Plugin '%s' handshake failed; will reconnect",
                    proc.name,
                    exc_info=True,
                )
            else:
                backoff = _RECONNECT_INITIAL_DELAY  # handshake succeeded — reset
                proc._health_task = asyncio.create_task(self._health_loop(proc))
                # Validate contracts now that this plugin's producers exist.
                self._extension_registry.validate_all_contracts()
                await disconnected.wait()

            # Teardown for this connection (handshake failure or disconnect).
            if proc._health_task is not None:
                proc._health_task.cancel()
                proc._health_task = None
            if proc.state == PluginState.ACTIVE:
                self._extension_registry.on_unregister(proc.name)
            client.close()
            proc._client = None

            if proc._stop_event.is_set():
                break
            proc._reconnect_count += 1
            proc.state = PluginState.DISCONNECTED
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.DISCONNECTED.value).set(1)
            logger.warning(
                "Plugin '%s' connection lost; reconnect in %.0fs",
                proc.name,
                backoff,
            )
            if await self._sleep_or_stopped(proc, backoff):
                break
            backoff = min(backoff * 2, _RECONNECT_MAX_DELAY)

        if proc.state != PluginState.BLOCKED:
            proc.state = PluginState.STOPPED
            PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.STOPPED.value).set(1)

    async def _sleep_or_stopped(self, proc: PluginProcess, delay: float) -> bool:
        """Sleep *delay* seconds; return True immediately when stopping."""
        try:
            await asyncio.wait_for(proc._stop_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _handshake(self, proc: PluginProcess, client: JSONRPCClient) -> None:
        """Register + mutual token auth + capability registration."""
        proc.state = PluginState.REGISTERING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.REGISTERING.value).set(1)

        capabilities = await client.wait_for_register(timeout=10.0)

        # plugin → host auth: the token the plugin presented must match ours.
        expected = self._token or ""
        presented = client._register_token
        if not expected:
            raise PluginAuthError(
                "COURTIER_PLUGIN_TOKEN is not configured on the host — "
                "refusing unauthenticated plugin channel"
            )
        if not hmac.compare_digest(presented, expected):
            raise PluginAuthError("plugin presented an invalid token")

        # host → plugin auth: plugin rejects every method until this passes.
        try:
            await client.call(METHOD_PLUGIN_AUTH, {"token": expected}, timeout=10.0)
        except PluginRPCError as exc:
            raise PluginAuthError(f"plugin rejected our token: {exc}") from exc

        # Route capabilities to registries
        self._extension_registry.on_register(
            proc.name,
            client,
            capabilities,
            system_prompt=client._register_system_prompt,
        )
        proc.state = PluginState.ACTIVE
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.ACTIVE.value).set(1)
        proc._health_failures = 0

        # Register host service handler and tell the plugin which services it may use.
        client.set_host_request_handler(self._create_host_request_handler(proc))
        await client.notify(
            METHOD_HOST_SERVICES,
            {
                "host_services": list(proc.manifest.dependencies.host_services or []),
                "permissions": list(proc.manifest.dependencies.permissions or []),
            },
        )

        # Send runtime context (COURTIER.md, environment) so plugins can
        # inject project-level rules into their tool system prompts.
        await client.notify(METHOD_RUNTIME_CONTEXT, _build_runtime_context())
        logger.info("Plugin '%s' connected and ACTIVE", proc.name)

    # ------------------------------------------------------------ host services

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
                    return _deny(INTERNAL_ERROR, "Cache host service not declared")
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

            if method == METHOD_STORAGE_PRESIGN_GET:
                if "storage" not in host_services or "read:storage" not in perms:
                    return _deny(INVALID_PARAMS, "Missing read:storage permission")
                settings = get_settings()
                if not settings.minio_endpoint:
                    return _deny(INTERNAL_ERROR, "MinIO 未配置，无法签名下载链接")
                bucket = str(params.get("bucket") or "")
                key = str(params.get("key") or "")
                # Only the transfer bucket may be presigned through this
                # channel — plugins hold its restricted credentials already,
                # and everything else stays unreachable from plugin code.
                if bucket != settings.minio_bucket_plugin_io:
                    return _deny(INVALID_PARAMS, f"不允许签名的 bucket: {bucket}")
                if not key or key.startswith("/") or ".." in key.split("/"):
                    return _deny(INVALID_PARAMS, "非法的对象 key")
                try:
                    expires = int(params.get("expires") or _STORAGE_URL_EXPIRES_SECONDS)
                except (TypeError, ValueError):
                    return _deny(INVALID_PARAMS, "expires 必须是整数秒")
                expires = max(60, min(expires, _STORAGE_URL_EXPIRES_SECONDS))
                try:
                    url = await asyncio.to_thread(
                        storage_client.get_presigned_url, bucket, key, expires
                    )
                except Exception as exc:
                    logger.warning(
                        "storage.presign_get 签名失败: %s/%s", bucket, key, exc_info=True
                    )
                    return _deny(INTERNAL_ERROR, f"签名下载链接失败: {exc}")
                return {
                    "bucket": bucket,
                    "object_key": key,
                    "download_url": url,
                    "expires_in": expires,
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

    # ------------------------------------------------------------------ health

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

    async def _health_loop(self, proc: PluginProcess) -> None:
        """Periodic health check loop.

        After 3 consecutive failures the connection is closed, which the
        connection loop observes as a disconnect and reconnects.
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
                    "Plugin '%s' failed health check 3 times, closing connection",
                    proc.name,
                )
                if proc._client is not None:
                    proc._client.close()
                break

    # --------------------------------------------------------------- admin ops

    async def start_plugin(self, name: str) -> PluginState:
        """(Re)enter the connection loop for a stopped/blocked plugin."""
        proc = self._processes.get(name)
        if proc is not None and proc.state in (
            PluginState.ACTIVE,
            PluginState.REGISTERING,
            PluginState.CONNECTING,
        ):
            return proc.state
        if proc is None:
            result = self._scan_results.get(name)
            if result is None:
                raise KeyError(f"Unknown plugin: {name}")
            if result.manifest is None:
                # Last scan failed; restart_plugin re-scans first, so this
                # means the on-disk manifest is still invalid.
                raise PluginBlockedError(name, result.error or "manifest failed to load")
            self._resolve_connection_config()
            proc = PluginProcess(
                name=result.name,
                manifest=result.manifest,
                plugin_dir=result.dir,
                endpoint=(self._endpoints or {}).get(result.name),
            )
            self._processes[name] = proc
        if proc.endpoint is None:
            proc.state = PluginState.BLOCKED
            logger.error("Plugin '%s' has no endpoint configured — stays BLOCKED", name)
            return proc.state
        proc._health_failures = 0
        self._spawn_connection_loop(proc)
        return proc.state

    async def stop_plugin(self, name: str) -> PluginState:
        """Disconnect from a plugin and unregister its capabilities."""
        proc = self._processes.get(name)
        if proc is None:
            raise KeyError(f"Unknown plugin: {name}")
        if proc.state in (PluginState.STOPPED, PluginState.SCANNED):
            return proc.state
        was_active = proc.state == PluginState.ACTIVE
        proc.state = PluginState.STOPPING
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.STOPPING.value).set(1)
        proc._stop_event.set()
        client = proc._client
        if client is not None:
            await client.notify("plugin.shutdown")
            client.close()
            proc._client = None
        if was_active:
            self._extension_registry.on_unregister(proc.name)
        task = proc._connect_task
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        proc.state = PluginState.STOPPED
        PLUGIN_STATE.labels(plugin_name=proc.name, state=PluginState.STOPPED.value).set(1)
        return proc.state

    async def restart_plugin(self, name: str) -> PluginState:
        """Drop the current connection (if any), re-scan, and redial.

        The re-scan merges fresh manifest results into the cached scan
        results before redialling, so a fixed plugin.yaml revives the
        plugin from admin restart without restarting the host.  Plugins
        that vanished from disk keep their last record.
        """
        proc = self._processes.get(name)
        if proc is not None and proc.state in (
            PluginState.ACTIVE,
            PluginState.REGISTERING,
            PluginState.CONNECTING,
            PluginState.DISCONNECTED,
        ):
            await self.stop_plugin(name)
        for result in self._scanner.scan(self._plugin_dir):
            self._scan_results[result.name] = result
        return await self.start_plugin(name)

    async def cancel_pending(self) -> None:
        """Cancel pending requests on all active plugin connections.

        Sends ``request.cancel`` notifications so plugins stop processing
        in-flight requests.  Does NOT disconnect — plugins remain ACTIVE
        for future sessions.
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

    async def notify(
        self, plugin_name: str, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Send a fire-and-forget notification to a live plugin.

        Silently skips plugins that are unknown or not ACTIVE; delivery
        failures are logged at debug level — notifications are advisory
        (e.g. cache invalidation) and must never block the caller.
        """
        proc = self._processes.get(plugin_name)
        if proc is None or proc.state != PluginState.ACTIVE or proc._client is None:
            return
        try:
            await proc._client.notify(method, params or {})
        except Exception:
            logger.debug("notify(%s) to plugin '%s' failed", method, plugin_name, exc_info=True)

    async def shutdown(self) -> None:
        """Disconnect all plugin channels (plugins themselves keep running)."""
        for proc in self._processes.values():
            if proc.state not in (PluginState.STOPPED, PluginState.SCANNED):
                try:
                    await self.stop_plugin(proc.name)
                except Exception:
                    logger.debug("Error stopping plugin '%s'", proc.name, exc_info=True)


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
