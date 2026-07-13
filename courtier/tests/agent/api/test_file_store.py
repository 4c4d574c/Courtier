"""Tests for FileStore path-traversal safety."""

import tempfile
from pathlib import Path

import pytest

from courtier.agent.api.file_store import FileInfo, FileStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield FileStore(str(Path(d) / ".file_registry"))


class TestSafeResolve:
    def test_rejects_absolute_path(self):
        assert FileStore._safe_resolve(Path("/tmp/uploads"), "/etc/passwd") is None

    def test_rejects_parent_traversal(self):
        assert FileStore._safe_resolve(Path("/tmp/uploads"), "../../etc/passwd") is None

    def test_rejects_deep_traversal(self):
        assert (
            FileStore._safe_resolve(Path("/tmp/uploads"), "subdir/../../../etc/passwd")
            is None
        )

    def test_accepts_safe_relative_path(self):
        result = FileStore._safe_resolve(Path("/tmp/uploads"), "2024/01/01/x.pdf")
        assert result == Path("/tmp/uploads/2024/01/01/x.pdf")

    def test_rejects_empty_path(self):
        assert FileStore._safe_resolve(Path("/tmp/uploads"), "") is None


@pytest.mark.asyncio
class TestResolvePath:
    async def test_resolve_path_rejects_traversal(self, store):
        info = FileInfo(
            file_id="file_00000001",
            original_name="x.pdf",
            stored_path="../../etc/passwd",
            size_bytes=0,
        )
        store._files[info.file_id] = info
        assert await store.resolve_path(info.file_id, "/tmp/uploads") is None

    async def test_resolve_path_accepts_safe_relative_path(self, store):
        info = FileInfo(
            file_id="file_00000002",
            original_name="x.pdf",
            stored_path="2024/01/01/x.pdf",
            size_bytes=0,
        )
        store._files[info.file_id] = info
        resolved = await store.resolve_path(info.file_id, "/tmp/uploads")
        assert resolved == Path("/tmp/uploads/2024/01/01/x.pdf")

    async def test_resolve_path_nonexistent_id(self, store):
        assert await store.resolve_path("file_nonexistent", "/tmp/uploads") is None
