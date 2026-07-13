"""HookChain — extensible observer/interceptor pattern."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, NewType

from ..core.state import AgentState

logger = logging.getLogger(__name__)

# Type-safe event names — NewType is a plain str at runtime.
HookEvent = NewType("HookEvent", str)

PRE_THINK: HookEvent = HookEvent("pre_think")
POST_OBSERVE: HookEvent = HookEvent("post_observe")
PRE_SEARCH: HookEvent = HookEvent("pre_search")

# Handler types
HookHandler = Callable[["HookContext"], Coroutine[Any, Any, AgentState]]
HookObserver = Callable[["HookContext"], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class HookContext:
    """Context passed to hook handlers."""

    event: str
    state: AgentState
    agent_name: str = ""
    session_id: str = ""
    current_step: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class HookHandle:
    """Cancelable handle returned by register() / register_observer()."""

    def __init__(
        self,
        chain: "HookChain",
        event: str,
        handler: HookHandler | HookObserver,
    ) -> None:
        self._chain = chain
        self._event = event
        self._handler = handler
        self._cancelled = False

    def cancel(self) -> None:
        """Remove the handler from the chain."""
        if not self._cancelled:
            self._chain.unregister(self._event, self._handler)
            self._cancelled = True


class HookChain:
    """可扩展的观测者/拦截器链。

    Two handler types:
    - Interceptors (HookHandler): return AgentState, pipeline mode.
    - Observers (HookObserver): return None, fire-and-forget.

    Handlers run in priority order (higher first).
    Default behavior: pass-through (no-op for unregistered events).
    """

    def __init__(
        self,
        handler_timeout: float | None = None,
        continue_on_error: bool = True,
    ) -> None:
        # Interceptors: (priority, registration_order, handler, timeout)
        self._handlers: dict[
            str, list[tuple[int, int, HookHandler, float | None]]
        ] = defaultdict(list)
        # Observers: flat list
        self._observers: dict[str, list[HookObserver]] = defaultdict(list)
        self._handler_counter: int = 0
        self._handler_timeout = handler_timeout
        self._continue_on_error = continue_on_error
        # Session-level context
        self._agent_name = ""
        self._session_id = ""
        self._metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def set_context(
        self,
        *,
        agent_name: str = "",
        session_id: str = "",
        **metadata: Any,
    ) -> None:
        """Set session-level context available to all handlers via HookContext."""
        self._agent_name = agent_name
        self._session_id = session_id
        self._metadata = metadata

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        event: str,
        handler: HookHandler,
        *,
        priority: int = 0,
        timeout: float | None = None,
    ) -> HookHandle:
        """Register an interceptor handler for the given event.

        Interceptors run in priority order (higher first) and each receives
        the state returned by the previous handler (pipeline mode).

        Args:
            event: Event name to listen for.
            handler: Async callable receiving HookContext, returning AgentState.
            priority: Higher values run first (default 0).
            timeout: Per-handler timeout in seconds (None = no timeout).

        Returns:
            HookHandle that can be used to cancel the registration.
        """
        entry = (priority, self._handler_counter, handler, timeout)
        self._handlers[event].append(entry)
        self._handler_counter += 1
        return HookHandle(self, event, handler)

    def register_observer(
        self, event: str, handler: HookObserver
    ) -> HookHandle:
        """Register an observer handler for the given event.

        Observers run after all interceptors. They cannot modify state —
        useful for logging, metrics, auditing, etc.

        Args:
            event: Event name to listen for.
            handler: Async callable receiving HookContext, returning None.

        Returns:
            HookHandle that can be used to cancel the registration.
        """
        self._observers[event].append(handler)
        return HookHandle(self, event, handler)

    def unregister(self, event: str, handler: HookHandler | HookObserver) -> None:
        """Remove a previously registered handler.

        Called by HookHandle.cancel().  Does nothing if the handler
        is not found (idempotent).
        """
        entries = self._handlers.get(event, [])
        for i, (_, _, h, _) in enumerate(entries):
            if h is handler:
                del entries[i]
                return
        obs: list[HookObserver] = self._observers.get(event, [])
        for i, h in enumerate(obs):
            if h is handler:
                del obs[i]
                return

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def run(
        self,
        event: str,
        state: AgentState,
        *,
        continue_on_error: bool | None = None,
    ) -> AgentState:
        """Run all handlers for the given event.

        Interceptors run first (in priority order), then observers.
        Each interceptor receives the state from the previous handler.
        If no handlers registered, returns state unchanged.

        Args:
            event: Event name to trigger.
            state: Current AgentState.
            continue_on_error: Override chain-level error-handling policy.

        Returns:
            The (possibly modified) AgentState.
        """
        cont = (
            continue_on_error
            if continue_on_error is not None
            else self._continue_on_error
        )
        current_state = state

        # Interceptors — sorted by priority descending, stable by registration order
        entries = self._handlers.get(event, [])
        if entries:
            sorted_entries = sorted(entries, key=lambda x: (-x[0], x[1]))
            for _priority, _order, handler, _timeout in sorted_entries:
                ctx = self._make_context(event, current_state)
                try:
                    timeout = (
                        _timeout if _timeout is not None else self._handler_timeout
                    )
                    if timeout is not None:
                        current_state = await asyncio.wait_for(
                            handler(ctx), timeout=timeout
                        )
                    else:
                        current_state = await handler(ctx)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Hook handler timed out after %.1fs: event=%s",
                        timeout,
                        event,
                    )
                    if not cont:
                        raise
                except Exception:
                    logger.exception(
                        "Hook handler raised exception: event=%s", event
                    )
                    if not cont:
                        raise

        # Observers — fire-and-forget, exceptions always caught
        for observer in self._observers.get(event, []):
            ctx = self._make_context(event, current_state)
            try:
                await observer(ctx)
            except Exception:
                logger.exception(
                    "Hook observer raised exception: event=%s", event
                )

        return current_state

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_context(self, event: str, state: AgentState) -> HookContext:
        return HookContext(
            event=event,
            state=state,
            agent_name=self._agent_name,
            session_id=self._session_id,
            current_step=state.current_step,
            metadata=dict(self._metadata),
        )
