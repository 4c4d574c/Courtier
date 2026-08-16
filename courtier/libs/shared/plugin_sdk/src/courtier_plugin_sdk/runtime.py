"""PluginRuntime — base class for plugin subprocess entry points.

Plugin authors subclass PluginRuntime, register capabilities and
handlers, then call ``await runtime.run()`` in their entry.py.

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

            @self.on("checker.check")
            def handle_checker_check(params):
                return {"is_valid": True, "violations": []}

    if __name__ == "__main__":
        import asyncio
        asyncio.run(MyPlugin().run())
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

from .protocol import (
    INTERNAL_ERROR,
    METHOD_CHECKER_LIST,
    METHOD_HEALTH,
    METHOD_HOST_SERVICES,
    METHOD_NOT_FOUND,
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

    Uses negative request IDs so the main PluginRuntime dispatcher can
    route host responses back to this client without colliding with the
    positive IDs used for host→plugin requests.
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
        # (sys.stdout.buffer / test writers).  HostServiceClient may
        # receive either depending on whether PluginRuntime is running
        # in-process or as a subprocess.
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


def _log_task_exception(task: asyncio.Task) -> None:
    """Log any unhandled exception from a concurrently processed request."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Unhandled exception in plugin request task: %s", exc, exc_info=exc)


class PluginRuntime:
    """Base class for plugin subprocess entry points.

    Handlers registered via :meth:`on` are regular functions or coroutines
    that return a result dict (for tool/checker calls).

    Built-in methods (health, shutdown, tool.list, checker.list) are
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
        self._running = False
        # Duck-typed I/O: asyncio.Queue / BinaryIO / test doubles are injected
        # for testing; sys.stdin.buffer / sys.stdout.buffer in production.
        self._reader: Any = None
        self._writer: Any = None
        self._tool_names: list[str] = tool_names or []
        self._checker_names: list[str] = checker_names or []
        self._pending_caps: list[dict[str, Any]] = []
        self._tool_instances: dict[str, Any] = {}
        self._system_prompt: str = ""
        self._pending_tasks: set[asyncio.Task] = set()
        self._active_requests: dict[int, asyncio.Task] = {}  # req_id → task
        self._host_services: list[str] = []
        self._host_service_client: HostServiceClient | None = None
        self._host_artifact_store: Any = None
        self._runtime_context: dict[str, str] = {}
        self._stdin_transport: asyncio.ReadTransport | None = None
        self._notification_handlers: dict[str, Callable] = {}

    @property
    def host_service_client(self) -> HostServiceClient | None:
        """Client for plugin-initiated host-service calls.

        Set once the host's ``plugin.host_services`` notification arrives
        (during registration, before any tool execution); ``None`` when the
        plugin declared no host services or the handshake has not run yet.
        """
        return self._host_service_client

    def register_tool(self, tool_instance: Any) -> dict[str, Any]:
        """Register a tool instance and auto-extract contract metadata.

        Extracts: name, display_name, description, parameters,
        output_artifact_type, input_fields, output_schema, runtime_policy,
        skip_persist, skip_ref_resolution.
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
        """
        tool_name = params.get("tool", "")
        args = params.get("args", {})
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

    async def run(self) -> None:
        """Start the JSON-RPC server loop on stdin/stdout."""
        # Ensure INFO-level logs are visible through the host's stderr capture.
        # Python defaults to WARNING; without this, diagnostic logs are silent.
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
            force=False,  # don't override if plugin configures its own
        )

        self._setup_handlers()

        # Collect tool/checker names for built-in list handlers
        caps, system_prompt = self._collect_capabilities()

        for cap in caps:
            if cap.get("type") == "tool" and "name" in cap:
                name = cap["name"]
                if name not in self._tool_names:
                    self._tool_names.append(name)
            elif cap.get("type") == "checker" and "name" in cap:
                name = cap["name"]
                if name not in self._checker_names:
                    self._checker_names.append(name)

        # Allow injection of reader/writer for testing.
        # Use binary I/O for consistency with the host's asyncio.StreamReader/Writer.
        if self._reader is None:
            self._reader = sys.stdin.buffer
        if self._writer is None:
            self._writer = sys.stdout.buffer

        self._running = True

        # Send registration notification
        self._send_notification(
            METHOD_REGISTER,
            {
                "capabilities": caps,
                "system_prompt": system_prompt,
            },
        )

        # Process requests line by line
        if isinstance(self._reader, asyncio.Queue):
            await self._run_queue_loop()
        else:
            await self._run_stdin_loop()

        # On shutdown, wait briefly for pending tasks to finish
        if self._pending_tasks:
            logger.debug("Waiting for %d pending task(s) to finish...", len(self._pending_tasks))
            await asyncio.wait(self._pending_tasks, timeout=5.0)
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()

    async def _run_queue_loop(self) -> None:
        """Read lines from an asyncio.Queue (used in tests)."""
        while True:
            line = await self._reader.get()
            if not self._running:
                break
            line = line.strip()
            if not line:
                break
            await self._process_line(line)

    async def _run_stdin_loop(self) -> None:
        """Read lines from stdin using asyncio streams (binary mode)."""
        loop = asyncio.get_event_loop()
        # Requests can carry large arguments (e.g. embedded document text) on
        # a single JSON-RPC line — use the shared protocol limit instead of
        # the 64 KiB default.
        reader = asyncio.StreamReader(limit=STREAM_LIMIT_BYTES)
        # connect_read_pipe returns a (transport, protocol) pair; keep the
        # transport so it can be closed on shutdown.
        transport, _protocol = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            sys.stdin.buffer,
        )
        self._stdin_transport = transport
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                # Process requests concurrently so long-running tool.execute
                # calls don't block other requests.
                logger.info("READ line: %s", _sanitize_rpc_log(line_str))
                task = asyncio.create_task(self._process_line_safe(line_str))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
                task.add_done_callback(_log_task_exception)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    logger.debug("Error closing stdin transport", exc_info=True)

    async def _process_line(self, line: str) -> None:
        """Process a single JSON-RPC line."""
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
            if isinstance(msg_id, int) and msg_id < 0 and self._host_service_client is not None:
                self._host_service_client.dispatch_response(msg)
            return

        if "method" in msg and "id" not in msg:
            self._handle_notification(msg)
        elif "id" in msg and "method" in msg:
            logger.info("DISPATCH req=%d method=%s", msg["id"], msg.get("method", "?"))
            await self._handle_request_async(msg)
            logger.info("DISPATCH_DONE req=%d method=%s", msg["id"], msg.get("method", "?"))

    async def _process_line_safe(self, line: str) -> None:
        """Wrapper that logs exceptions from concurrent task processing."""
        try:
            await self._process_line(line)
        except Exception:
            logger.exception("Unhandled error processing request: %s", line[:200])

    async def _handle_request_async(self, msg: dict) -> None:
        """Handle a request, supporting sync and async handlers."""
        req_id = msg["id"]
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Register this task so the host can cancel it via request.cancel
        task = asyncio.current_task()
        if task is not None:
            self._active_requests[req_id] = task

        try:
            # --- Built-in methods ---
            if method == METHOD_HEALTH:
                self._send_response(req_id, {"status": "ok", "dependencies": {}})
                return
            if method == METHOD_SHUTDOWN:
                self._running = False
                self._send_response(req_id, "ok")
                return
            if method == METHOD_TOOL_LIST:
                self._send_response(req_id, self._tool_names)
                return
            if method == METHOD_CHECKER_LIST:
                self._send_response(req_id, self._checker_names)
                return

            # --- Custom handlers ---
            handler = self._handlers.get(method)

            # Fall back to built-in tool.execute dispatcher when no custom
            # handler is registered.  Plugin authors can still override this
            # by registering their own "tool.execute" handler via self.on().
            if handler is None and method == METHOD_TOOL_EXECUTE:
                handler = self._default_tool_execute

            if handler is None:
                self._send_error(req_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
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
                self._send_response(req_id, value)
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
                self._send_response(req_id, value)

        except asyncio.CancelledError:
            # Host cancelled this request — don't send a response
            logger.info("Request %s (req=%d) cancelled by host", method, req_id)
            raise
        except Exception as e:
            logger.exception("Error handling method '%s'", method)
            self._send_error(req_id, INTERNAL_ERROR, str(e))
        finally:
            self._active_requests.pop(req_id, None)

    async def _run_notification_handler(self, handler: Callable, params: dict) -> None:
        """Invoke a custom notification handler (sync or async)."""
        result = handler(params)
        if asyncio.iscoroutine(result):
            await result

    def _handle_notification(self, msg: dict) -> None:
        """Handle incoming notifications (e.g., shutdown, request.cancel)."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        # Custom handlers first; built-in methods keep their semantics.
        handler = self._notification_handlers.get(method)
        if handler is not None:
            task = asyncio.create_task(self._run_notification_handler(handler, params))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            task.add_done_callback(_log_task_exception)
            return
        if method == METHOD_SHUTDOWN:
            self._running = False
            # The stdin loop blocks in readline(); closing the transport
            # wakes it with EOF so the process actually exits on shutdown.
            if self._stdin_transport is not None:
                self._stdin_transport.close()
        elif method == METHOD_HOST_SERVICES:
            services = msg.get("params", {}).get("host_services") or []
            self._host_services = list(services)
            if self._host_services and self._reader is not None and self._writer is not None:
                self._host_service_client = HostServiceClient(self._reader, self._writer)
        elif method == METHOD_RUNTIME_CONTEXT:
            self._runtime_context = msg.get("params", {})
        elif method == "request.cancel":
            req_id = msg.get("params", {}).get("id")
            if req_id is not None:
                task = self._active_requests.get(req_id)
                if task is not None and not task.done():
                    logger.info("Cancelling request (req=%d) by host request", req_id)
                    task.cancel()

    def _send_response(self, req_id: int, result: Any) -> None:
        msg = json.dumps({"id": req_id, "result": result}, ensure_ascii=False)
        self._send_line(msg)

    def _send_error(self, req_id: int, code: int, message: str) -> None:
        msg = json.dumps(
            {"id": req_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )
        self._send_line(msg)

    def _send_notification(self, method: str, params: dict) -> None:
        msg = {"method": method, "params": params}
        self._send_line(json.dumps(msg, ensure_ascii=False))

    def _send_line(self, line: str) -> None:
        """Write a single line (as UTF-8 bytes) to stdout.

        Uses ``sys.stdout.buffer`` for consistent binary I/O — the host
        reads plugin stdout as a binary stream (asyncio.StreamReader),
        so writing text-mode ``sys.stdout`` can cause encoding mismatches
        or buffering inconsistencies across platforms.
        """
        if self._writer is None:
            raise RuntimeError("Plugin not started — call run() before sending messages")
        try:
            self._writer.write((line + "\n").encode("utf-8"))
            self._writer.flush()
        except Exception:
            logger.exception("Failed to write to stdout; marking runtime as stopped")
            self._running = False
