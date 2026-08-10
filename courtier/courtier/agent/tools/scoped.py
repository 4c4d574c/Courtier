"""ScopedTool — wrap a tool with host-injected kwargs invisible to the model.

Used to enforce server-side boundaries (e.g. resource-library visibility):
the host bakes the caller's owner scope into the tool call, and the injected
kwargs always win over anything the model supplies.
"""

from __future__ import annotations

from typing import Any


class ScopedTool:
    """Proxy wrapper that merges fixed *injections* into every execute call."""

    def __init__(self, inner: Any, injections: dict[str, Any]) -> None:
        self._inner = inner
        self._injections = dict(injections)
        # Mirror the public tool surface used by ToolRegistry / SSE metadata.
        self.name: str = inner.name
        self.display_name = getattr(inner, "display_name", None)
        self.description: str = getattr(inner, "description", "")
        self.parameters: dict[str, Any] = getattr(inner, "parameters", {})
        for attr in (
            "skill",
            "output_schema",
            "output_artifact_type",
            "input_fields",
            "skip_persist",
            "skip_ref_resolution",
            "skip_summarize",
            "skip_artifact_registration",
            "output_content_type",
            "input_contract",
            "output_contract",
            "runtime_policy",
        ):
            setattr(self, attr, getattr(inner, attr, None))

    async def execute(self, **kwargs: Any) -> Any:
        # Injected scope wins over model-supplied arguments.
        merged = {**kwargs, **self._injections}
        if hasattr(self._inner, "execute"):
            return await self._inner.execute(**merged)
        raise TypeError(f"ScopedTool inner tool {self.name!r} has no execute()")
