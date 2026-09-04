"""JSON-RPC 2.0-style message types for plugin communication."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class JSONRPCRequest(BaseModel):
    """A request from host to plugin (or vice versa)."""

    id: int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCResponse(BaseModel):
    """A response to a request."""

    id: int
    result: Any = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_result_or_error(self):
        has_result = self.result is not None
        has_error = self.error is not None
        if has_result and has_error:
            raise ValueError("Response cannot have both result and error")
        if not has_result and not has_error:
            raise ValueError("Response must have either result or error")
        return self

    @property
    def is_error(self) -> bool:
        return self.error is not None


class JSONRPCNotification(BaseModel):
    """A one-way notification (no id, no response expected)."""

    method: str
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_notification(self) -> bool:
        return True


# Method names (host → plugin, requests)
METHOD_HEALTH = "plugin.health"
METHOD_PLUGIN_AUTH = "plugin.auth"

# Method names (plugin → host, notifications)
METHOD_REGISTER = "plugin.register"

# Host → plugin notifications
METHOD_HOST_SERVICES = "plugin.host_services"
METHOD_RUNTIME_CONTEXT = "plugin.runtime_context"

# Error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT_ERROR = -32000
TOOL_NOT_FOUND = -32001
PLUGIN_CRASHED = -32002
AUTH_ERROR = -32003

# Host service method names (plugin → host requests)
METHOD_CACHE_PERSIST = "cache.persist"
METHOD_CACHE_LOAD = "cache.load"
METHOD_CACHE_RESOLVE = "cache.resolve"
METHOD_CACHE_MICRO_COMPACT = "cache.micro_compact"
METHOD_ARTIFACT_STORE_PUT = "artifact_store.put"
METHOD_ARTIFACT_STORE_GET = "artifact_store.get"
METHOD_ARTIFACT_STORE_LIST = "artifact_store.list"
METHOD_STORAGE_PUT = "storage.put"
METHOD_STORAGE_PRESIGN_GET = "storage.presign_get"
METHOD_TEMPLATE_STORE_GET = "template_store.get"
METHOD_MEMORY_STORE_CALL = "memory_store.call"
