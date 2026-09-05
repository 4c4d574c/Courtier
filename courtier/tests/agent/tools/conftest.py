"""Shared helpers for artifact tool tests."""

import domain_artifacts  # domains/docaudit is on the pytest pythonpath

from courtier.agent.artifacts.executor import MaterializerRegistry
from courtier.agent.artifacts.models import default_registry
from courtier.agent.artifacts.projectors import register_rebuild_hook


# Same rationale as tests/agent/artifacts/conftest.py.
def _register_docaudit_materializers(reg) -> None:
    # reg IS the materializer registry being built — calling default()
    # here would recurse.
    domain_artifacts.register_domain_artifacts(default_registry, reg)


def _register_docaudit_projectors(reg) -> None:
    domain_artifacts.register_domain_artifacts(
        default_registry, MaterializerRegistry.default(), reg
    )


MaterializerRegistry.register_rebuild_hook("docaudit", _register_docaudit_materializers)
register_rebuild_hook("docaudit", _register_docaudit_projectors)

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata  # noqa: E402


def _make(
    aid: str,
    atype: str = "core.plain_text",
    data: dict | None = None,
    role: str = "document",
    subject: str = "current",
) -> Artifact:
    """Create an artifact with full data dict (used by get_artifact tests)."""
    return Artifact(
        artifact_id=aid,
        artifact_type=atype,
        data=data or {"text": "hello", "language": "zh", "source_scope": "full_document"},
        metadata=ArtifactMetadata(
            created_by="test",
            semantic_role=role,
            subject=subject,
            content_hash="sha256:x",
        ),
    )


def _make_simple(
    aid: str,
    atype: str = "core.plain_text",
    role: str = "document",
    subject: str = "current",
) -> Artifact:
    """Create an artifact with simple text data (used by list_artifacts tests)."""
    return Artifact(
        artifact_id=aid,
        artifact_type=atype,
        data={"text": "test"},
        metadata=ArtifactMetadata(
            created_by="parse_layout",
            semantic_role=role,
            subject=subject,
            content_hash="sha256:abc",
        ),
    )


def _noop_progress(progress: dict) -> None:
    pass
