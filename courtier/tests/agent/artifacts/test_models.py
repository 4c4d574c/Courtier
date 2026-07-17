from __future__ import annotations

from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactMetadata,
    ArtifactSchema,
    InputField,
    MaterializerSpec,
    ProjectionFeatureFlags,
    ProjectionPolicy,
    ProjectorSpec,
    build_contract_from_input_fields,
    derive_upstream_producers,
    get_artifact_schema,
    make_artifact_ref,
    parse_artifact_ref,
    register_artifact_schema,
    validate_artifact_data,
)
from courtier.agent.artifacts.projectors import create_default_projector_registry

# -- Gap 2: ArtifactMetadata schema_version -----------------------------------

def test_artifact_metadata_defaults_schema_version():
    meta = ArtifactMetadata(created_by="test")
    assert meta.schema_version == "1.0"


def test_artifact_metadata_explicit_schema_version():
    meta = ArtifactMetadata(created_by="test", schema_version="2.0")
    assert meta.schema_version == "2.0"


def test_artifact_defaults_to_projection_allowed_business_artifact():
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="docaudit.parsed_document",
        schema_version="1.0",
        data={"pages": []},
        metadata=ArtifactMetadata(created_by="parse_document"),
    )

    assert artifact.metadata.projection_allowed is True
    assert artifact.metadata.debug_only is False
    assert artifact.metadata.semantic_role == "intermediate"


def test_debug_artifact_is_not_projection_candidate():
    artifact = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        schema_version="1.0",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            semantic_role="debug",
            subject="debug",
            projection_allowed=False,
            debug_only=True,
        ),
    )

    assert artifact.is_projection_candidate is False


def test_input_fields_contract_building():
    fields = (
        InputField(
            name="new_doc",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
        InputField(
            name="library_docs",
            artifact_type="core.text_collection",
            materialize_as="list_string",
        ),
    )
    normalized = build_contract_from_input_fields("detect_plagiarism", fields)
    assert len(normalized) == 2
    assert normalized[0].name == "new_doc"
    assert normalized[0].artifact_type == "core.plain_text"
    assert normalized[0].materialize_as == "string"
    assert normalized[0].required is True
    assert normalized[1].name == "library_docs"
    assert normalized[1].artifact_type == "core.text_collection"
    assert normalized[1].materialize_as == "list_string"


def test_input_field_defaults():
    field = InputField(name="text", artifact_type="core.plain_text")
    assert field.required is True
    assert field.constraints == {}
    assert field.materialize_as is None


def test_projection_policy_defaults_are_safe():
    policy = ProjectionPolicy()

    assert policy.max_depth == 3
    assert policy.allow_debug_artifacts is False
    assert policy.allow_deprecated is False


# -- Gap 4: ProjectorSpec source/target schema version + test_fixtures ---------

def test_projector_spec_defaults_schema_versions():
    spec = ProjectorSpec(
        name="test.projector",
        source_type="core.plain_text",
        target_type="core.text_collection",
        owner="test",
    )
    assert spec.source_schema_version == ">=1.0,<2.0"
    assert spec.target_schema_version == "1.0"
    assert spec.test_fixtures == ()


def test_projector_spec_explicit_fixtures():
    spec = ProjectorSpec(
        name="test.projector",
        source_type="a",
        target_type="b",
        owner="test",
        test_fixtures=("fixture_a.json", "fixture_b.json"),
    )
    assert spec.test_fixtures == ("fixture_a.json", "fixture_b.json")


# -- Gap 5: MaterializerSpec path field ----------------------------------------

def test_materializer_spec_default_path_is_empty():
    spec = MaterializerSpec(artifact_type="core.plain_text", materialize_as="string")
    assert spec.path == ""


def test_materializer_spec_explicit_path():
    spec = MaterializerSpec(
        artifact_type="core.plain_text", materialize_as="string", path="$.text"
    )
    assert spec.path == "$.text"


# -- Gap 12: core.json_object and core.error_report schemas -------------------

def test_core_json_object_schema_exists():
    schema = get_artifact_schema("core.json_object")
    assert schema is not None
    assert schema.schema_version == "1.0"


def test_core_error_report_schema_exists():
    schema = get_artifact_schema("core.error_report")
    assert schema is not None
    assert "errors" in schema.schema_body.get("required", [])


# -- Gap 1: ArtifactSchema validation -----------------------------------------

def test_validate_plain_text_passes_for_valid_data():
    errors = validate_artifact_data("core.plain_text", {"text": "hello", "language": "zh"})
    assert errors == []


def test_validate_plain_text_fails_for_missing_text():
    errors = validate_artifact_data("core.plain_text", {"language": "zh"})
    assert len(errors) >= 1
    assert "missing required key" in errors[0]


def test_validate_unknown_type_passes_through():
    errors = validate_artifact_data("custom.unknown", {"anything": 1})
    assert errors == []


def test_validate_non_dict_returns_error():
    errors = validate_artifact_data("core.plain_text", "not-a-dict")
    assert len(errors) >= 1


def test_validate_text_collection_requires_items():
    errors = validate_artifact_data("core.text_collection", {})
    assert len(errors) >= 1
    assert any("items" in e for e in errors)


def test_validate_text_collection_valid_passes():
    errors = validate_artifact_data(
        "core.text_collection",
        {"items": [{"text": "hello", "metadata": {}}]},
    )
    assert errors == []


def test_validate_type_mismatch_reports_error():
    errors = validate_artifact_data("core.plain_text", {"text": 123})
    assert len(errors) >= 1
    assert "expected string" in errors[0]


def test_register_and_retrieve_custom_schema():
    custom = ArtifactSchema(
        schema_version="2.0",
        schema_body={"type": "object", "required": ["id"]},
    )
    register_artifact_schema("custom.test_type", custom)
    retrieved = get_artifact_schema("custom.test_type")
    assert retrieved is not None
    assert retrieved.schema_version == "2.0"


# -- Gap 12: derive_upstream_producers coverage --------------------------------
# (replaces the removed upstream_producer_for which relied on the deleted
# _UPSTREAM_PRODUCERS static mapping)


def test_derive_upstream_producers_direct():
    """Tools with output_artifact_type are returned as direct producers."""

    class FakeTool:
        name = "parse"
        output_artifact_type = "docaudit.parsed_document"

    producers = derive_upstream_producers(
        [FakeTool()], create_default_projector_registry()
    )
    assert "docaudit.parsed_document" in producers
    assert "parse" in producers["docaudit.parsed_document"]


def test_derive_upstream_producers_unknown_type_absent():
    producers = derive_upstream_producers([], create_default_projector_registry())
    assert "core.unknown_type" not in producers


# -- Gap 4: new artifact type schemas -----------------------------------------

def test_docaudit_document_metadata_schema_exists():
    schema = get_artifact_schema("docaudit.document_metadata")
    assert schema is not None
    assert "doc_id" in schema.schema_body.get("required", [])


def test_docaudit_document_structure_schema_exists():
    schema = get_artifact_schema("docaudit.document_structure")
    assert schema is not None
    assert "sections" in schema.schema_body.get("required", [])


def test_docaudit_audit_finding_list_schema_exists():
    schema = get_artifact_schema("docaudit.audit_finding_list")
    assert schema is not None
    assert "findings" in schema.schema_body.get("required", [])


def test_docaudit_audit_report_schema_exists():
    schema = get_artifact_schema("docaudit.audit_report")
    assert schema is not None
    assert "auditor" in schema.schema_body.get("required", [])


# -- Gap 7: feature flags -----------------------------------------------------

def test_feature_flags_default_all_enabled():
    flags = ProjectionFeatureFlags()
    assert flags.artifact_contracts_enabled is True
    assert flags.projection_resolver_enabled is True
    assert flags.tool_auto_binding_enabled is True
    assert flags.hide_debug_tools_for_task_agents is True
    assert flags.resolver_dry_run is False


def test_projection_policy_includes_feature_flags():
    policy = ProjectionPolicy()
    assert policy.features.artifact_contracts_enabled is True


# -- Gap 8: ArtifactRef URL scheme --------------------------------------------

def test_parse_artifact_ref_uri():
    parsed = parse_artifact_ref("artifact://docaudit.parsed_document/parse_document/1")
    assert parsed == {"type": "docaudit.parsed_document", "tool": "parse_document", "n": "1"}


def test_parse_artifact_ref_legacy_dollar_ref():
    """Legacy $ref format is no longer parsed — returns None."""
    parsed = parse_artifact_ref("$ref:parse_document:1")
    assert parsed is None


def test_parse_artifact_ref_invalid_returns_none():
    assert parse_artifact_ref("not_a_ref") is None
    assert parse_artifact_ref("") is None


def test_make_artifact_ref_builds_uri():
    ref = make_artifact_ref("core.plain_text", "projected", 3)
    assert ref == "artifact://core.plain_text/projected/3"
