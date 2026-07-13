from __future__ import annotations

from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactMetadata,
    InputField,
    ProjectionPolicy,
)
from courtier.agent.artifacts.projectors import create_default_projector_registry
from courtier.agent.artifacts.resolver import ProjectionResolver


def test_resolves_two_step_plain_text_path():
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "detect_plagiarism"
    fields = (
        InputField(
            name="new_doc",
            artifact_type="core.plain_text",
            materialize_as="string",
            constraints={"source_scope": "body", "normalize_whitespace": True},
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields,
        tool_name,
        [artifact],
        ProjectionPolicy(),
    )

    assert resolution.status == "resolved"
    plan = resolution.plans["new_doc"]
    assert [step.projector_name for step in plan.steps] == [
        "docaudit.parsed_document.to_paragraph_list",
        "docaudit.paragraph_list.to_plain_text",
    ]


def test_ignores_debug_artifact_by_default():
    artifact = Artifact(
        artifact_id="$ref:debug_tool:1",
        artifact_type="core.plain_text",
        data={"text": "debug text"},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            semantic_role="primary_document",
            subject="current_upload",
            projection_allowed=False,
            debug_only=True,
        ),
    )
    tool_name = "detect_plagiarism"
    fields = (
        InputField(
            name="new_doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields,
        tool_name,
        [artifact],
        ProjectionPolicy(),
    )

    assert resolution.status == "failed"
    assert "field_unresolved" in resolution.diagnostics[0].code


def test_type_match_resolves_regardless_of_role():
    """With simplified matching (type-only), different roles no longer block resolution."""
    artifact = Artifact(
        artifact_id="reference-text",
        artifact_type="core.plain_text",
        data={"text": "reference"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="reference_document",
            subject="reference_library",
        ),
    )
    tool_name = "detect_plagiarism"
    fields = (
        InputField(
            name="new_doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields,
        tool_name,
        [artifact],
        ProjectionPolicy(),
    )

    # Role mismatch no longer blocks — matching is by artifact_type only.
    assert resolution.status == "resolved"


# -- Gap 3: scope and sensitivity matching -------------------------------------

def test_scope_is_ignored_with_simplified_matching():
    """With simplified matching, scope no longer affects artifact selection."""
    artifact = Artifact(
        artifact_id="scoped",
        artifact_type="core.plain_text",
        data={"text": "body text"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="primary_document",
            subject="current_upload",
            scope="body",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )
    assert resolution.status == "resolved"


def test_scope_mismatch_no_longer_rejects():
    """With simplified matching, scope mismatch does not reject artifacts."""
    artifact = Artifact(
        artifact_id="scoped",
        artifact_type="core.plain_text",
        data={"text": "title text"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="primary_document",
            subject="current_upload",
            scope="title",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
            constraints={"source_scope": "body"},
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )

    # Scope mismatch no longer rejects — matching is by artifact_type only.
    assert resolution.status == "resolved"


def test_sensitivity_no_longer_blocks_matching():
    """With simplified matching, sensitivity does not affect artifact selection."""
    artifact = Artifact(
        artifact_id="public",
        artifact_type="core.plain_text",
        data={"text": "pub"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="primary_document",
            subject="current_upload",
            sensitivity="public",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )
    # Sensitivity no longer blocks — matching is by artifact_type only.
    assert resolution.status == "resolved"


def test_resolver_enforces_policy_min_quality():
    """Projector with quality_score below policy.min_quality should be skipped."""
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(min_quality=0.99),
    )
    assert resolution.status == "failed"


def test_resolver_allows_lossless_only_direct_match():
    """When allow_lossy=False, direct type match (no projectors needed) still resolves."""
    artifact = Artifact(
        artifact_id="plain",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(allow_lossy=False),
    )
    assert resolution.status == "resolved"  # direct match, no projector needed


# -- Gap 2: schema version compatibility ---------------------------------------

def test_resolver_skips_projector_with_incompatible_source_schema_version():
    """Projector requiring >=1.0,<2.0 should skip artifact with schema_version=2.0."""
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        schema_version="2.0",  # incompatible with default source_schema_version=">=1.0,<2.0"
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )
    # Should fail because the only path uses projectors that reject v2.0
    assert resolution.status == "failed"


def test_resolver_accepts_compatible_schema_version():
    """Artifact with schema_version=1.0 should be compatible with >=1.0,<2.0."""
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        schema_version="1.0",
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )
    assert resolution.status == "resolved"


def test_resolver_default_schema_version_is_compatible():
    """Artifact without explicit schema_version (defaults to '1.0') should work."""
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        # schema_version defaults to "1.0"
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    tool_name = "test"
    fields = (
        InputField(
            name="doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )
    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        fields, tool_name, [artifact], ProjectionPolicy(),
    )
    assert resolution.status == "resolved"
