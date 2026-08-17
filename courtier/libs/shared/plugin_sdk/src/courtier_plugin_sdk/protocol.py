"""Protocol constants shared between host and plugin SDK."""

# Method names (host → plugin)
METHOD_HEALTH = "plugin.health"
METHOD_SHUTDOWN = "plugin.shutdown"
METHOD_PLUGIN_AUTH = "plugin.auth"
METHOD_TOOL_LIST = "tool.list"
METHOD_TOOL_EXECUTE = "tool.execute"
METHOD_CHECKER_LIST = "checker.list"
METHOD_CHECKER_CHECK = "checker.check"

# Method names (plugin → host, notifications)
METHOD_REGISTER = "plugin.register"

# Host → plugin notifications
METHOD_HOST_SERVICES = "plugin.host_services"
METHOD_RUNTIME_CONTEXT = "plugin.runtime_context"

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

# Stream read limit (bytes) for the JSON-RPC line protocol on both ends.
# Responses carry full parsed documents / large search result sets on a
# single line; the asyncio default (64 KiB) is far too small and used to
# kill healthy plugins via LimitOverrunError.  64 MiB is generous but still
# bounded against a runaway plugin.
STREAM_LIMIT_BYTES = 64 * 1024 * 1024

# Error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT_ERROR = -32000
TOOL_NOT_FOUND = -32001
PLUGIN_CRASHED = -32002
AUTH_ERROR = -32003
