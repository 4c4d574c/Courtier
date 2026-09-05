"""Projector registry (core edges; domain edges come from profiles)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from .models import (
    Artifact,
    ArtifactMetadata,
    ProjectionDiagnostic,
    ProjectionQuality,
    ProjectorLayer,
    ProjectorSpec,
    stable_content_hash,
    validate_artifact_data,
)

logger = logging.getLogger(__name__)

ProjectorFn = Callable[
    [Artifact, dict[str, Any]],
    tuple[Any, ProjectionQuality, tuple[ProjectionDiagnostic, ...]],
]


class Projector:
    """A deterministic ArtifactType-to-ArtifactType transformation."""

    def __init__(self, spec: ProjectorSpec, fn: ProjectorFn) -> None:
        self.spec = spec
        self._fn = fn

    def project(
        self,
        artifact: Artifact,
        constraints: dict[str, Any],
        *,
        skip_schema_validation: bool = False,
    ) -> "ProjectionResult":
        if artifact.artifact_type != self.spec.source_type:
            raise ValueError(
                f"Projector {self.spec.name} expected {self.spec.source_type}, "
                f"got {artifact.artifact_type}"
            )
        unsupported = set(constraints) - set(self.spec.supported_constraints)
        if unsupported:
            raise ValueError(
                f"Projector {self.spec.name} does not support constraints: "
                f"{sorted(unsupported)}"
            )
        data, quality, diagnostics = self._fn(artifact, constraints)
        # Validate output data against target artifact type schema
        if not skip_schema_validation:
            schema_errors = validate_artifact_data(self.spec.target_type, data)
            if schema_errors:
                diagnostics = diagnostics + tuple(
                    ProjectionDiagnostic(
                        level="error",
                        code="schema_validation_failed",
                        message=err,
                    )
                    for err in schema_errors
                )
                raise ValueError(
                    f"Projector {self.spec.name} produced invalid data for "
                    f"{self.spec.target_type}: {'; '.join(schema_errors)}"
                )
        metadata = ArtifactMetadata(
            created_by=self.spec.name,
            source_refs=(artifact.artifact_id,),
            content_hash=stable_content_hash(data),
            semantic_role=artifact.metadata.semantic_role,
            subject=artifact.metadata.subject,
            quality=quality.model_dump(),
            lineage=artifact.metadata.lineage + (self.spec.name,),
        )
        projected = Artifact(
            artifact_id=f"projected:{self.spec.target_type}:{metadata.content_hash[-12:]}",
            artifact_type=self.spec.target_type,
            schema_version="1.0",
            data=data,
            metadata=metadata,
        )
        return ProjectionResult(artifact=projected, quality=quality, diagnostics=diagnostics)


class ProjectionResult:
    """Result of one projector execution."""

    def __init__(
        self,
        *,
        artifact: Artifact,
        quality: ProjectionQuality,
        diagnostics: tuple[ProjectionDiagnostic, ...] = (),
    ) -> None:
        self.artifact = artifact
        self.quality = quality
        self.diagnostics = diagnostics


class ProjectorRegistry:
    """Registry of known projector edges.

    Maintains five indexes for efficient lookup:
    - by_name: name -> Projector
    - by_source: source_type -> list[Projector] (outgoing edges)
    - by_target: target_type -> list[Projector] (incoming edges)
    - by_edge: (source_type, target_type) -> list[Projector]
    - by_layer: layer -> list[Projector]
    """

    # Allowed layer promotion order (left-to-right).  Skipping a layer is OK
    # (e.g. local -> core) but reversing (core -> local) is forbidden.
    _LAYER_ORDER: tuple[ProjectorLayer, ...] = ("local", "plugin", "domain", "core")

    def __init__(self) -> None:
        self._by_name: dict[str, Projector] = {}
        self._by_source: dict[str, list[Projector]] = {}
        self._by_target: dict[str, list[Projector]] = {}
        self._by_edge: dict[tuple[str, str], list[Projector]] = {}
        self._by_layer: dict[str, list[Projector]] = {}

    def register(self, projector: Projector) -> None:
        if projector.spec.name in self._by_name:
            raise ValueError(f"Duplicate projector: {projector.spec.name}")
        self._by_name[projector.spec.name] = projector
        self._by_source.setdefault(projector.spec.source_type, []).append(projector)
        self._by_target.setdefault(projector.spec.target_type, []).append(projector)
        edge = (projector.spec.source_type, projector.spec.target_type)
        self._by_edge.setdefault(edge, []).append(projector)
        self._by_layer.setdefault(projector.spec.layer, []).append(projector)

    def promote(self, name: str, new_layer: ProjectorLayer) -> Projector:
        """Promote a projector to a higher layer.

        Raises ValueError if the promotion reverts to a lower layer or if
        the projector is experimental/deprecated.
        """
        projector = self._by_name.get(name)
        if projector is None:
            raise ValueError(f"Unknown projector: {name}")
        spec = projector.spec
        if spec.stability not in ("stable",):
            raise ValueError(
                f"Cannot promote projector {name}: stability is {spec.stability}, "
                f"only stable projectors can be promoted."
            )
        old_idx = self._LAYER_ORDER.index(spec.layer)
        new_idx = self._LAYER_ORDER.index(new_layer)
        if new_idx <= old_idx:
            raise ValueError(
                f"Cannot promote {name} from {spec.layer} to {new_layer}: "
                f"promotion must move to a higher layer (left-to-right in "
                f"{list(self._LAYER_ORDER)})."
            )
        # Create a new ProjectorSpec with the updated layer
        old_layer = spec.layer
        new_spec = spec.model_copy(update={"layer": new_layer})
        new_projector = Projector(new_spec, projector._fn)
        # Replace in name index
        self._by_name[name] = new_projector

        def _replace_in_index(index: dict, key: Any) -> None:
            index[key] = [new_projector if p.spec.name == name else p for p in index.get(key, [])]

        _replace_in_index(self._by_source, spec.source_type)
        _replace_in_index(self._by_target, spec.target_type)
        _replace_in_index(self._by_edge, (spec.source_type, spec.target_type))
        # Layer: remove from old layer, add to new layer
        self._by_layer[old_layer] = [
            p for p in self._by_layer.get(old_layer, []) if p.spec.name != name
        ]
        self._by_layer.setdefault(new_layer, []).append(new_projector)
        return new_projector

    def get(self, name: str) -> Projector:
        return self._by_name[name]

    def outgoing(self, artifact_type: str) -> list[Projector]:
        return list(self._by_source.get(artifact_type, []))

    def by_target(self, artifact_type: str) -> list[Projector]:
        """Return all projectors that produce *artifact_type*."""
        return list(self._by_target.get(artifact_type, []))

    def by_edge(self, source_type: str, target_type: str) -> list[Projector]:
        """Return all projectors for a specific source→target edge."""
        return list(self._by_edge.get((source_type, target_type), []))

    def by_layer(self, layer: str) -> list[Projector]:
        """Return all projectors registered at *layer*."""
        return list(self._by_layer.get(layer, []))

    def all(self) -> list[Projector]:
        return list(self._by_name.values())


_default_projector_registry: ProjectorRegistry | None = None
#: Named rebuild hooks - domain profiles re-apply their registrations
#: whenever the default registry is rebuilt (see DomainActivator).
_rebuild_hooks: dict[str, Any] = {}


def register_rebuild_hook(name: str, fn: Any) -> None:
    """Register a named hook applied on every default-registry build.

    If the default instance already exists, *fn* runs immediately.
    """
    _rebuild_hooks[name] = fn
    if _default_projector_registry is not None:
        fn(_default_projector_registry)


def create_default_projector_registry() -> ProjectorRegistry:
    """Return the global default ProjectorRegistry, building it once.

    The returned instance is a module-level singleton — callers that register
    custom projectors on it will affect every other caller, which is the
    intended behaviour for application-wide type-conversion edges.
    """
    global _default_projector_registry
    if _default_projector_registry is None:
        _default_projector_registry = _build_default_projector_registry()
    return _default_projector_registry


def reset_default_projector_registry() -> None:
    """Reset the singleton so the next call to create_default_projector_registry()
    builds a fresh instance. Only intended for test teardown."""
    global _default_projector_registry
    _default_projector_registry = None


def _document_markdown_to_plain_text(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    """Project a convert_document Markdown artifact into core.plain_text.

    Content skills (content audit, text correction, plagiarism, secret
    analysis) consume plain text; the Markdown is used verbatim — no
    markdown-source stripping, so heading markers etc. stay visible to the
    consumer.
    """
    markdown = artifact.data.get("markdown", "") or ""
    return (
        {"text": markdown, "language": "zh", "source_scope": "full_document"},
        ProjectionQuality(
            confidence=1.0 if markdown else 0.0,
            lossiness="lossy",
            stats={"chars": len(markdown)},
        ),
        (),
    )



def _build_default_projector_registry() -> ProjectorRegistry:
    registry = ProjectorRegistry()
    registry.register(
        Projector(
            ProjectorSpec(
                name="core.document_markdown.to_plain_text",
                source_type="core.document_markdown",
                target_type="core.plain_text",
                owner="document",
                quality_score=0.98,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=(),
                description="Use the Markdown rendering verbatim as plain text.",
            ),
            _document_markdown_to_plain_text,
        )
    )
    for hook in _rebuild_hooks.values():
        hook(registry)
    return registry


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in paragraph.get("elements", []) or []:
        font = element.get("font", {}) if isinstance(element, dict) else {}
        text = font.get("text", "")
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


def _append_paragraph(
    paragraphs: list[dict[str, Any]],
    paragraph: dict[str, Any] | None,
    *,
    section: str,
    source_path: str,
) -> None:
    if not isinstance(paragraph, dict):
        return
    text = _paragraph_text(paragraph)
    if not text:
        return
    paragraphs.append(
        {
            "index": len(paragraphs),
            "text": text,
            "section": section,
            "source_path": source_path,
            "style": {
                "outline_level": paragraph.get("outline_level"),
                "alignment": paragraph.get("alignment"),
            },
        }
    )


def _iter_body_paragraphs(data: dict) -> Iterator[tuple[str, dict[str, Any], str]]:
    """遍历 parsed_layout 正文段落：yield (section, paragraph, source_path)。

    parsed_layout → paragraph_list 投影与 parsed_layout_to_text 共用此
    提取路径，避免两处对 pages[].page_content.body.{title,main_text} 结构的
    理解漂移。
    """
    for page_index, page in enumerate(data.get("pages", []) or []):
        body = (page.get("page_content") or {}).get("body") or {}
        title = body.get("title")
        if isinstance(title, dict):
            yield (
                "title",
                title,
                f".pages[{page_index}].page_content.body.title",
            )
        for paragraph_index, paragraph in enumerate(body.get("main_text", []) or []):
            if isinstance(paragraph, dict):
                yield (
                    "body",
                    paragraph,
                    f".pages[{page_index}].page_content.body.main_text[{paragraph_index}]",
                )


def parsed_layout_to_text(data: dict, source_scope: str | None = None) -> str:
    """parsed_layout 数据 → 按阅读序拼接的正文文本。

    与 _parsed_layout_to_paragraph_list 共用 _iter_body_paragraphs 提取路径；
    source_scope="body" 时仅取正文段落，缺省取 title + body。
    """
    lines: list[str] = []
    for section, paragraph, _ in _iter_body_paragraphs(data):
        if source_scope == "body" and section != "body":
            continue
        text = _paragraph_text(paragraph)
        if text:
            lines.append(text)
    return "\n".join(lines)

def audit_projector_graph(registry: ProjectorRegistry) -> dict[str, Any]:
    """Audit the projector graph for common issues.

    Returns a report dict with these keys:
    - ``unproduced_types``: artifact types that have consumers but no producers
    - ``unconsumed_types``: artifact types that have producers but no consumers
    - ``unused_projectors``: projectors with deprecated/disabled stability
    - ``duplicate_edges``: source-target pairs with multiple indistinguishable projectors
    - ``local_in_production``: projectors in the local layer
    - ``deep_paths``: paths where source_type and target_type are very far apart
      (>3 hops via different types)
    - ``missing_contracts``: hint that this is checked separately via ToolInputContract
    - ``debug_artifacts_in_business_paths``: any projector touching debug_view or
      with debug_only metadata
    """
    produced: set[str] = set()
    consumed: set[str] = set()
    deprecated_projectors: list[str] = []
    local_projectors: list[str] = []
    edge_counts: dict[tuple[str, str], list[str]] = {}
    debug_touching: list[str] = []

    for proj in registry.all():
        spec = proj.spec
        produced.add(spec.target_type)
        consumed.add(spec.source_type)

        if spec.stability in ("deprecated", "disabled"):
            deprecated_projectors.append(spec.name)
        if spec.layer == "local":
            local_projectors.append(spec.name)

        edge = (spec.source_type, spec.target_type)
        edge_counts.setdefault(edge, []).append(spec.name)

        if spec.source_type == "core.debug_view" or spec.target_type == "core.debug_view":
            debug_touching.append(spec.name)

    duplicate_edges = {
        f"{src} -> {tgt}": names for (src, tgt), names in edge_counts.items() if len(names) > 1
    }

    all_types = produced | consumed

    return {
        "unproduced_types": sorted(consumed - produced),
        "unconsumed_types": sorted(produced - consumed),
        "unused_projectors": deprecated_projectors,
        "duplicate_edges": duplicate_edges,
        "local_in_production": local_projectors,
        "deep_paths_hint": (
            "Run a BFS from each produced type to each consumed type to detect "
            "paths exceeding max_depth=3. Use ProjectionResolver for this check."
        ),
        "missing_contracts_hint": (
            "Tools missing contracts are not tracked here; check ToolRegistry."
        ),
        "debug_artifacts_in_business_paths": debug_touching,
        "total_projectors": len(registry.all()),
        "total_types": len(all_types),
    }
