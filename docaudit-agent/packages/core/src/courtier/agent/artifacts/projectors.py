"""Projector registry and initial docaudit projectors."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from .models import (
    Artifact,
    ArtifactMetadata,
    ProjectionDiagnostic,
    ProjectionQuality,
    ProjectorSpec,
    stable_content_hash,
    validate_artifact_data,
)

logger = logging.getLogger(__name__)

ProjectorFn = Callable[[Artifact, dict[str, Any]], tuple[Any, ProjectionQuality, tuple[ProjectionDiagnostic, ...]]]


class Projector:
    """A deterministic ArtifactType-to-ArtifactType transformation."""

    def __init__(self, spec: ProjectorSpec, fn: ProjectorFn) -> None:
        self.spec = spec
        self._fn = fn

    def project(self, artifact: Artifact, constraints: dict[str, Any], *, skip_schema_validation: bool = False) -> "ProjectionResult":
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
            index[key] = [
                new_projector if p.spec.name == name else p
                for p in index.get(key, [])
            ]

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


def _build_default_projector_registry() -> ProjectorRegistry:
    registry = ProjectorRegistry()
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.parsed_document.to_paragraph_list",
                source_type="docaudit.parsed_document",
                target_type="docaudit.paragraph_list",
                owner="document",
                quality_score=0.95,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=("source_scope",),
                description="Extract document paragraphs in reading order.",
            ),
            _parsed_document_to_paragraph_list,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.paragraph_list.to_plain_text",
                source_type="docaudit.paragraph_list",
                target_type="core.plain_text",
                owner="document",
                quality_score=0.98,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=("source_scope", "normalize_whitespace", "max_chars"),
                description="Join paragraph texts into plain text. When max_chars truncation is "
                "active the projector is effectively a summary, not lossy.",
            ),
            _paragraph_list_to_plain_text,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.search_results.to_reference_text_list",
                source_type="docaudit.search_results",
                target_type="docaudit.reference_text_list",
                owner="search",
                quality_score=0.92,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=("max_items", "min_text_chars", "dedupe"),
                description="Extract reference chunks from search results.",
            ),
            _search_results_to_reference_text_list,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.reference_text_list.to_text_collection",
                source_type="docaudit.reference_text_list",
                target_type="core.text_collection",
                owner="search",
                quality_score=0.97,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=("min_items",),
                description="Convert reference texts to generic text collection.",
            ),
            _reference_text_list_to_text_collection,
        )
    )
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


def _parsed_document_to_paragraph_list(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    paragraphs: list[dict[str, Any]] = []
    for page_index, page in enumerate(artifact.data.get("pages", []) or []):
        body = ((page.get("page_content") or {}).get("body") or {})
        _append_paragraph(
            paragraphs,
            body.get("title"),
            section="title",
            source_path=f".pages[{page_index}].page_content.body.title",
        )
        for paragraph_index, paragraph in enumerate(body.get("main_text", []) or []):
            _append_paragraph(
                paragraphs,
                paragraph,
                section="body",
                source_path=(
                    f".pages[{page_index}].page_content.body.main_text[{paragraph_index}]"
                ),
            )
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()
    confidence = 1.0
    if not paragraphs:
        confidence = 0.0
        pages = artifact.data.get("pages", []) or []
        first_page_keys = list(pages[0].keys()) if pages else []
        logger.warning(
            "No paragraphs extracted from parsed document. "
            "Expected structure: pages[].page_content.body.{title,main_text}. "
            "Got %d pages; first page keys: %s",
            len(pages), first_page_keys,
        )
        diagnostics = (
            ProjectionDiagnostic(
                level="warning",
                code="no_paragraphs_extracted",
                message="No paragraphs were extracted from parsed document.",
            ),
        )
    return (
        {"paragraphs": paragraphs},
        ProjectionQuality(
            confidence=confidence,
            lossiness="lossy",
            stats={"paragraphs": len(paragraphs)},
        ),
        diagnostics,
    )


def _paragraph_list_to_plain_text(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    source_scope = constraints.get("source_scope", "full_document")
    normalize = bool(constraints.get("normalize_whitespace", False))
    max_chars = constraints.get("max_chars")
    paragraphs = artifact.data.get("paragraphs", []) or []
    if source_scope == "body":
        paragraphs = [p for p in paragraphs if p.get("section") == "body"]
    texts = [p.get("text", "") for p in paragraphs if p.get("text")]
    text = "\n".join(texts)
    if normalize:
        text = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
    if isinstance(max_chars, int) and max_chars >= 0:
        text = text[:max_chars]
    return (
        {"text": text, "language": "zh", "source_scope": source_scope},
        ProjectionQuality(
            confidence=1.0 if text else 0.0,
            lossiness="lossy",
            stats={"chars": len(text)},
        ),
        (),
    )


def _search_results_to_reference_text_list(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    max_items = constraints.get("max_items")
    min_text_chars = int(constraints.get("min_text_chars", 1))
    dedupe = bool(constraints.get("dedupe", False))
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for hit in artifact.data.get("hits", []) or []:
        text = str(hit.get("chunk_text", "")).strip()
        if len(text) < min_text_chars:
            continue
        if dedupe and text in seen:
            continue
        seen.add(text)
        items.append(
            {
                "text": text,
                "title": hit.get("title"),
                "resource_id": hit.get("resource_id"),
                "source_id": hit.get("source_id"),
                "chunk_no": hit.get("chunk_no"),
                "score": hit.get("score"),
            }
        )
        if isinstance(max_items, int) and len(items) >= max_items:
            break
    return (
        {"items": items},
        ProjectionQuality(
            confidence=1.0 if items else 0.0,
            lossiness="lossy",
            stats={"items": len(items)},
        ),
        (),
    )


def audit_projector_graph(registry: ProjectorRegistry) -> dict[str, Any]:
    """Audit the projector graph for common issues.

    Returns a report dict with these keys:
    - ``unproduced_types``: artifact types that have consumers but no producers
    - ``unconsumed_types``: artifact types that have producers but no consumers
    - ``unused_projectors``: projectors with deprecated/disabled stability
    - ``duplicate_edges``: source-target pairs with multiple indistinguishable projectors
    - ``local_in_production``: projectors in the local layer
    - ``deep_paths``: paths where source_type and target_type are very far apart (>3 hops via different types)
    - ``missing_contracts``: hint that this is checked separately via ToolInputContract
    - ``debug_artifacts_in_business_paths``: any projector touching debug_view or with debug_only metadata
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
        f"{src} -> {tgt}": names
        for (src, tgt), names in edge_counts.items()
        if len(names) > 1
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
        "missing_contracts_hint": "Tools missing contracts are not tracked here; check ToolRegistry.",
        "debug_artifacts_in_business_paths": debug_touching,
        "total_projectors": len(registry.all()),
        "total_types": len(all_types),
    }


def _reference_text_list_to_text_collection(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    items = [
        {
            "text": item.get("text", ""),
            "metadata": {k: v for k, v in item.items() if k != "text"},
        }
        for item in artifact.data.get("items", []) or []
        if item.get("text")
    ]
    return (
        {"items": items},
        ProjectionQuality(
            confidence=1.0 if items else 0.0,
            lossiness="lossy",
            stats={"items": len(items)},
        ),
        (),
    )
