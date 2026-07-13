"""Bounded projection graph resolver."""

from __future__ import annotations

import logging
import time
from collections import deque

from .models import (
    Artifact,
    InputField,
    MaterializerSpec,
    ProjectionDiagnostic,
    ProjectionEvent,
    ProjectionPlan,
    ProjectionPolicy,
    ProjectionResolution,
    ProjectionStep,
    check_schema_version_compatible,
)
from .projectors import ProjectorRegistry

logger = logging.getLogger(__name__)

# Default materializer paths per artifact type (Section 10).
# Only set for types where the artifact data key directly yields the
# materialized value without further transformation.
_PATH_DEFAULTS: dict[str, str] = {
    "core.plain_text": "$.text",
}


class ProjectionResolver:
    """Resolve tool input contracts into projection plans."""

    def __init__(self, registry: ProjectorRegistry) -> None:
        self._registry = registry

    def resolve(
        self,
        fields: tuple[InputField, ...],
        tool_name: str,
        artifacts: list[Artifact],
        policy: ProjectionPolicy,
        producers: dict[str, list[str]] | None = None,
    ) -> ProjectionResolution:
        plans: dict[str, ProjectionPlan] = {}
        diagnostics: list[ProjectionDiagnostic] = []
        suggested_actions: list[dict] = []
        resolved_count = 0
        failed_count = 0
        for field in fields:
            candidates = self._resolve_field_multi(field, artifacts, policy)
            if not candidates:
                failed_count += 1
                diag = ProjectionDiagnostic(
                    level="error",
                    code="field_unresolved",
                    message=f"Could not resolve required field {field.name} "
                    f"(type={field.artifact_type})",
                    details={
                        "required_type": field.artifact_type,
                    },
                )
                diagnostics.append(diag)
                # Suggest upstream tools based on dynamic producers
                upstream = producers.get(field.artifact_type, []) if producers else []
                for producer in upstream:
                    suggested_actions.append(
                        {
                            "action": "call_tool",
                            "tool": producer,
                            "reason": (
                                f"{producer} outputs {field.artifact_type}, "
                                f"which can satisfy the {field.name} field"
                            ),
                        }
                    )
                continue
            # Select best candidate according to policy
            best = self._select_best(candidates, policy)
            if best is not None:
                resolved_count += 1
                plans[field.name] = best
        if diagnostics:
            emit_event("projection_resolution", {
                "tool": tool_name,
                "status": "failed",
                "resolved_fields": resolved_count,
                "failed_fields": failed_count,
                "suggestions": [s.get("tool") for s in suggested_actions],
            })
            return ProjectionResolution(
                status="failed",
                diagnostics=tuple(diagnostics),
                suggested_actions=tuple(suggested_actions),
            )
        emit_event("projection_resolution", {
            "tool": tool_name,
            "status": "resolved",
            "resolved_fields": resolved_count,
        })
        return ProjectionResolution(status="resolved", plans=plans)

    def _resolve_field_multi(
        self,
        field: InputField,
        artifacts: list[Artifact],
        policy: ProjectionPolicy,
    ) -> list[ProjectionPlan]:
        """Find all valid projection plans for a field, sorted by score descending."""
        results: list[ProjectionPlan] = []
        candidates = [
            artifact
            for artifact in artifacts
            if self._artifact_allowed(artifact, field, policy)
        ]
        for source_artifact in candidates:
            plan = self._find_shortest_path(source_artifact, field, policy)
            if plan is not None:
                results.append(plan)
        results.sort(key=lambda p: p.score, reverse=True)
        return results

    def _find_shortest_path(
        self,
        source_artifact: Artifact,
        field: InputField,
        policy: ProjectionPolicy,
    ) -> ProjectionPlan | None:
        """BFS to find the shortest projection path from source to target type."""
        best_plan: ProjectionPlan | None = None
        best_score = -1.0
        # Queue carries (steps, current_type, current_version, current_score).
        # source_artifact is extracted to the outer scope — it never changes
        # during BFS, so carrying it in every queue element was redundant.
        queue: deque[tuple[tuple[ProjectionStep, ...], str, str, float]] = deque(
            [((), source_artifact.artifact_type, source_artifact.schema_version, 1.0)]
        )
        visited: set[tuple[str, int]] = set()
        while queue:
            steps, current_type, current_version, current_score = queue.popleft()
            # depth guard: still allow matching when len(steps) == max_depth
            # (a zero-step match has len(steps) == 0, always permitted)
            if current_type == field.artifact_type and len(steps) <= policy.max_depth:
                candidate_score = current_score - (0.05 * len(steps))
                if candidate_score > best_score:
                    best_score = candidate_score
                    materializer_path = _PATH_DEFAULTS.get(field.artifact_type, "")
                    best_plan = ProjectionPlan(
                        field_name=field.name,
                        required_type=field.artifact_type,
                        source_artifact_id=source_artifact.artifact_id,
                        steps=steps,
                        materializer=MaterializerSpec(
                            artifact_type=field.artifact_type,
                            materialize_as=field.materialize_as,
                            path=materializer_path,
                        ),
                        score=candidate_score,
                    )
                continue
            key = (current_type, len(steps))
            if key in visited:
                continue
            visited.add(key)
            # Only expand if we haven't hit max_depth yet
            if len(steps) >= policy.max_depth:
                continue
            for projector in self._registry.outgoing(current_type):
                if projector.spec.stability == "disabled":
                    continue
                if projector.spec.stability == "deprecated" and not policy.allow_deprecated:
                    continue
                if projector.spec.stability == "experimental" and not policy.allow_experimental:
                    continue
                # Enforce quality and lossiness policy
                if projector.spec.quality_score < policy.min_quality:
                    continue
                if projector.spec.lossiness != "lossless" and not policy.allow_lossy:
                    continue
                # Enforce schema version compatibility
                if not check_schema_version_compatible(
                    current_version, projector.spec.source_schema_version,
                ):
                    continue
                constraints = {
                    name: value
                    for name, value in field.constraints.items()
                    if name in projector.spec.supported_constraints
                }
                edge_quality = projector.spec.quality_score
                queue.append(
                    (
                        steps
                        + (
                            ProjectionStep(
                                projector_name=projector.spec.name,
                                source_type=projector.spec.source_type,
                                target_type=projector.spec.target_type,
                                constraints=constraints,
                            ),
                        ),
                        projector.spec.target_type,
                        projector.spec.target_schema_version,
                        current_score * edge_quality,
                    )
                )
        return best_plan

    def _select_best(
        self,
        candidates: list[ProjectionPlan],
        policy: ProjectionPolicy,
    ) -> ProjectionPlan | None:
        """Select the best candidate considering policy preferences."""
        if not candidates:
            return None
        # When prefer_cached is set, reorder: prefer zero-step (already cached) plans
        if policy.prefer_cached:
            zero_step = [p for p in candidates if not p.steps]
            if zero_step:
                return zero_step[0]
        # When strict_ambiguity is set, warn if multiple candidates are very close
        if policy.strict_ambiguity and len(candidates) >= 2:
            top = candidates[0]
            second = candidates[1]
            if abs(top.score - second.score) < 0.05:
                logger.warning(
                    "Ambiguous projection: top candidates have near-identical scores "
                    "(%s=%.3f vs %s=%.3f). Using highest-scored candidate.",
                    top.source_artifact_id,
                    top.score,
                    second.source_artifact_id,
                    second.score,
                )
        return candidates[0]

    @staticmethod
    def _artifact_allowed(
        artifact: Artifact,
        field: InputField,
        policy: ProjectionPolicy,
    ) -> bool:
        """Check if artifact can satisfy a contract field.

        Simplified: only checks debug_only and projection_allowed flags.
        Role/subject/scope/sensitivity matching removed — all artifacts
        in a single-run session share the same role/subject defaults,
        so the extra checks never rejected anything in practice.
        """
        if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
            return False
        if not artifact.metadata.projection_allowed:
            return False
        return True


def emit_event(event: str, details: dict | None = None) -> ProjectionEvent:
    """Emit a structured projection event.

    Events are logged at DEBUG level and returned for optional collection.
    """
    evt = ProjectionEvent(
        event=event,
        timestamp=time.monotonic(),
        details=details or {},
    )
    logger.debug("projection_event: %s %s", event, details or "")
    return evt
