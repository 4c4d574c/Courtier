"""Projection execution and materialization."""

from __future__ import annotations

import logging
from typing import Any

from .models import (
    Artifact,
    InputField,
    MaterializedBinding,
    MaterializerSpec,
    ProjectionFeatureFlags,
    ProjectionPlan,
    ProjectionTrace,
    ProjectionTraceStep,
)
from .projectors import ProjectorRegistry
from .resolver import emit_event
from .store import ArtifactStore

logger = logging.getLogger(__name__)


class MaterializerRegistry:
    """Registry for converting artifacts into concrete tool arguments."""

    _default_instance: "MaterializerRegistry | None" = None

    def __init__(self) -> None:
        self._materializers: dict[tuple[str, str], Any] = {}

    @classmethod
    def default(cls) -> "MaterializerRegistry":
        """Return the global default MaterializerRegistry, building it once.

        The returned instance is a module-level singleton — callers that
        register custom materializers on it will affect every other caller.
        """
        if cls._default_instance is None:
            cls._default_instance = cls._build_default()
        return cls._default_instance

    @classmethod
    def reset_default(cls) -> None:
        """Reset the singleton so the next call to default() builds a fresh
        instance. Only intended for test teardown."""
        cls._default_instance = None

    @classmethod
    def _build_default(cls) -> "MaterializerRegistry":
        registry = cls()
        registry.register(
            "core.plain_text", "string", lambda artifact: artifact.data.get("text", "")
        )
        registry.register(
            "core.text_collection",
            "list_string",
            lambda artifact: [item.get("text", "") for item in artifact.data.get("items", [])],
        )
        registry.register("docaudit.parsed_document", "dict", lambda artifact: artifact.data)
        registry.register(
            "docaudit.paragraph_list",
            "list_string",
            lambda artifact: [p.get("text", "") for p in artifact.data.get("paragraphs", [])],
        )
        registry.register("docaudit.paragraph_list", "dict", lambda artifact: artifact.data)
        registry.register(
            "docaudit.paragraph_list",
            "list_dict",
            lambda artifact: artifact.data.get("paragraphs", []),
        )
        return registry

    def register(self, artifact_type: str, materialize_as: str, fn: Any) -> None:
        self._materializers[(artifact_type, materialize_as)] = fn

    @property
    def materializable_types(self) -> set[str]:
        """Return the set of artifact types that have at least one materializer."""
        return {t for (t, _) in self._materializers}

    def materialize(self, artifact: Artifact, spec: MaterializerSpec) -> Any:
        key = (spec.artifact_type, spec.materialize_as)
        if artifact.artifact_type != spec.artifact_type:
            raise ValueError(
                f"Materializer expected {spec.artifact_type}, got {artifact.artifact_type}"
            )
        # If path is specified, extract value directly from artifact data
        if spec.path and isinstance(artifact.data, dict):
            data_key = spec.path.lstrip("$.")
            if data_key in artifact.data:
                return artifact.data[data_key]
        # Fall through to registered materializer function
        if key not in self._materializers:
            raise KeyError(f"No materializer registered for {key}")
        return self._materializers[key](artifact)


def validate_materialized_value(
    value: Any,
    field: InputField,
) -> str | None:
    """Validate a materialized value against field constraints.

    Returns an error message string if validation fails, or None if it passes.
    """
    constraints = field.constraints
    if not constraints:
        return None
    # min_chars / max_chars for string values
    if isinstance(value, str):
        min_chars = constraints.get("min_chars")
        if isinstance(min_chars, int) and len(value) < min_chars:
            return (
                f"Field {field.name}: text length {len(value)} is below "
                f"minimum {min_chars} characters"
            )
        max_chars = constraints.get("max_chars")
        if isinstance(max_chars, int) and len(value) > max_chars:
            return (
                f"Field {field.name}: text length {len(value)} exceeds "
                f"maximum {max_chars} characters"
            )
    # min_items / max_items for list values
    if isinstance(value, list):
        min_items = constraints.get("min_items")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"Field {field.name}: {len(value)} items is below " f"minimum {min_items}"
        max_items = constraints.get("max_items")
        if isinstance(max_items, int) and len(value) > max_items:
            return f"Field {field.name}: {len(value)} items exceeds " f"maximum {max_items}"
        # item_min_chars for list items
        item_min_chars = constraints.get("item_min_chars")
        if isinstance(item_min_chars, int):
            for i, item in enumerate(value):
                if isinstance(item, str) and len(item) < item_min_chars:
                    return (
                        f"Field {field.name}: item {i} has {len(item)} chars, "
                        f"below minimum {item_min_chars}"
                    )
    return None


class ProjectionExecutor:
    """Execute projection plans using registered projectors, with step-level caching."""

    def __init__(
        self,
        *,
        projector_registry: ProjectorRegistry,
        materializer_registry: MaterializerRegistry,
        artifact_store: ArtifactStore,
        features: ProjectionFeatureFlags | None = None,
    ) -> None:
        self._projectors = projector_registry
        self._materializers = materializer_registry
        self._store = artifact_store
        self._step_cache: dict[str, Artifact] = {}
        self._features = features or ProjectionFeatureFlags()

    def _step_cache_key(
        self,
        source_artifact: Artifact,
        projector_name: str,
        constraints: dict[str, Any],
    ) -> str:
        """Build a stable cache key for a projection step.

        Key includes: source artifact id, content hash, projector name/version,
        target type, and normalized constraints.
        """
        projector = self._projectors.get(projector_name)
        parts = [
            source_artifact.artifact_id,
            source_artifact.metadata.content_hash or "no-hash",
            projector_name,
            projector.spec.version,
            projector.spec.target_type,
            str(sorted(constraints.items())),
        ]
        return "|".join(parts)

    def execute(self, plan: ProjectionPlan) -> MaterializedBinding:
        current = self._store.require(plan.source_artifact_id)
        trace_steps: list[ProjectionTraceStep] = []
        emit_event(
            "projection_execution_started",
            {
                "field": plan.field_name,
                "source": plan.source_artifact_id,
                "steps": len(plan.steps),
            },
        )
        for step in plan.steps:
            cache_key = self._step_cache_key(current, step.projector_name, step.constraints)
            if cache_key in self._step_cache:
                current = self._step_cache[cache_key]
                emit_event(
                    "projection_step_cache_hit",
                    {
                        "projector": step.projector_name,
                        "cache_key": cache_key[:80],
                    },
                )
                trace_steps.append(
                    ProjectionTraceStep(
                        projector=step.projector_name
                        + "@"
                        + self._projectors.get(step.projector_name).spec.version,
                        cache="hit",
                        output_ref=current.artifact_id,
                    )
                )
                continue
            projector = self._projectors.get(step.projector_name)
            emit_event(
                "projection_step_started",
                {
                    "projector": step.projector_name,
                    "source_type": step.source_type,
                    "target_type": step.target_type,
                },
            )
            try:
                result = projector.project(
                    current,
                    step.constraints,
                    skip_schema_validation=not self._features.projector_schema_validation_enabled,
                )
            except Exception:
                emit_event(
                    "projection_step_failed",
                    {
                        "projector": step.projector_name,
                        "error": "projector raised exception",
                    },
                )
                raise
            current = self._store.put(result.artifact)
            self._step_cache[cache_key] = current
            emit_event(
                "projection_step_completed",
                {
                    "projector": step.projector_name,
                    "output_ref": current.artifact_id,
                    "quality": result.quality.model_dump(),
                },
            )
            trace_steps.append(
                ProjectionTraceStep(
                    projector=step.projector_name + "@" + projector.spec.version,
                    cache="miss",
                    output_ref=current.artifact_id,
                    quality=result.quality.model_dump(),
                )
            )
        value = self._materializers.materialize(current, plan.materializer)
        materializer_desc = f"{plan.materializer.artifact_type}.{plan.materializer.materialize_as}"
        trace = ProjectionTrace(
            field=plan.field_name,
            source_artifact=plan.source_artifact_id,
            steps=tuple(trace_steps),
            materializer={
                "name": materializer_desc,
                "path": plan.materializer.path or "",
                "output_type": plan.materializer.materialize_as,
            },
        )
        binding = MaterializedBinding(
            value=value,
            artifact_id=current.artifact_id,
            materializer=materializer_desc,
            trace=trace,
        )
        emit_event(
            "projection_materialized",
            {
                "field": plan.field_name,
                "artifact_id": binding.artifact_id,
                "materializer": binding.materializer,
            },
        )
        return binding
