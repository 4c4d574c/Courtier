"""Proxy objects that implement existing Protocols via JSON-RPC forwarding."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from courtier_plugin_sdk.types import ComplianceResult, Violation

from courtier.agent.artifacts.models import InputField, RuntimePolicy
from courtier.agent.tools.protocol import OnToolProgress, ToolResult
from courtier.agent.tools.summary import ToolSummary, summarize_result
from courtier.prompts.errors import render_error


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

# A string consisting purely of base64 alphabet characters is inline data,
# not a path (annotate's `source` is dual-mode).  Paths always contain at
# least one character outside this alphabet ("/", ".", …).
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


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
        # Host-only service tools (internal) stay in the base registry for
        # API-layer callers but never reach agent sessions.
        self.internal: bool = bool(tool_spec.get("internal", False))

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

    def _file_ref_params(self) -> tuple[str, ...]:
        """Input properties the plugin marked as file references (file-ref)."""
        properties = (self.parameters or {}).get("properties") or {}
        return tuple(
            name
            for name, spec in properties.items()
            if isinstance(spec, dict) and spec.get("format") == "file-ref"
        )

    async def _rewrite_file_args(self, args: dict[str, Any]) -> str | None:
        """Rewrite file-ref arguments from upload-dir paths to minio:// refs.

        Returns an error message on sandbox violation / transfer failure
        (fail-closed), None on success.  The upload-dir containment check is
        the path sandbox that used to live inside the plugins — with
        standalone plugins it must happen here, before anything leaves the
        host.
        """
        params = self._file_ref_params()
        if not params:
            return None
        from courtier.config import get_settings
        from courtier.storage import client as storage_client

        settings = get_settings()
        upload_root = Path(settings.upload_dir).resolve()
        bucket = settings.minio_bucket_plugin_io

        for name in params:
            value = args.get(name)
            if not isinstance(value, str) or not value or value.startswith("minio://"):
                continue
            if _BASE64_RE.fullmatch(value):
                continue  # inline bytes, not a path
            p = Path(value)
            resolved = p.resolve() if p.is_absolute() else (upload_root / p).resolve()
            if not resolved.is_relative_to(upload_root):
                return render_error("errors.plugin_arg_path_escape", param_name=name, value=value)
            if not resolved.is_file():
                return render_error("errors.plugin_arg_file_missing", param_name=name, value=value)

            rel = resolved.relative_to(upload_root)
            stat = resolved.stat()
            # Content-addressed key: identical file content/state never
            # re-uploads; overwriting the file changes the digest.
            digest = hashlib.sha256(
                f"{rel}|{stat.st_size}|{stat.st_mtime_ns}".encode()
            ).hexdigest()[:32]
            key = f"in/{digest}/{resolved.name}"
            try:
                exists = await asyncio.to_thread(storage_client.object_exists, bucket, key)
                if not exists:
                    ctype = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
                    await asyncio.to_thread(
                        storage_client.fput_object, bucket, key, str(resolved), ctype
                    )
            except Exception as exc:
                logger.warning("file-ref transfer to MinIO failed: %s", key, exc_info=True)
                return render_error("errors.plugin_file_transfer_failed", error=str(exc))

            ref = f"minio://{bucket}/{key}"
            args[name] = ref
            logger.info("Rewrote file arg %s=%s -> %s", name, value, ref)
        return None

    async def execute(self, *, on_progress: OnToolProgress, **kwargs: Any) -> ToolResult:
        """Forward the execute call to the plugin subprocess via JSON-RPC."""
        args = {k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS}
        rewrite_error = await self._rewrite_file_args(args)
        if rewrite_error is not None:
            return ToolResult(success=False, error=rewrite_error)
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
                error=render_error(
                    "errors.plugin_bad_response_type",
                    plugin_name=self._client.plugin_name,
                    tool_name=self.name,
                    response_type=type(resp).__name__,
                ),
            )
        if "success" not in resp:
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.plugin_missing_success_field",
                    plugin_name=self._client.plugin_name,
                    tool_name=self.name,
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

    def __init__(self, client: _JSONRPCClientLike, checker_spec: dict[str, Any]) -> None:
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
                        message=(f"Checker returned {type(resp).__name__} " f"instead of dict"),
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
