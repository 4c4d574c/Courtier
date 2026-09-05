"""Docaudit artifact profile: schemas, materializers, projection hints.

Registered by ``DomainActivator`` when the docaudit domain activates —
the core artifacts machinery carries no docaudit knowledge.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from courtier.agent.artifacts.executor import MaterializerRegistry
from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactSchema,
    ProjectionDiagnostic,
    ProjectionQuality,
    register_domain_materialize_as_defaults,
)
from courtier.agent.artifacts.projectors import (
    Projector,
    ProjectorRegistry,
    ProjectorSpec,
    _append_paragraph,
    _iter_body_paragraphs,
)
from courtier.agent.artifacts.schema_registry import ArtifactSchemaRegistry

logger = logging.getLogger(__name__)


def register_domain_artifacts(
    schemas: ArtifactSchemaRegistry,
    materializers: MaterializerRegistry,
    projectors: "ProjectorRegistry | None" = None,
) -> None:
    """Register docaudit artifact types (idempotent; safe to re-call)."""
    build_schemas(schemas)
    register_materializers(materializers)
    if projectors is not None:
        register_projectors(projectors)


def build_schemas(registry: ArtifactSchemaRegistry) -> None:
    """Register the docaudit ``docaudit.*`` types into *registry*."""
    schemas: dict[str, ArtifactSchema] = {}

    schemas["docaudit.parsed_layout"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["pages"],
            "properties": {"pages": {"type": "array"}},
        },
        description="Parsed document with pages, each containing body/header/footer structure.",
    )

    schemas["docaudit.paragraph_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["paragraphs"],
            "properties": {
                "paragraphs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text", "section"],
                        "properties": {
                            "index": {"type": "integer"},
                            "text": {"type": "string"},
                            "section": {"type": "string"},
                            "source_path": {"type": "string"},
                            "style": {"type": "object"},
                        },
                    },
                },
            },
        },
        description="Ordered list of document paragraphs extracted from parsed document.",
    )

    schemas["docaudit.search_results"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["total", "hits"],
            "properties": {
                "total": {"type": "integer"},
                "took_ms": {"type": "integer"},
                "hits": {"type": "array"},
            },
        },
        description="Raw search results from document search, containing total count and hit list.",
    )

    schemas["docaudit.document_structure"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["sections"],
            "properties": {
                "sections": {"type": "array"},
                "outline": {"type": "array"},
                "page_count": {"type": "integer"},
            },
        },
        description="Structural analysis of a document (sections, outline, page layout).",
    )

    schemas["docaudit.audit_finding_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "message"],
                        "properties": {
                            "severity": {"type": "string"},
                            "message": {"type": "string"},
                            "location": {"type": "string"},
                            "rule": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
        description="List of audit findings produced by any audit agent.",
    )

    schemas["docaudit.audit_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["auditor", "findings"],
            "properties": {
                "auditor": {"type": "string"},
                "doc_type": {"type": "string"},
                "findings": {"type": "array"},
                "passed": {"type": "boolean"},
                "summary": {"type": "string"},
                "details": {"type": "object"},
            },
        },
        description="Aggregated audit report from a single auditor.",
    )

    schemas["docaudit.reference_text_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "title": {"type": "string"},
                            "resource_id": {"type": "integer"},
                            "source_id": {"type": "string"},
                            "chunk_no": {"type": "integer"},
                            "score": {"type": "number"},
                        },
                    },
                },
            },
        },
        description="Filtered reference text items extracted from search results.",
    )

    schemas["docaudit.plagiarism_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["is_plagiarism", "max_similarity"],
            "properties": {
                "is_plagiarism": {"type": "boolean"},
                "max_similarity": {"type": "number"},
                "dynamic_threshold": {"type": "number"},
                "matched_doc_index": {"type": "integer"},
                "matched_substring_length": {"type": "integer"},
                "matched_text": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        description="Plagiarism detection report.",
    )

    registry.register_many(schemas)


def register_materializers(materializers: MaterializerRegistry) -> None:
    """Register docaudit materializers and projection hints."""
    materializers.register("docaudit.parsed_layout", "dict", lambda artifact: artifact.data)
    materializers.register(
        "docaudit.paragraph_list",
        "list_string",
        lambda artifact: [p.get("text", "") for p in artifact.data.get("paragraphs", [])],
    )
    materializers.register("docaudit.paragraph_list", "dict", lambda artifact: artifact.data)
    materializers.register(
        "docaudit.paragraph_list",
        "list_dict",
        lambda artifact: artifact.data.get("paragraphs", []),
    )

    materializers.register_projection_candidates(
        "list_string", ("docaudit.paragraph_list",)
    )
    materializers.register_projection_candidates(
        "list_dict", ("docaudit.paragraph_list", "docaudit.reference_text_list")
    )
    materializers.register_default_materialize_as(
        {
            "docaudit.paragraph_list": "dict",
            "docaudit.reference_text_list": "dict",
            "docaudit.search_results": "dict",
            "docaudit.parsed_layout": "dict",
            "docaudit.audit_finding_list": "dict",
            "docaudit.audit_report": "dict",
            "docaudit.document_structure": "dict",
            "docaudit.plagiarism_report": "dict",
        }
    )
    register_domain_materialize_as_defaults(
        {
            "docaudit.parsed_layout": "dict",
            "docaudit.paragraph_list": "list_dict",
            "docaudit.search_results": "dict",
            "docaudit.document_structure": "dict",
            "docaudit.audit_finding_list": "list_dict",
            "docaudit.audit_report": "dict",
            "docaudit.reference_text_list": "list_dict",
            "docaudit.plagiarism_report": "dict",
        }
    )


# -- Projector functions ------------------------------------------------------

def _parsed_layout_to_paragraph_list(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    paragraphs: list[dict[str, Any]] = []
    for section, paragraph, source_path in _iter_body_paragraphs(artifact.data):
        _append_paragraph(paragraphs, paragraph, section=section, source_path=source_path)
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
            len(pages),
            first_page_keys,
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



def register_projectors(registry) -> None:
    """Register the docaudit projection edges."""
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.parsed_layout.to_paragraph_list",
                source_type="docaudit.parsed_layout",
                target_type="docaudit.paragraph_list",
                owner="document",
                quality_score=0.95,
                lossiness="lossy",
                cost="cheap",
                supported_constraints=("source_scope",),
                description="Extract document paragraphs in reading order.",
            ),
            _parsed_layout_to_paragraph_list,
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
