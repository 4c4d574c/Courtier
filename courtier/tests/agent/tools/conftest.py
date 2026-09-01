"""Shared helpers for artifact tool tests."""

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata


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
