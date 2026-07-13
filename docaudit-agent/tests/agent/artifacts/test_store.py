from __future__ import annotations

from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactContext,
    ArtifactContextEntry,
    ArtifactMetadata,
    ArtifactPermission,
)
from courtier.agent.artifacts.store import ArtifactStore, ScopedArtifactStore


def test_put_and_get_artifact():
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(artifact)

    assert store.get("a1") == artifact


def test_list_projection_candidates_excludes_debug_artifacts():
    store = ArtifactStore()
    business = Artifact(
        artifact_id="business",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="projector"),
    )
    debug = Artifact(
        artifact_id="debug",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            projection_allowed=False,
            debug_only=True,
        ),
    )

    store.put(business)
    store.put(debug)

    assert store.list_projection_candidates() == [business]


def test_register_cached_ref_creates_artifact_with_metadata():
    store = ArtifactStore()

    artifact = store.register_cached_ref(
        ref_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_document",
        data={"pages": []},
        role="primary_document",
        subject="current_upload",
    )

    assert artifact.artifact_id == "$ref:parse_document:1"
    assert artifact.artifact_type == "docaudit.parsed_document"
    assert artifact.metadata.semantic_role == "primary_document"
    assert artifact.metadata.subject == "current_upload"


# -- Gap 6: persist / llm_visible policy enforcement ---------------------------

def test_persist_never_drops_artifact():
    store = ArtifactStore()
    store.set_type_policy("core.debug_view", persist="never", llm_visible="hidden")
    artifact = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(created_by="debug_tool"),
    )

    returned = store.put(artifact)
    assert store.get("debug1") is None  # not stored
    assert returned.artifact_id == "debug1"  # returned but not kept


def test_persist_auto_stores_artifact():
    store = ArtifactStore()
    store.set_type_policy("core.plain_text", persist="auto", llm_visible="summary")
    artifact = Artifact(
        artifact_id="plain1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(artifact)
    assert store.get("plain1") is not None


def test_list_visible_excludes_hidden_types():
    store = ArtifactStore()
    store.set_type_policy("core.plain_text", persist="auto", llm_visible="summary")
    store.set_type_policy("core.debug_view", persist="never", llm_visible="hidden")

    visible = Artifact(
        artifact_id="v1",
        artifact_type="core.plain_text",
        data={"text": "visible"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    hidden = Artifact(
        artifact_id="h1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(visible)
    store.put(hidden)  # debug_view with persist=never won't be stored

    visible_ids = [a.artifact_id for a in store.list_visible()]
    assert "v1" in visible_ids
    assert "h1" not in visible_ids


# -- Gap 6: scoped artifact context for subagents ------------------------------

def test_scoped_store_allow_all_bypasses_restrictions():
    """allow_all=True gives unrestricted access."""
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    store.put(artifact)

    ctx = ArtifactContext(allow_all=True)
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None
    assert len(scoped.list_projection_candidates()) == 1


def test_scoped_store_restricts_access_by_artifact_id():
    """Only artifacts listed in the context are accessible."""
    store = ArtifactStore()
    for aid in ("a1", "a2"):
        store.put(Artifact(
            artifact_id=aid,
            artifact_type="core.plain_text",
            data={"text": aid},
            metadata=ArtifactMetadata(created_by="test"),
        ))

    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(ref="a1", permissions=ArtifactPermission()),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None
    assert scoped.get("a2") is None  # not in context


def test_scoped_store_project_permission():
    """Artifact without project permission is excluded from projection candidates."""
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    store.put(artifact)

    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(
                ref="a1",
                permissions=ArtifactPermission(project=False),
            ),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None  # can read
    assert scoped.list_projection_candidates() == []  # but can't project


def test_scoped_store_debug_read_permission():
    """Debug artifacts are excluded when debug_read=False (default)."""
    store = ArtifactStore()
    debug = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            projection_allowed=False,
            debug_only=True,
        ),
    )
    store.put(debug)

    # Context without debug_read — debug artifact is invisible
    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(
                ref="debug1",
                permissions=ArtifactPermission(debug_read=False),
            ),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)
    assert scoped.get("debug1") is not None  # explicit allow overrides

    # Context without listing debug artifact at all
    ctx2 = ArtifactContext()
    scoped2 = ScopedArtifactStore(store, ctx2)
    assert scoped2.get("debug1") is None  # not allowed
    assert scoped2.list_projection_candidates() == []  # debug excluded


def test_scoped_store_can_create_new_artifacts():
    """Subagent can always create new artifacts in the backing store."""
    store = ArtifactStore()
    ctx = ArtifactContext()  # empty context
    scoped = ScopedArtifactStore(store, ctx)

    new_artifact = scoped.register_cached_ref(
        ref_id="subagent_result",
        artifact_type="docaudit.plagiarism_report",
        created_by="detect_plagiarism",
        data={"is_plagiarism": False},
        role="report",
    )
    assert store.get("subagent_result") is not None
    assert new_artifact.artifact_type == "docaudit.plagiarism_report"


def test_find_by_type_returns_matching_artifacts():
    store = ArtifactStore()
    store.put(Artifact(
        artifact_id="a1", artifact_type="core.plain_text",
        data={"text": "hello"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a2", artifact_type="docaudit.parsed_document",
        data={"pages": []}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a3", artifact_type="core.plain_text",
        data={"text": "world"}, metadata=ArtifactMetadata(created_by="test"),
    ))

    assert len(store.find_by_type("core.plain_text")) == 2
    assert len(store.find_by_type("docaudit.parsed_document")) == 1
    assert store.find_by_type("nonexistent") == []


def test_list_all_returns_all_stored():
    store = ArtifactStore()
    store.put(Artifact(
        artifact_id="a1", artifact_type="core.plain_text",
        data={"text": "hello"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a2", artifact_type="core.plain_text",
        data={"text": "world"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    assert len(store.list_all()) == 2


def test_require_raises_keyerror_for_missing_artifact():
    store = ArtifactStore()
    try:
        store.require("nonexistent")
        assert False, "Expected KeyError"
    except KeyError:
        pass
