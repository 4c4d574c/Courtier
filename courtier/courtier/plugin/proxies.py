"""Proxy objects that implement existing Protocols via JSON-RPC forwarding."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from courtier.agent.artifacts.models import InputField, RuntimePolicy
from courtier.agent.tools.protocol import OnToolProgress, ToolResult
from courtier.agent.tools.summary import ToolSummary, summarize_result
from courtier.plugin.types import ComplianceResult, Violation


@runtime_checkable
class _JSONRPCClientLike(Protocol):
    """Structural interface for JSONRPCClient — avoids circular imports."""

    plugin_name: str

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> Any: ...


@runtime_checkable
class _ToolRegistryLike(Protocol):
    """Structural interface for ToolRegistry."""

    def register(self, tool: Any, force: bool = False) -> None: ...
    def unregister(self, name: str) -> None: ...
    def list_tools(self) -> list[Any]: ...


logger = logging.getLogger(__name__)


class ProxyTool:
    """Implements ToolProtocol by forwarding execute() over JSON-RPC.

    Provides the same interface (name, description, parameters, execute)
    so it can be registered with ToolRegistry.
    """

    def __init__(self, client: Any, tool_spec: dict[str, Any]) -> None:
        self._client = client
        name = tool_spec.get("name")
        if not name:
            raise ValueError(
                f"ProxyTool requires a 'name' field in tool_spec, got keys: "
                f"{list(tool_spec.keys())}"
            )
        self.name: str = name
        self.display_name: str | None = tool_spec.get("display_name")
        self.description: str = tool_spec.get("description", "")
        self.parameters: dict[str, Any] = tool_spec.get("parameters", {})

        # Contract fields — read from plugin spec if present (#2)
        self.output_artifact_type: str | None = tool_spec.get("output_artifact_type")
        self.input_fields: tuple = (
            tuple(InputField(**f) for f in tool_spec.get("input_fields", []))
            if "input_fields" in tool_spec
            else ()
        )
        self.output_schema: dict | None = tool_spec.get("output_schema")
        self.skip_persist: bool = tool_spec.get("skip_persist", False)
        self.skip_ref_resolution: bool = tool_spec.get("skip_ref_resolution", False)

        rp = tool_spec.get("runtime_policy")
        self.runtime_policy: RuntimePolicy | None = RuntimePolicy(**rp) if rp else None

    # Host-side objects injected by ToolRegistry.execute — must NOT be
    # forwarded to plugin subprocesses (they are not JSON-serializable).
    _HOST_KWARGS = frozenset({"context_manager", "artifact_store", "audit_logger"})

    async def execute(
        self, *, on_progress: OnToolProgress, **kwargs: Any
    ) -> ToolResult:
        """Forward the execute call to the plugin subprocess via JSON-RPC."""
        args = {k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS}
        on_progress(
            {
                "status": "running",
                "message": f"调用插件工具 {self.name}...",
                "detail": None,
            }
        )
        resp = await self._client.call(
            "tool.execute",
            {
                "tool": self.name,
                "args": args,
            },
        )
        on_progress(
            {
                "status": "done",
                "message": f"插件工具 {self.name} 返回结果",
                "detail": None,
            }
        )

        # Validate plugin response structure.  A malformed response (missing
        # success field, non-dict) is treated as a failure with a clear error
        # message rather than silently defaulting to success=False with no info.
        if not isinstance(resp, dict):
            return ToolResult(
                success=False,
                error=(
                    f"插件 {self._client.plugin_name} 工具 {self.name} "
                    f"返回了非预期的响应类型: {type(resp).__name__}"
                ),
            )
        if "success" not in resp:
            return ToolResult(
                success=False,
                error=(
                    f"插件 {self._client.plugin_name} 工具 {self.name} "
                    f"响应缺少 'success' 字段"
                ),
                metadata=resp,
            )
        return ToolResult(
            success=resp.get("success", False),
            data=resp.get("data"),
            error=resp.get("error"),
            metadata=resp.get("metadata", {}),
        )

    def summarize(self, result: ToolResult) -> ToolSummary:
        return summarize_result(result)


class ProxyChecker:
    """Implements ContentChecker Protocol by forwarding check() over JSON-RPC."""

    def __init__(
        self, client: _JSONRPCClientLike, checker_spec: dict[str, Any]
    ) -> None:
        self._client = client
        self._checker_name = checker_spec["name"]
        self.doc_type: str = checker_spec["doc_type"]

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        """Forward the check call to the plugin subprocess via JSON-RPC."""
        resp = await self._client.call(
            "checker.check",
            {
                "name": self._checker_name,
                "text": text,
                "subtype": subtype,
            },
        )
        if not isinstance(resp, dict):
            logger.error(
                "Checker '%s' from plugin '%s' returned non-dict response: %s",
                self._checker_name,
                self._client.plugin_name,
                type(resp).__name__,
            )
            return ComplianceResult(
                is_valid=False,
                violations=(
                    Violation(
                        rule_id="malformed_checker_response",
                        message=(
                            f"Checker returned {type(resp).__name__} "
                            f"instead of dict"
                        ),
                        severity="error",
                    ),
                ),
            )
        return ComplianceResult(
            is_valid=resp.get("is_valid", False),
            violations=tuple(
                Violation(
                    rule_id=v["rule_id"],
                    message=v["message"],
                    severity=v.get("severity", "error"),
                    position=v.get("position"),
                )
                for v in resp.get("violations", [])
            ),
        )


class ProxyRoute:
    """Wraps a FastAPI route handler forwarded to a plugin subprocess.

    This is a placeholder that captures route metadata. The actual HTTP
    proxying is handled by the host's FastAPI router at mount time.
    """

    def __init__(self, route_spec: dict[str, Any]) -> None:
        self.prefix: str = route_spec["prefix"]
        self.description: str = route_spec.get("description", "")
