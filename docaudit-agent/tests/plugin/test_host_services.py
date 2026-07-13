"""Tests for ProcessManager host service request handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.agent.core.cache_store import CacheStore
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.artifacts.registry import SessionArtifactStoreRegistry
from courtier.plugin.manager import PluginProcess, PluginState, ProcessManager
from courtier.plugin.manifest import PluginManifest
from courtier.plugin.protocol import (
    METHOD_CACHE_PERSIST,
    METHOD_CACHE_LOAD,
    METHOD_ARTIFACT_STORE_PUT,
)


def _make_process(permissions: list[str], host_services: list[str]) -> PluginProcess:
    manifest = PluginManifest(
        name="test_plugin",
        version="1.0.0",
        api="v1",
        dependencies={
            "permissions": permissions,
            "host_services": host_services,
        },
    )
    return PluginProcess(
        name="test_plugin",
        manifest=manifest,
        plugin_dir=Path("."),
        state=PluginState.ACTIVE,
    )


@pytest.mark.asyncio
class TestHostServiceHandler:
    @pytest.mark.asyncio
    async def test_cache_persist_allowed(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        registry = SessionArtifactStoreRegistry()
        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),  # not used
            cache_store=cache_store,
            artifact_store_registry=registry,
        )
        proc = _make_process(
            permissions=["write:cache", "read:cache"],
            host_services=["cache"],
        )
        handler = manager._create_host_request_handler(proc)

        result = await handler({
            "method": METHOD_CACHE_PERSIST,
            "params": {"data": "x" * 5000, "tool_name": "test_plugin.big"},
        })

        assert result["persisted"] is True
        assert result["ref_id"].startswith("$ref:test_plugin.big:")
        assert result["data"]["__persisted_output__"] is True

    @pytest.mark.asyncio
    async def test_cache_persist_denied_without_permission(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),
            cache_store=cache_store,
        )
        proc = _make_process(permissions=[], host_services=["cache"])
        handler = manager._create_host_request_handler(proc)

        result = await handler({
            "method": METHOD_CACHE_PERSIST,
            "params": {"data": "x", "tool_name": "big"},
        })

        assert "error" in result
        assert result["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_cache_load_resolves_ref(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),
            cache_store=cache_store,
        )
        proc = _make_process(
            permissions=["read:cache"],
            host_services=["cache"],
        )
        handler = manager._create_host_request_handler(proc)

        persist_result = await cache_store.persist({"value": 42}, "host_tool", force=True)
        ref_id = persist_result.ref_id

        result = await handler({
            "method": METHOD_CACHE_LOAD,
            "params": {"ref_id": ref_id},
        })

        assert result == {"value": 42}

    @pytest.mark.asyncio
    async def test_artifact_store_put_with_session(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        artifact_registry = SessionArtifactStoreRegistry()
        store = ArtifactStore()
        await artifact_registry.register("sess_1", store)

        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),
            cache_store=cache_store,
            artifact_store_registry=artifact_registry,
        )
        proc = _make_process(
            permissions=["write:artifacts"],
            host_services=["artifact_store"],
        )
        handler = manager._create_host_request_handler(proc)

        result = await handler({
            "method": METHOD_ARTIFACT_STORE_PUT,
            "params": {
                "session_id": "sess_1",
                "artifact": {
                    "artifact_id": "art_1",
                    "artifact_type": "test.result",
                    "data": {"result": "ok"},
                    "metadata": {
                        "created_by": "test",
                        "content_hash": "abc",
                        "semantic_role": "intermediate",
                    },
                },
            },
        })

        assert result is not None
        assert result["artifact_id"] == "art_1"
        assert store.get("art_1") is not None
