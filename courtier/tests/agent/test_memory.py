"""Tests for MemoryStore."""

from pathlib import Path

import pytest

from courtier.agent.memory.store import FileMemoryStore


class TestFileMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        return FileMemoryStore(root_dir=str(tmp_path))

    @pytest.mark.asyncio
    async def test_set_and_get(self, store):
        await store.set("key1", {"name": "test", "value": 42})
        result = await store.get("key1")
        assert result == {"name": "test", "value": 42}

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store):
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, store):
        await store.set("key1", "value")
        await store.delete("key1")
        assert await store.get("key1") is None

    @pytest.mark.asyncio
    async def test_list_keys_returns_all(self, store):
        await store.set("a", 1)
        await store.set("b", 2)
        keys = await store.list_keys()
        assert sorted(keys) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, store):
        await store.set("key", "session_val", namespace="session")
        await store.set("key", "persistent_val", namespace="persistent")
        assert await store.get("key", namespace="session") == "session_val"
        assert await store.get("key", namespace="persistent") == "persistent_val"

    @pytest.mark.asyncio
    async def test_clear_namespace(self, store):
        await store.set("a", 1, namespace="session")
        await store.set("b", 2, namespace="persistent")
        await store.clear_namespace("session")
        assert await store.get("a", namespace="session") is None
        assert await store.get("b", namespace="persistent") == 2

    def test_root_dir_created(self, tmp_path):
        root = str(tmp_path / "memory_store")
        FileMemoryStore(root_dir=root)
        assert Path(root).exists() and Path(root).is_dir()
