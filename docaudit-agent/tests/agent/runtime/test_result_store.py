"""Tests for ResultStore and backends."""

import pytest

from courtier.agent.runtime.store import DiskResultBackend, ResultStore


@pytest.fixture
def disk_backend(tmp_path):
    return DiskResultBackend(cache_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_disk_backend_store_and_read(disk_backend):
    stored = await disk_backend.store("r-1", {"key": "value"}, {"actor_name": "test"})
    assert stored.result_id == "r-1"
    assert stored.backend == "disk"

    read = await disk_backend.read("r-1")
    assert "error" not in read
    assert read["data"] == {"key": "value"}


@pytest.mark.asyncio
async def test_disk_backend_read_missing(disk_backend):
    read = await disk_backend.read("missing")
    assert "error" in read
    assert read["data"] is None


@pytest.mark.asyncio
async def test_disk_backend_query_filter(disk_backend):
    data = "apple\nbanana\ncherry\ndate"
    await disk_backend.store("r-2", data, {"actor_name": "test"})
    read = await disk_backend.read("r-2", query="banana")
    assert "banana" in read["data"]
    assert "apple" not in read["data"]


@pytest.mark.asyncio
async def test_result_store_uses_fallback_when_primary_fails(tmp_path):
    from courtier.agent.core.cache_store import CacheStore

    class FailingBackend:
        name = "failing"

        async def store(self, result_id, data, metadata=None):
            raise RuntimeError("primary down")

        async def read(self, result_id, *, query=None, chunk_index=0, max_tokens=2000):
            raise RuntimeError("primary down")

        async def exists(self, result_id):
            raise RuntimeError("primary down")

    fallback = DiskResultBackend(cache_dir=str(tmp_path))
    store = ResultStore(primary=FailingBackend(), fallback=fallback)

    stored = await store.store("r-3", {"ok": True})
    assert stored.backend == "disk"

    read = await store.read("r-3")
    assert read["data"] == {"ok": True}


@pytest.mark.asyncio
async def test_result_store_without_primary_uses_fallback(tmp_path):
    from courtier.agent.core.cache_store import CacheStore

    fallback = DiskResultBackend(cache_dir=str(tmp_path))
    store = ResultStore(fallback=fallback)

    stored = await store.store("r-4", "hello")
    assert stored.backend == "disk"
    assert await store.exists("r-4") is True
