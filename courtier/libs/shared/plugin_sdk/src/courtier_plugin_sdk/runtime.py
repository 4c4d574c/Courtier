"""PluginRuntime — base class for standalone plugin servers.

Plugin authors subclass PluginRuntime, register capabilities and
handlers, then call ``await runtime.run()`` in their entry.py.

In production the runtime starts a TCP server (newline-delimited
JSON-RPC); the host dials in, the plugin registers on every accepted
connection, and both sides authenticate each other with a shared token
(``COURTIER_PLUGIN_TOKEN``) before any method is served.

Handlers registered via :meth:`on` are regular functions or coroutines
that return a result dict directly (for ``tool.execute``, ``checker.check``,
etc.).

Usage in entry.py::

    class MyPlugin(PluginRuntime):
        def register_capabilities(self):
            return [
                {"type": "tool", "name": "my_tool", "display_name": "My Tool"},
                {"type": "checker", "name": "my_checker", "doc_type": "通知"},
            ]

        def _setup_handlers(self):
            @self.on("tool.execute")
            def handle_tool_execute(params):
                return {"success": True, "data": {"echo": params.get("args", {})}}

    if __name__ == "__main__":
        import asyncio
        asyncio.run(MyPlugin().run())

Listen address resolution (first match wins): ``--listen host:port`` CLI
argument → ``COURTIER_PLUGIN_LISTEN`` env → ``runtime.port`` from the
plugin.yaml in the current working directory (host defaults to 0.0.0.0).

Tests may inject ``runtime._reader`` (asyncio.Queue) and ``runtime._writer``
before calling ``run()``; that in-process path skips the network and the
auth gate entirely (there is no trust boundary inside one process).
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol import (
    AUTH_ERROR,
    INTERNAL_ERROR,
    METHOD_CHECKER_LIST,
    METHOD_HEALTH,
    METHOD_HOST_SERVICES,
    METHOD_NOT_FOUND,
    METHOD_PLUGIN_AUTH,
    METHOD_REGISTER,
    METHOD_RUNTIME_CONTEXT,
    METHOD_SHUTDOWN,
    METHOD_TOOL_EXECUTE,
    METHOD_TOOL_LIST,
    STREAM_LIMIT_BYTES,
)

logger = logging.getLogger(__name__)

# JSON-RPC payload keys that should be redacted from logs
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "password",
        "secret",
        "token",
        "authorization",
        "auth",
    }
)

#: Env var holding the shared host↔plugin authentication token.
ENV_PLUGIN_TOKEN = "COURTIER_PLUGIN_TOKEN"
#: Env var selecting the listen address (``host:port``).
ENV_PLUGIN_LISTEN = "COURTIER_PLUGIN_LISTEN"


def _sanitize_rpc_log(line: str, max_len: int = 200) -> str:
    """Redact sensitive values from a JSON-RPC log line."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line[:max_len]

    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ("***" if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS else _redact(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_redact(v) for v in value]
        return value

    return json.dumps(_redact(obj), ensure_ascii=False)[:max_len]


class HostServiceError(Exception):
    """Error returned by a host service called from the plugin."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class HostServiceClient:
    """Client for plugin-initiated JSON-RPC calls to the host.

    Uses negative request IDs so the connection dispatcher can route host
    responses back to this client without colliding with the positive IDs
    used for host→plugin requests.
    """

    def __init__(self, reader: Any, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    def _new_id(self) -> int:
        self._next_id += 1
        return -self._next_id

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """Call a host service method and return its result."""
        req_id = self._new_id()
        msg = {"id": req_id, "method": method, "params": params or {}}
        line = json.dumps(msg, ensure_ascii=False)
        self._writer.write((line + "\n").encode("utf-8"))
        # drain() is async (asyncio.StreamWriter); flush() is sync
        # (test writers).  HostServiceClient may receive either depending
        # on whether the connection is a socket or an in-process double.
        if hasattr(self._writer, "drain"):
            await self._writer.drain()
        elif hasattr(self._writer, "flush"):
            self._writer.flush()

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            raw = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

        if isinstance(raw, dict):
            error = raw.get("error")
            if error:
                raise HostServiceError(
                    error.get("code", -1),
                    error.get("message", "Unknown error"),
                )
            return raw.get("result")
        return raw

    def dispatch_response(self, data: dict[str, Any]) -> None:
        """Route a host response to the pending future for its request ID."""
        msg_id = data.get("id")
        if not isinstance(msg_id, int):
            return
        future = self._pending.pop(msg_id, None)
        if future is None or future.done():
            return
        if data.get("error"):
            future.set_exception(
                HostServiceError(
                    data["error"].get("code", -1),
                    data["error"].get("message", "Unknown error"),
                )
            )
        else:
            future.set_result(data)

    def fail_all_pending(self, exc: Exception) -> None:
        """Fail every outstanding call (connection lost)."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _log_task_exception(task: asyncio.Task) -> None:
    """Log any unhandled exception from a concurrently processed request."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Unhandled exception in plugin request task: %s", exc, exc_info=exc)


class _Connection:
    """One host↔plugin channel: socket (production) or injected I/O (tests).

    Holds all per-connection state (auth flag, host-service client, active
    requests) so multiple hosts could attach to one plugin server with
    independent request-ID spaces; in practice exactly one host connects.
    """

    def __init__(
        self,
        runtime: PluginRuntime,
        reader: Any,
        writer: Any,
        *,
        peer: Any = None,
        require_auth: bool,
    ) -> None:
        self.runtime = runtime
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.require_auth = require_auth
        self.authed = not require_auth
        self.closed = False
        self.host_service_client: HostServiceClient | None = None
        self._active_requests: dict[int, asyncio.Task] = {}
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ serve

    async def serve(self) -> None:
        """Send registration, then process lines until EOF or shutdown."""
        params: dict[str, Any] = {
            "capabilities": self.runtime._caps,
            "system_prompt": self.runtime._system_prompt,
        }
        if self.require_auth:
            # plugin → host authentication: the host verifies this token
            # against its own COURTIER_PLUGIN_TOKEN before serving us.
            params["token"] = self.runtime._token
        await self._send_notification(METHOD_REGISTER, params)
        logger.info("Connection open (peer=%s); register sent", self.peer or "in-process")

        try:
            while not self.closed:
                if isinstance(self.reader, asyncio.Queue):
                    line = await self.reader.get()
                    line = line.strip() if isinstance(line, str) else ""
                    if not line:
                        break  # test-side EOF marker
                else:
                    raw = await self.reader.readline()
                    if not raw:
                        break  # host hung up
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                logger.info("READ line: %s", _sanitize_rpc_log(line))
                task = asyncio.create_task(self._process_line_safe(line))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
                task.add_done_callback(_log_task_exception)
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Connection lost: cancel in-flight requests, fail pending host calls."""
        self.closed = True
        for task in self._active_requests.values():
            if not task.done():
                task.cancel()
        if self._pending_tasks:
            await asyncio.wait(self._pending_tasks, timeout=5.0)
        if self.host_service_client is not None:
            self.host_service_client.fail_all_pending(
                HostServiceError(-32002, "connection to host lost")
            )
        if self.runtime._host_service_client is self.host_service_client:
            self.runtime._host_service_client = None
        try:
            self.writer.close()
            if hasattr(self.writer, "wait_closed"):
                await self.writer.wait_closed()
        except Exception:
            logger.debug("Error closing connection writer", exc_info=True)
        logger.info("Connection closed (peer=%s)", self.peer or "in-process")

    async def close(self) -> None:
        """Actively close this connection (shutdown notification / server stop)."""
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            logger.debug("Error closing connection writer", exc_info=True)

    # -------------------------------------------------------------- dispatch

    async def _process_line_safe(self, line: str) -> None:
        try:
            await self._process_line(line)
        except Exception:
            logger.exception("Unhandled error processing request: %s", line[:200])

    async def _process_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.error("Malformed JSON: %s", line[:200])
            return

        if not isinstance(msg, dict):
            return

        # Host responses to plugin-initiated host-service requests use negative IDs.
        if "id" in msg and "method" not in msg:
            msg_id = msg.get("id")
            if isinstance(msg_id, int) and msg_id < 0 and self.host_service_client is not None:
                self.host_service_client.dispatch_response(msg)
            return

        if "method" in msg and "id" not in msg:
            await self._handle_notification(msg)
        elif "id" in msg and "method" in msg:
            logger.info("DISPATCH req=%d method=%s", msg["id"], msg.get("method", "?"))
            await self._handle_request(msg)
            logger.info("DISPATCH_DONE req=%d method=%s", msg["id"], msg.get("method", "?"))

    async def _handle_request(self, msg: dict) -> None:
        """Handle a request, supporting sync and async handlers."""
        req_id = msg["id"]
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Register this task so the host can cancel it via request.cancel
        task = asyncio.current_task()
        if task is not None:
            self._active_requests[req_id] = task

        try:
            # --- Auth gate: nothing but plugin.auth before authentication ---
            if not self.authed and method != METHOD_PLUGIN_AUTH:
                await self._send_error(
                    req_id, AUTH_ERROR, "not authenticated — send plugin.auth first"
                )
                return
            if method == METHOD_PLUGIN_AUTH:
                await self._handle_auth(req_id, params)
                return

            # --- Built-in methods ---
            if method == METHOD_HEALTH:
                await self._send_response(req_id, {"status": "ok", "dependencies": {}})
                return
            if method == METHOD_SHUTDOWN:
                await self._send_response(req_id, "ok")
                await self.close()
                return
            if method == METHOD_TOOL_LIST:
                await self._send_response(req_id, self.runtime._tool_names)
                return
            if method == METHOD_CHECKER_LIST:
                await self._send_response(req_id, self.runtime._checker_names)
                return

            # --- Custom handlers ---
            handler = self.runtime._handlers.get(method)

            # Fall back to built-in tool.execute dispatcher when no custom
            # handler is registered.  Plugin authors can still override this
            # by registering their own "tool.execute" handler via self.on().
            if handler is None and method == METHOD_TOOL_EXECUTE:
                handler = self.runtime._default_tool_execute

            if handler is None:
                await self._send_error(req_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
                return

            # Determine handler type BEFORE calling, so we can run sync
            # handlers in a thread pool instead of blocking the event loop.
            #
            # inspect.iscoroutinefunction returns False for bound-method objects,
            # so unwrap to the underlying function first.
            _fn = handler.__func__ if hasattr(handler, "__func__") else handler
            if inspect.iscoroutinefunction(_fn):
                # Async handler — await directly in the running event loop so
                # cancellation propagates and the handler can use the same
                # loop-local state (e.g. asyncio.Queue, locks) as the runtime.
                value = await handler(params)
                await self._send_response(req_id, value)
            else:
                # Synchronous handler — offload to thread pool to avoid
                # blocking the event loop.  Even a "fast" sync handler can
                # accumulate latency when many requests arrive concurrently.
                logger.info(
                    "Running sync handler %s in thread executor (req=%d)",
                    method,
                    req_id,
                )
                loop = asyncio.get_running_loop()
                value = await loop.run_in_executor(None, handler, params)
                await self._send_response(req_id, value)

        except asyncio.CancelledError:
            # Host cancelled this request — don't send a response
            logger.info("Request %s (req=%d) cancelled by host", method, req_id)
            raise
        except Exception as e:
            logger.exception("Error handling method '%s'", method)
            await self._send_error(req_id, INTERNAL_ERROR, str(e))
        finally:
            self._active_requests.pop(req_id, None)

    async def _handle_auth(self, req_id: int, params: dict) -> None:
        """Verify the host's token; close the connection on mismatch."""
        token = params.get("token") if isinstance(params, dict) else None
        expected = self.runtime._token or ""
        if isinstance(token, str) and token and hmac.compare_digest(token, expected):
            self.authed = True
            logger.info("Host authenticated (peer=%s)", self.peer or "in-process")
            await self._send_response(req_id, {"ok": True})
            return
        logger.warning("Auth failed — closing connection (peer=%s)", self.peer or "in-process")
        await self._send_error(req_id, AUTH_ERROR, "invalid token")
        await self.close()

    async def _handle_notification(self, msg: dict) -> None:
        """Handle incoming notifications (e.g., shutdown, request.cancel)."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        # Custom handlers first; built-in methods keep their semantics.
        handler = self.runtime._notification_handlers.get(method)
        if handler is not None:
            task = asyncio.create_task(self._run_notification_handler(handler, params))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(_log_task_exception)
            return
        if method == METHOD_SHUTDOWN:
            await self.close()
        elif method == METHOD_HOST_SERVICES:
            services = params.get("host_services") or []
            self.runtime._host_services = list(services)
            if self.runtime._host_services:
                self.host_service_client = HostServiceClient(self.reader, self.writer)
                # Last connection wins; single-host is the supported topology.
                self.runtime._host_service_client = self.host_service_client
        elif method == METHOD_RUNTIME_CONTEXT:
            self.runtime._runtime_context = params
        elif method == "request.cancel":
            req_id = params.get("id")
            if req_id is not None:
                task = self._active_requests.get(req_id)
                if task is not None and not task.done():
                    logger.info("Cancelling request (req=%d) by host request", req_id)
                    task.cancel()

    async def _run_notification_handler(self, handler: Callable, params: dict) -> None:
        """Invoke a custom notification handler (sync or async)."""
        result = handler(params)
        if asyncio.iscoroutine(result):
            await result

    # ------------------------------------------------------------------ send

    async def _send_response(self, req_id: int, result: Any) -> None:
        msg = json.dumps({"id": req_id, "result": result}, ensure_ascii=False)
        await self._send_line(msg)

    async def _send_error(self, req_id: int, code: int, message: str) -> None:
        msg = json.dumps(
            {"id": req_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )
        await self._send_line(msg)

    async def _send_notification(self, method: str, params: dict) -> None:
        msg = {"method": method, "params": params}
        await self._send_line(json.dumps(msg, ensure_ascii=False))

    async def _send_line(self, line: str) -> None:
        """Write one line (UTF-8) to the connection."""
        try:
            self.writer.write((line + "\n").encode("utf-8"))
            if hasattr(self.writer, "drain"):
                await self.writer.drain()
            elif hasattr(self.writer, "flush"):
                self.writer.flush()
        except Exception:
            logger.exception("Failed to write to connection; marking it closed")
            self.closed = True


class PluginRuntime:
    """Base class for standalone plugin servers.

    Handlers registered via :meth:`on` are regular functions or coroutines
    that return a result dict (for tool/checker calls).

    Built-in methods (health, shutdown, tool.list, checker.list, auth) are
    handled automatically.

    Parameters
    ----------
    tool_names : list of str
        Tool names exposed by this plugin (used for ``tool.list`` responses).
    checker_names : list of str
        Checker names exposed by this plugin (used for ``checker.list`` responses).
    """

    def __init__(
        self,
        tool_names: list[str] | None = None,
        checker_names: list[str] | None = None,
    ) -> None:
        self._handlers: dict[str, Callable] = {}
        # Duck-typed I/O: asyncio.Queue / test doubles injected for testing.
        # When set, run() takes the in-process path (no network, no auth).
        self._reader: Any = None
        self._writer: Any = None
        self._tool_names: list[str] = tool_names or []
        self._checker_names: list[str] = checker_names or []
        self._pending_caps: list[dict[str, Any]] = []
        self._tool_instances: dict[str, Any] = {}
        self._system_prompt: str = ""
        self._caps: list[dict[str, Any]] = []
        self._host_services: list[str] = []
        self._host_service_client: HostServiceClient | None = None
        self._runtime_context: dict[str, str] = {}
        self._notification_handlers: dict[str, Callable] = {}
        self._token: str | None = None
        self._connections: set[_Connection] = set()
        self._server: asyncio.AbstractServer | None = None
        self._prepared = False

    @property
    def host_service_client(self) -> HostServiceClient | None:
        """Client for plugin-initiated host-service calls.

        Set once the host's ``plugin.host_services`` notification arrives
        (during registration, before any tool execution); ``None`` when the
        plugin declared no host services, the handshake has not run yet, or
        the owning connection dropped.  With multiple connections the most
        recently established one wins (single-host is the supported topology).
        """
        return self._host_service_client

    def register_tool(self, tool_instance: Any) -> dict[str, Any]:
        """Register a tool instance and auto-extract contract metadata.

        Extracts: name, display_name, description, parameters,
        output_artifact_type, input_fields, output_schema, runtime_policy,
        skip_persist, skip_ref_resolution, file_params.
        """
        cap: dict[str, Any] = {
            "type": "tool",
            "name": tool_instance.name,
            "display_name": getattr(tool_instance, "display_name", None),
            "description": tool_instance.description,
            "parameters": tool_instance.parameters,
        }

        # Auto-extract scalar contract fields
        for attr in (
            "output_artifact_type",
            "output_schema",
            "skip_persist",
            "skip_ref_resolution",
        ):
            value = getattr(tool_instance, attr, None)
            if value is not None:
                cap[attr] = value

        # file_params — mark file-bearing input properties so the host
        # rewrites them into minio:// references before dispatch.
        file_params = getattr(tool_instance, "file_params", None)
        if file_params:
            properties = cap.get("parameters", {}).get("properties") or {}
            for param in file_params:
                if param in properties:
                    properties[param] = {**properties[param], "format": "file-ref"}
            cap["file_params"] = list(file_params)

        # input_fields — serialize InputField objects to plain dicts
        input_fields = getattr(tool_instance, "input_fields", None)
        if input_fields:
            cap["input_fields"] = [
                {
                    "name": f.name,
                    "artifact_type": f.artifact_type,
                    "materialize_as": f.materialize_as,
                }
                for f in input_fields
            ]

        # runtime_policy
        rp = getattr(tool_instance, "runtime_policy", None)
        if rp is not None:
            cap["runtime_policy"] = {
                "max_calls": rp.max_calls,
                "max_consecutive": rp.max_consecutive,
            }

        self._pending_caps.append(cap)
        self._tool_instances[tool_instance.name] = tool_instance
        return cap

    def _collect_capabilities(self) -> tuple[list[dict[str, Any]], str]:
        """Collect caps from register_capabilities() AND register_tool() calls.

        Returns (capabilities_list, system_prompt).
        """
        caps_result = self.register_capabilities()
        system_prompt = ""
        if isinstance(caps_result, dict):
            caps = caps_result.get("capabilities", [])
            system_prompt = caps_result.get("system_prompt", "")
        elif isinstance(caps_result, list):
            caps = caps_result
        else:
            caps = []

        if not isinstance(caps, list):
            caps = []

        caps.extend(self._pending_caps)
        self._system_prompt = system_prompt
        return caps, system_prompt

    async def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a registered tool by name and return result dict."""
        tool = self._tool_instances.get(tool_name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        try:
            result = await tool.execute(**args)
            return {
                "success": getattr(result, "success", True),
                "data": getattr(result, "data", None),
                "error": getattr(result, "error", None),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _default_tool_execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Built-in fallback handler for ``tool.execute``.

        Dispatches to registered tool instances by name.  Plugin authors no
        longer need to write repetitive ``if tool_name == ...`` logic in
        their entry.py — unless they need custom pre/post-processing.

        Every dispatch runs inside a per-request workdir so resolve_file()
        downloads are cleaned up when the call finishes.
        """
        from .files import request_workdir

        tool_name = params.get("tool", "")
        args = params.get("args", {})
        async with request_workdir():
            return await self._execute_tool(tool_name, args)

    def _tool_skip_persist(self, tool_name: str) -> bool:
        """Return True if the tool is marked to skip automatic persistence."""
        tool = self._tool_instances.get(tool_name)
        if tool is None:
            return False
        return bool(getattr(tool, "skip_persist", False))

    def on(self, method: str) -> Callable:
        """Decorator: register a handler for a JSON-RPC method."""

        def decorator(fn: Callable) -> Callable:
            self._handlers[method] = fn
            return fn

        return decorator

    def on_notification(self, method: str) -> Callable:
        """Decorator: register a handler for a host→plugin notification.

        Notifications carry no request id and expect no response; handlers
        run as background tasks (sync or async) with exceptions logged.
        """

        def decorator(fn: Callable) -> Callable:
            self._notification_handlers[method] = fn
            return fn

        return decorator

    def register_capabilities(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Override: return the capabilities this plugin provides.

        Returns either:
        - A list of capability dicts (backward compatible)
        - A dict with "capabilities" (list) and optional "system_prompt" (str)
        """
        return []

    def _setup_handlers(self) -> None:
        """Override: register handlers using self.on()."""
        pass

    # ------------------------------------------------------------------ run

    def _prepare(self) -> None:
        """Collect handlers and capabilities exactly once (both entry paths)."""
        if self._prepared:
            return
        self._prepared = True
        self._setup_handlers()

        # Collect tool/checker names for built-in list handlers
        caps, _system_prompt = self._collect_capabilities()
        self._caps = caps

        for cap in caps:
            if cap.get("type") == "tool" and "name" in cap:
                name = cap["name"]
                if name not in self._tool_names:
                    self._tool_names.append(name)
            elif cap.get("type") == "checker" and "name" in cap:
                name = cap["name"]
                if name not in self._checker_names:
                    self._checker_names.append(name)

    async def run(self) -> None:
        """Start the plugin: TCP server in production, in-process loop in tests."""
        # Ensure INFO-level logs are visible (plugins own their stdout/stderr
        # now — there is no host-side stderr capture anymore).
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
            force=False,  # don't override if plugin configures its own
        )

        if self._reader is not None or self._writer is not None:
            # In-process test path: duck-typed I/O injection, no auth gate
            # (there is no trust boundary inside a single process).
            self._prepare()
            conn = _Connection(self, self._reader, self._writer, require_auth=False)
            await conn.serve()
            return

        await self.serve()

    async def serve(self, listen: str | None = None) -> None:
        """Run the standalone TCP server until SIGTERM/SIGINT.

        ``listen`` (``host:port``) overrides CLI/env/manifest resolution.
        """
        self._prepare()
        self._token = os.environ.get(ENV_PLUGIN_TOKEN) or None
        if not self._token:
            logger.error(
                "%s is not set — refusing to start (the host authenticates "
                "itself and this plugin with a shared token)",
                ENV_PLUGIN_TOKEN,
            )
            raise SystemExit(1)

        manifest = self._load_manifest()
        self._apply_manifest_env(manifest)

        from . import files

        files.configure(plugin_name=str(manifest.get("name") or "plugin"))

        host, port = self._resolve_listen(listen, manifest)
        self._server = await asyncio.start_server(
            self._accept,
            host,
            port,
            limit=STREAM_LIMIT_BYTES,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        added_signals: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                added_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass  # Windows / non-main thread — Ctrl+C still works via KeyboardInterrupt

        sockets = ", ".join(str(s.getsockname()) for s in (self._server.sockets or []))
        logger.info("Plugin server listening on %s", sockets)
        try:
            async with self._server:
                await stop.wait()
        finally:
            # Remove our handlers before the loop closes or is reused (test
            # runners cycle event loops per test).
            for sig in added_signals:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            logger.info("Shutting down: closing %d connection(s)", len(self._connections))
            await asyncio.gather(
                *(conn.close() for conn in list(self._connections)), return_exceptions=True
            )

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one inbound host connection."""
        conn = _Connection(
            self,
            reader,
            writer,
            peer=writer.get_extra_info("peername"),
            require_auth=True,
        )
        self._connections.add(conn)
        try:
            await conn.serve()
        finally:
            self._connections.discard(conn)

    # ------------------------------------------------------------- manifest

    @staticmethod
    def _load_manifest() -> dict[str, Any]:
        """Load plugin.yaml from the current working directory (if present)."""
        path = Path.cwd() / "plugin.yaml"
        if not path.is_file():
            return {}
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.warning("Failed to parse %s", path, exc_info=True)
            return {}

    def _apply_manifest_env(self, manifest: dict[str, Any]) -> None:
        """Apply the manifest's ``runtime.env`` block to the process env.

        Plugins own their environment now — the host injects nothing.
        Literal values in the manifest are applied as defaults
        (``os.environ.setdefault``) so in-manifest tuning knobs keep working
        without deployment config; libraries resolve their own code defaults
        for everything else.  Legacy ``${ENV:...}`` markers are ignored (the
        plugin simply reads its own env at use sites).
        """
        env_block = (manifest.get("runtime") or {}).get("env") or {}
        if not isinstance(env_block, dict):
            return
        for key, raw in env_block.items():
            if isinstance(raw, str) and raw.strip().startswith("${ENV:"):
                continue
            if raw is not None:
                os.environ.setdefault(key, str(raw))

    @staticmethod
    def _resolve_listen(listen: str | None, manifest: dict[str, Any]) -> tuple[str, int]:
        """Resolve ``(host, port)``: arg > --listen CLI > env > manifest port."""
        candidate = listen or _listen_arg() or os.environ.get(ENV_PLUGIN_LISTEN)
        if candidate:
            host, sep, port = candidate.rpartition(":")
            if not sep or not port.isdigit():
                logger.error("Invalid listen address %r — expected host:port", candidate)
                raise SystemExit(1)
            return host or "0.0.0.0", int(port)
        port = (manifest.get("runtime") or {}).get("port")
        if isinstance(port, int) and port > 0:
            return "0.0.0.0", port
        logger.error(
            "No listen address: pass --listen host:port, set %s, or declare "
            "runtime.port in plugin.yaml",
            ENV_PLUGIN_LISTEN,
        )
        raise SystemExit(1)


def _listen_arg() -> str | None:
    """Extract ``--listen host:port`` / ``--listen=host:port`` from sys.argv."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--listen" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--listen="):
            return arg.split("=", 1)[1]
    return None
