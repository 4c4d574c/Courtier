"""Agent hooks — extensible observer/interceptor chain."""

from .chain import (
    POST_OBSERVE,
    PRE_SEARCH,
    PRE_THINK,
    HookChain,
    HookContext,
    HookEvent,
    HookHandle,
    HookHandler,
    HookObserver,
)

__all__ = [
    "HookChain",
    "HookContext",
    "HookEvent",
    "HookHandle",
    "HookHandler",
    "HookObserver",
    "POST_OBSERVE",
    "PRE_SEARCH",
    "PRE_THINK",
]
