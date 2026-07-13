"""ContractBinder — coordinates ToolRegistry and ArtifactStore for input/output contracts."""

from __future__ import annotations

import logging
from typing import Any

from .executor import (
    MaterializerRegistry,
    ProjectionExecutor,
    validate_materialized_value,
)
from .models import ProjectionPolicy, InputField
from .projectors import create_default_projector_registry
from .resolver import ProjectionResolver, emit_event
from .store import ArtifactStore
from ..tools.protocol import ToolResult

logger = logging.getLogger(__name__)


class ContractBinder:
    """Binds tool input contracts to artifacts and registers tool outputs."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        policy: ProjectionPolicy | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._policy = policy or ProjectionPolicy()

    def bind_tool_inputs(
        self,
        fields: tuple[InputField, ...],
        tool_name: str,
        explicit_kwargs: dict[str, Any],
        producers: dict[str, list[str]] | None = None,
    ) -> ToolResult:
        """Auto-bind missing tool input fields from artifacts."""
        missing_fields = tuple(
            f for f in fields
            if f.required and f.name not in explicit_kwargs
        )
        if not missing_fields:
            emit_event("tool_arguments_bound", {
                "tool": tool_name,
                "fields": [],
                "source": "explicit",
            })
            return ToolResult(
                success=True,
                data={
                    "arguments": dict(explicit_kwargs),
                    "artifact_bindings": {},
                },
            )

        resolver = ProjectionResolver(create_default_projector_registry())
        candidates = self._artifact_store.list_projection_candidates()
        resolution = resolver.resolve(
            missing_fields,
            tool_name,
            candidates,
            self._policy,
            producers=producers,
        )
        if resolution.status != "resolved":
            messages = [d.message for d in resolution.diagnostics]
            emit_event("tool_binding_failed", {
                "tool": tool_name,
                "missing_fields": [f.name for f in missing_fields],
                "suggestions": list(resolution.suggested_actions),
            })
            return ToolResult(success=False, error="; ".join(messages))

        executor = ProjectionExecutor(
            projector_registry=create_default_projector_registry(),
            materializer_registry=MaterializerRegistry.default(),
            artifact_store=self._artifact_store,
            features=self._policy.features,
        )
        arguments: dict[str, Any] = dict(explicit_kwargs)
        artifact_bindings: dict[str, str] = {}
        validation_errors: list[str] = []
        for name, plan in resolution.plans.items():
            binding = executor.execute(plan)
            field = next((f for f in fields if f.name == name), None)
            if field is not None:
                err = validate_materialized_value(binding.value, field)
                if err is not None:
                    validation_errors.append(err)
            arguments[name] = binding.value
            artifact_bindings[name] = binding.artifact_id
        if validation_errors:
            emit_event("tool_binding_failed", {
                "tool": tool_name,
                "reason": "constraint_validation",
                "errors": validation_errors,
            })
            return ToolResult(
                success=False,
                error="; ".join(validation_errors),
            )
        emit_event("tool_arguments_bound", {
            "tool": tool_name,
            "fields": list(artifact_bindings.keys()),
            "source": "auto_binding",
        })
        return ToolResult(
            success=True,
            data={"arguments": arguments, "artifact_bindings": artifact_bindings},
        )
