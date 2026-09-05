"""JSONRPCClient — manages a single JSON-RPC connection to a plugin subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..prompts.errors import render_error
from .protocol import JSONRPCNotification, JSONRPCRequest

logger = logging.getLogger(__name__)


class PluginRPCError(Exception):
    """Wraps a JSON-RPC error from a plugin."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class PluginCrashedError(Exception):
    """Raised when a plugin subprocess exits unexpectedly."""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        super().__init__(render_error("errors.plugin_crashed", plugin_name=plugin_name))


class JSONRPCClient:
    """Manages a single JSON-RPC over stdio connection to one plugin subprocess.

    Parameters
    ----------
    reader : asyncio.StreamReader or compatible
        Readable stream (subprocess stdout).
    writer : asyncio.StreamWriter or compatible
        Writable stream (subprocess stdin).
    plugin_name : str
        Human-readable name for log messages.
    on_disconnect : callable, optional
        Async callback invoked when the subprocess disconnects (EOF on stdout).
        Signature: ``async def on_disconnect() -> None``.
        This is where ProcessManager wires crash recovery.
    """

    def __init__(
        self,
        reader: Any,
        writer: Any,
        plugin_name: str,
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        host_request_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.plugin_name = plugin_name
        self._on_disconnect = on_disconnect
        self._host_request_handler = host_request_handler
        self._default_timeout = default_timeout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._response_buffer: dict[int, list[dict]] = {}
        self._session_by_request: dict[int, str] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._register_event: asyncio.Event | None = None
        self._register_caps: list[dict] | None = None
        self._register_system_prompt: str = ""
        # Token the plugin presented in its register notification; the
        # manager verifies it against the shared secret (mutual auth).
        self._register_token: str = ""

    def set_host_request_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None,
    ) -> None:
        """Register a handler for plugin-to-host JSON-RPC requests."""
        self._host_request_handler = handler

    async def _ensure_reader(self) -> None:
        """Start the background reader task if not already running."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Continuously read lines from stdin, dispatch to pending futures."""
        try:
            while not self._closed:
                try:
                    line = await self._reader.readline()
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    break
                except ValueError as exc:
                    # StreamReader limit overrun (a single response line larger
                    # than the stream limit) lands here.  Without this log the
                    # failure is silent and looks exactly like a plugin crash.
                    logger.error(
                        "Read error for plugin '%s' — likely a response line "
                        "exceeding the stream limit: %s",
                        self.plugin_name,
                        exc,
                    )
                    break
                except Exception:
                    logger.warning(
                        "Unexpected exception in %s read loop, closing connection",
                        self.plugin_name,
                        exc_info=True,
                    )
                    break

                if not line:  # EOF — subprocess exited
                    self._fail_all_pending(PluginCrashedError(self.plugin_name))
                    # Notify ProcessManager for crash recovery
                    if self._on_disconnect is not None:
                        try:
                            await self._on_disconnect()
                        except Exception:
                            logger.warning(
                                "on_disconnect callback failed for plugin '%s'",
                                self.plugin_name,
                                exc_info=True,
                            )
                    break

                try:
                    line_str = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    logger.warning(
                        "UTF-8 decode failed for plugin '%s' stdout line",
                        self.plugin_name,
                    )
                    continue

                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning(
                        "JSON decode failed for plugin '%s': %.200s",
                        self.plugin_name,
                        line_str,
                    )
                    continue

                await self._dispatch(data)
        finally:
            # Ensure every exit path (network errors, unexpected exceptions,
            # EOF) fails pending futures and triggers crash recovery.
            self._fail_all_pending(PluginCrashedError(self.plugin_name))
            if self._on_disconnect is not None:
                try:
                    await self._on_disconnect()
                except Exception:
                    logger.debug(
                        "on_disconnect callback failed for plugin '%s'",
                        self.plugin_name,
                        exc_info=True,
                    )

    async def _dispatch(self, data: dict) -> None:
        """Route an incoming message to a pending future or notification handler.

        Also dispatches plugin-to-host JSON-RPC requests to the registered
        host request handler and writes the response back to the plugin.
        """
        if "id" in data and "method" in data:
            await self._handle_host_request(data)
            return
        if "id" in data and "method" not in data:
            # It's a response or streaming chunk
            msg_id = data["id"]
            future = self._pending.get(msg_id)
            if future is None or future.done():
                if future is None:
                    # No future in _pending — buffer for next stream iteration.
                    bucket = self._response_buffer.setdefault(msg_id, [])
                    # Late responses for timed-out ids are never consumed
                    # (request ids are strictly increasing) — cap each so a
                    # few oversized stale payloads cannot balloon memory.
                    if len(bucket) < 4:
                        bucket.append(data)
                else:
                    # Future exists but already resolved (done=True).
                    # This is the end-chunk race: chunk2 arrived after chunk1
                    # resolved the future but before the stream loop popped it.
                    # Buffer it so the next iteration can pick it up.
                    self._response_buffer.setdefault(msg_id, []).append(data)
                return
            if "error" in data and data["error"] is not None:
                err = data["error"]
                future.set_exception(
                    PluginRPCError(err.get("code", -1), err.get("message", "Unknown error"))
                )
            else:
                future.set_result(data)
            return
        if "method" in data and "id" not in data:
            self._handle_notification(data)

    async def _handle_host_request(self, data: dict) -> None:
        """Handle a plugin-to-host JSON-RPC request and write the response."""
        from .protocol import INTERNAL_ERROR, METHOD_NOT_FOUND

        req_id = data["id"]
        method = data.get("method", "")
        params = data.get("params", {})

        if self._host_request_handler is None:
            self._send_error_response(
                req_id, METHOD_NOT_FOUND, f"Host service '{method}' not available"
            )
            await self._writer.drain()
            return

        try:
            result = await self._host_request_handler({"method": method, "params": params})
        except Exception as exc:
            logger.exception("Host request handler failed for method '%s'", method)
            self._send_error_response(req_id, INTERNAL_ERROR, str(exc))
            await self._writer.drain()
            return

        self._send_response(req_id, result)
        await self._writer.drain()

    def _send_response(self, req_id: int, result: Any) -> None:
        """Write a JSON-RPC response to the plugin."""
        msg = json.dumps({"id": req_id, "result": result}, ensure_ascii=False)
        self._send_line(msg)

    def _send_error_response(self, req_id: int, code: int, message: str) -> None:
        """Write a JSON-RPC error response to the plugin."""
        msg = json.dumps(
            {"id": req_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )
        self._send_line(msg)

    def _send_line(self, line: str) -> None:
        """Write a line to the plugin subprocess.

        Raises on write failure so host-request responses do not silently
        disappear when the plugin has crashed.
        """
        self._writer.write((line + "\n").encode("utf-8"))

    def _handle_notification(self, data: dict) -> None:
        """Handle an incoming notification."""
        method = data.get("method", "")
        params = data.get("params", {})
        if method == "plugin.register":
            caps = params.get("capabilities", [])
            self._register_caps = caps
            self._register_system_prompt = params.get("system_prompt", "")
            self._register_token = params.get("token") or ""
            if self._register_event is not None:
                self._register_event.set()

    def _fail_all_pending(self, exc: BaseException) -> None:
        """Reject all pending futures (called on disconnect/crash).

        Accepts ``BaseException`` because session cancellation fails futures
        with ``asyncio.CancelledError``, which is not an ``Exception``
        subclass.
        """
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        *,
        session_id: str | None = None,
    ) -> Any:
        """Send a JSON-RPC request and wait for the response.

        ``session_id`` tags the request so :meth:`cancel_pending` can scope
        cancellations to a single session.
        """
        if timeout is None:
            timeout = self._default_timeout

        if self._closed:
            raise PluginCrashedError(self.plugin_name)

        await self._ensure_reader()

        self._next_id += 1
        req_id = self._next_id
        if session_id:
            self._session_by_request[req_id] = session_id

        request = JSONRPCRequest(id=req_id, method=method, params=params or {})

        try:
            self._writer.write((request.model_dump_json() + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as e:
            raise PluginCrashedError(self.plugin_name) from e

        # Check if response already arrived (buffered by _dispatch)
        if req_id in self._response_buffer:
            data_list = self._response_buffer.pop(req_id)
            data = data_list[-1]  # Last response wins for call()
            if "error" in data and data["error"] is not None:
                err = data["error"]
                raise PluginRPCError(err.get("code", -1), err.get("message", "Unknown error"))
            return data.get("result")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            raw = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            # Bare TimeoutError stringifies to "" — the model would see an
            # empty error.  Re-raise with an actionable message.
            raise asyncio.TimeoutError(
                render_error(
                    "errors.plugin_call_timeout", plugin_name=self.plugin_name, timeout=timeout
                )
            ) from None

        # The _dispatch sets the raw response dict as the future result.
        # Extract the "result" field for regular responses.
        if isinstance(raw, dict):
            return raw.get("result")
        return raw

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a one-way notification (no response expected)."""
        if self._closed:
            return
        notif = JSONRPCNotification(method=method, params=params or {})
        try:
            self._writer.write((notif.model_dump_json() + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception:
            logger.warning(
                "Failed to send notification '%s' to plugin '%s'",
                method,
                self.plugin_name,
                exc_info=True,
            )

    async def wait_for_register(self, timeout: float = 10.0) -> list[dict]:
        """Wait for the plugin.register notification and return capabilities."""
        if self._closed:
            raise PluginCrashedError(self.plugin_name)

        await self._ensure_reader()

        # If caps already arrived before we created the event, return them immediately.
        if self._register_caps is not None:
            return self._register_caps

        self._register_event = asyncio.Event()

        # Race: _dispatch may set the event between the check above and now.
        # Re-check after assigning _register_event.
        if self._register_caps is not None:
            return self._register_caps

        try:
            await asyncio.wait_for(self._register_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if self._closed:
                raise PluginCrashedError(self.plugin_name)
            raise asyncio.TimeoutError(
                f"Plugin '{self.plugin_name}' did not send register notification within {timeout}s"
            )
        # _handle_notification assigns _register_caps before setting the
        # event, so it is populated here; fall back to [] defensively.
        return self._register_caps or []

    async def cancel_pending(self, session_id: str | None = None) -> None:
        """Cancel pending requests, optionally scoped to one session.

        Sends a ``request.cancel`` notification for each matching pending
        request ID, then cancels those pending futures.  Does NOT close the
        connection — the plugin stays alive for future sessions.
        """
        if session_id is None:
            pending_ids = list(self._pending.keys())
        else:
            pending_ids = [
                req_id
                for req_id, sid in self._session_by_request.items()
                if sid == session_id and req_id in self._pending
            ]
        for req_id in pending_ids:
            await self.notify("request.cancel", {"id": req_id})
            future = self._pending.pop(req_id, None)
            if future is not None and not future.done():
                future.set_exception(asyncio.CancelledError("Session cancelled"))
            self._session_by_request.pop(req_id, None)
            self._response_buffer.pop(req_id, None)

    def close(self) -> None:
        """Close the writer and cancel the reader."""
        self._closed = True
        self._fail_all_pending(PluginCrashedError(self.plugin_name))
        self._response_buffer.clear()
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        try:
            self._writer.close()
        except Exception:
            logger.debug(
                "Error closing writer for plugin '%s'",
                self.plugin_name,
                exc_info=True,
            )
