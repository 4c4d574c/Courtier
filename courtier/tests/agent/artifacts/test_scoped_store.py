"""Tests for ScopedArtifactView — sub-agent artifact visibility isolation."""

from __future__ import annotations

import pytest

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata
from courtier.agent.artifacts.scoped_store import ScopedArtifactView
from courtier.agent.artifacts.store import ArtifactStore


def _artifact(artifact_id: str, data: object = None) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type="core.plain_text",
        data=data if data is not None else {"text": f"data-for-{artifact_id}"},
        metadata=ArtifactMetadata(created_by="test"),
    )


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(cache_dir=str(tmp_path / "cache"))


def _child_view(parent: ScopedArtifactView, scope: str, **kwargs) -> ScopedArtifactView:
    """Build a nested view the way AgentRuntime._build_scoped_store does."""
    return ScopedArtifactView(
        parent,
        scope=scope,
        allowed=parent.allowed | {parent.scope},
        **kwargs,
    )


# -- Visibility: own / ancestors / public visible, siblings hidden -------------


def test_view_sees_own_ancestor_and_public_but_not_sibling(store):
    store.put(_artifact("pub-1"))  # root write stays subject="unknown"

    orch = ScopedArtifactView(store, scope="h-orch")
    orch.put(_artifact("orch-1"))

    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")
    child_a.put(_artifact("a-1"))

    def ids(view):
        return {a.artifact_id for a in view.list_all()}

    assert ids(child_a) == {"pub-1", "orch-1", "a-1"}
    assert ids(child_b) == {"pub-1", "orch-1"}  # sibling's a-1 hidden
    assert ids(orch) == {"pub-1", "orch-1"}  # parent does not see child scope
    assert {a.artifact_id for a in store.list_all()} == {"pub-1", "orch-1", "a-1"}

    # get / require behave like "not found" for hidden artifacts.
    assert child_b.get("a-1") is None
    with pytest.raises(KeyError, match="Artifact not found: a-1"):
        child_b.require("a-1")
    assert child_a.get("a-1") is not None

    # Enumerating read paths apply the same filter.
    assert [a.artifact_id for a in child_b.list_projection_candidates()] == [
        "pub-1",
        "orch-1",
    ]
    assert [a.artifact_id for a in child_b.list_visible()] == ["pub-1", "orch-1"]
    assert [a.artifact_id for a in child_b.find_by_type("core.plain_text")] == [
        "pub-1",
        "orch-1",
    ]


def test_nested_view_inherits_ancestor_chain_but_not_uncle(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    orch.put(_artifact("orch-1"))

    child_a = _child_view(orch, "h-a")
    child_a.put(_artifact("a-1"))
    uncle = _child_view(orch, "h-uncle")
    uncle.put(_artifact("uncle-1"))

    grandchild = _child_view(child_a, "h-g")
    grandchild.put(_artifact("g-1"))

    ids = {a.artifact_id for a in grandchild.list_all()}
    assert ids == {"orch-1", "a-1", "g-1"}  # grandparent + parent + own, no uncle

    # The grandchild's own writes stay hidden from parent and uncle views.
    assert "g-1" not in {a.artifact_id for a in child_a.list_all()}
    assert "g-1" not in {a.artifact_id for a in uncle.list_all()}


def test_extra_allowed_explicitly_admits_sibling_scope(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_a.put(_artifact("a-1"))

    # ref_ids-style explicit allow-list: h-b may see h-a's artifacts.
    child_b = _child_view(orch, "h-b", extra_allowed={"h-a"})
    assert child_b.get("a-1") is not None
    assert "a-1" in {a.artifact_id for a in child_b.list_all()}

    # Without the explicit grant the sibling stays hidden.
    child_c = _child_view(orch, "h-c")
    assert child_c.get("a-1") is None


# -- Write path: creator stamping -----------------------------------------------


def test_writes_through_view_are_stamped_with_view_scope(store):
    view = ScopedArtifactView(store, scope="h-1")

    stamped = view.put(_artifact("via-put"))
    assert stamped.metadata.subject == "h-1"
    assert store.require("via-put").metadata.subject == "h-1"

    registered = view.register_cached_ref(
        ref_id="$ref:echo:1",
        artifact_type="core.plain_text",
        created_by="echo",
        data={"text": "hi"},
    )
    assert registered.metadata.subject == "h-1"
    assert store.require("$ref:echo:1").metadata.subject == "h-1"

    # A caller-provided subject is always overridden — the artifact belongs
    # to the creator writing through the view.
    overridden = view.register_cached_ref(
        ref_id="$ref:echo:2",
        artifact_type="core.plain_text",
        created_by="echo",
        data={"text": "hi again"},
        subject="someone-else",
    )
    assert overridden.metadata.subject == "h-1"

    pre_stamped = _artifact("pre-stamped").model_copy(
        update={"metadata": ArtifactMetadata(created_by="test", subject="other")}
    )
    assert view.put(pre_stamped).metadata.subject == "h-1"


def test_root_store_direct_writes_stay_public(store):
    store.put(_artifact("pub-1"))
    registered = store.register_cached_ref(
        ref_id="$ref:root:1",
        artifact_type="core.plain_text",
        created_by="root",
        data={"text": "root data"},
    )
    assert store.require("pub-1").metadata.subject == "unknown"
    assert registered.metadata.subject == "unknown"

    view = ScopedArtifactView(store, scope="h-1")
    assert view.get("pub-1") is not None
    assert view.get("$ref:root:1") is not None


# -- Persistence read paths ------------------------------------------------------


async def _persist_and_register(
    store: ArtifactStore, view: ScopedArtifactView, tool: str, data: object
) -> str:
    result = await view.persist(data, tool, force=True)
    assert result.persisted
    view.register_cached_ref(
        ref_id=result.ref_id,
        artifact_type="core.plain_text",
        created_by=tool,
        data=data,
    )
    return result.ref_id


@pytest.mark.asyncio
async def test_persistence_reads_hide_sibling_refs(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")

    data = {"text": "sibling A payload " * 100}
    ref_id = await _persist_and_register(store, child_a, "tool_a", data)

    # Owner and root store can read it.
    owner_read = await child_a.read(ref_id)
    assert "error" not in owner_read
    assert child_a.exists(ref_id)
    assert child_a.get_info(ref_id) is not None
    assert child_a.load(ref_id) is not None
    assert ref_id in child_a.ref_map
    assert (await store.read(ref_id)).get("data") is not None

    # Sibling gets exactly the "not found" behaviour on every read path.
    sibling_read = await child_b.read(ref_id)
    assert sibling_read == {"error": f"result not found: {ref_id}", "data": None}
    assert not child_b.exists(ref_id)
    assert child_b.get_info(ref_id) is None
    assert child_b.load(ref_id) is None
    assert ref_id not in child_b.ref_map


@pytest.mark.asyncio
async def test_unregistered_persisted_refs_stay_public(store):
    """Refs persisted without typed-artifact registration bypass no filter."""
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")

    result = await store.persist({"text": "summarizer result " * 100}, "skill_x", force=True)
    assert result.persisted

    # No register_cached_ref call — like summarizer-persisted results.
    assert (await child_a.read(result.ref_id)).get("data") is not None
    assert (await child_b.read(result.ref_id)).get("data") is not None


@pytest.mark.asyncio
async def test_resolve_refs_keeps_hidden_refs_as_literals(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")

    hidden_ref = await _persist_and_register(store, child_a, "tool_a", {"v": "a-data"})
    public = await store.persist({"v": "public-data"}, "tool_pub", force=True)
    store.register_cached_ref(
        ref_id=public.ref_id,
        artifact_type="core.plain_text",
        created_by="tool_pub",
        data={"v": "public-data"},
    )

    # Owner resolves both; the sibling resolves only the public ref.
    resolved_owner = child_a.resolve_refs({"doc": hidden_ref, "pub": public.ref_id})
    assert resolved_owner["doc"] == {"v": "a-data"}
    assert resolved_owner["pub"] == {"v": "public-data"}

    resolved_sibling = child_b.resolve_refs({"doc": hidden_ref, "pub": public.ref_id})
    assert resolved_sibling["doc"] == hidden_ref  # kept as literal string
    assert resolved_sibling["pub"] == {"v": "public-data"}


@pytest.mark.asyncio
async def test_resolve_refs_masks_hidden_embedded_refs(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")

    hidden_ref = await _persist_and_register(store, child_a, "tool_a", "secret-text")
    prose = f"审计文档：{hidden_ref} 的结果"

    resolved_sibling = child_b.resolve_refs({"task": prose})
    assert resolved_sibling["task"] == prose  # original string preserved

    resolved_owner = child_a.resolve_refs({"task": prose})
    assert resolved_owner["task"] == "审计文档：secret-text 的结果"


@pytest.mark.asyncio
async def test_resolve_refs_hides_persisted_output_markers(store):
    orch = ScopedArtifactView(store, scope="h-orch")
    child_a = _child_view(orch, "h-a")
    child_b = _child_view(orch, "h-b")

    hidden_ref = await _persist_and_register(store, child_a, "tool_a", {"v": 1})
    marker = {"__persisted_output__": True, "ref_id": hidden_ref}

    resolved_sibling = child_b.resolve_refs({"payload": marker})
    assert resolved_sibling["payload"] == hidden_ref  # same as backend miss
    resolved_owner = child_a.resolve_refs({"payload": marker})
    assert resolved_owner["payload"] == {"v": 1}


# -- Interface behaviour ----------------------------------------------------------


def test_view_is_not_an_artifact_store_instance(store):
    view = ScopedArtifactView(store, scope="h-1")
    assert not isinstance(view, ArtifactStore)


def test_uncovered_attributes_delegate_to_inner_store(store, tmp_path):
    view = ScopedArtifactView(store, scope="h-1")
    assert view.cache_dir == store.cache_dir

    # set_type_policy is not overridden — delegation keeps it working.
    view.set_type_policy("core.plain_text", persist="always", llm_visible="full")
    assert store._persist_policies["core.plain_text"] == "always"
