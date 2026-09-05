"""Core ``core.*`` artifact type schemas.

The core vocabulary: domain packages must NOT add entries here - they
register their own types via their ``domain_artifacts`` profile module
(wired by ``DomainActivator``).
"""

from __future__ import annotations

from .models import ArtifactSchema
from .schema_registry import ArtifactSchemaRegistry


def register_core_schemas(registry: ArtifactSchemaRegistry) -> None:
    """Register the core ``core.*`` types into *registry* (idempotent)."""
    schemas: dict[str, ArtifactSchema] = {}

    schemas["core.plain_text"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string"},
                "source_scope": {
                    "type": "string",
                    "enum": [
                        "full_document",
                        "body",
                        "header",
                        "footer",
                        "mixed",
                        "unknown",
                    ],
                },
            },
        },
        description="Plain text with optional language and scope metadata.",
    )

    schemas["core.text_collection"] = ArtifactSchema(
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
                            "metadata": {"type": "object"},
                        },
                    },
                },
            },
        },
        description="Ordered collection of text items with optional per-item metadata.",
    )

    schemas["core.json_object"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description=(
            "Fallback type for arbitrary JSON objects. "
            "Tool contracts should not casually require this."
        ),
    )

    schemas["core.document_markdown"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["markdown"],
            "properties": {
                "markdown": {"type": "string"},
                "format": {"type": "string"},
                "ocr": {"type": "boolean"},
                "pages": {"type": "integer"},
            },
        },
        description=("GitHub-Flavored Markdown rendering of a document (optionally OCR'd)."),
    )

    schemas["core.error_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["errors"],
            "properties": {
                "errors": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "string"},
            },
        },
        description="Structured error/warning report from an audit or validation tool.",
    )

    schemas["core.debug_view"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description="Debug-only view of cached tool output. Not for business data flow.",
    )

    schemas["core.cached_output"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description=(
            "Fallback type for tool outputs without an explicit artifact type contract.  "
            "Always projection-allowed so downstream tools can discover and use the data."
        ),
    )

    registry.register_many(schemas)
