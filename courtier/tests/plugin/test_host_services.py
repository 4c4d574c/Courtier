"""Tests for ProcessManager host service request handling."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from courtier.agent.artifacts.registry import SessionArtifactStoreRegistry
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.core.cache_store import CacheStore
from courtier.plugin.manager import PluginProcess, PluginState, ProcessManager
from courtier.plugin.manifest import PluginManifest
from courtier.plugin.protocol import (
    METHOD_ARTIFACT_STORE_PUT,
    METHOD_CACHE_LOAD,
    METHOD_CACHE_PERSIST,
    METHOD_STORAGE_PRESIGN_GET,
    METHOD_STORAGE_PUT,
    METHOD_TEMPLATE_STORE_GET,
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
            artifact_store=cache_store,
            artifact_store_registry=registry,
        )
        proc = _make_process(
            permissions=["write:cache", "read:cache"],
            host_services=["cache"],
        )
        handler = manager._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_CACHE_PERSIST,
                "params": {"data": "x" * 5000, "tool_name": "test_plugin.big"},
            }
        )

        assert result["persisted"] is True
        assert result["ref_id"].startswith("$ref:test_plugin.big:")
        assert result["data"]["__persisted_output__"] is True

    @pytest.mark.asyncio
    async def test_cache_persist_denied_without_permission(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),
            artifact_store=cache_store,
        )
        proc = _make_process(permissions=[], host_services=["cache"])
        handler = manager._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_CACHE_PERSIST,
                "params": {"data": "x", "tool_name": "big"},
            }
        )

        assert "error" in result
        assert result["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_cache_load_resolves_ref(self, tmp_path: Path):
        cache_store = CacheStore(cache_dir=str(tmp_path / "cache"))
        manager = ProcessManager(
            plugin_dir=tmp_path,
            extension_registry=object(),
            artifact_store=cache_store,
        )
        proc = _make_process(
            permissions=["read:cache"],
            host_services=["cache"],
        )
        handler = manager._create_host_request_handler(proc)

        persist_result = await cache_store.persist({"value": 42}, "host_tool", force=True)
        ref_id = persist_result.ref_id

        result = await handler(
            {
                "method": METHOD_CACHE_LOAD,
                "params": {"ref_id": ref_id},
            }
        )

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
            artifact_store=cache_store,
            artifact_store_registry=artifact_registry,
        )
        proc = _make_process(
            permissions=["write:artifacts"],
            host_services=["artifact_store"],
        )
        handler = manager._create_host_request_handler(proc)

        result = await handler(
            {
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
            }
        )

        assert result is not None
        assert result["artifact_id"] == "art_1"
        assert store.get("art_1") is not None


@pytest.mark.asyncio
class TestStoragePutHostService:
    """storage.put：插件产物经宿主上传到 MinIO 并返回预签名下载链接。"""

    def _manager(self, tmp_path: Path) -> ProcessManager:
        return ProcessManager(plugin_dir=tmp_path, extension_registry=object())

    async def test_storage_put_allowed(self, tmp_path: Path, monkeypatch):
        import courtier.storage.client as storage_client_mod

        uploaded: dict = {}
        monkeypatch.setattr(
            "courtier.plugin.manager.get_settings",
            lambda: SimpleNamespace(
                minio_endpoint="minio:9000", minio_bucket_plugin_io="courtier-plugin-io"
            ),
        )

        def _fake_put(bucket, key, data, content_type="application/octet-stream"):
            uploaded.update(bucket=bucket, key=key, data=data, content_type=content_type)

        monkeypatch.setattr(storage_client_mod, "put_object", _fake_put)
        monkeypatch.setattr(
            storage_client_mod,
            "get_presigned_url",
            lambda bucket, key, expires=3600: f"http://minio/{bucket}/{key}?e={expires}",
        )

        proc = _make_process(permissions=["write:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PUT,
                "params": {
                    "filename": "报告_annotated.docx",
                    "data_b64": base64.b64encode(b"docx-bytes").decode(),
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument" ".wordprocessingml.document"
                    ),
                },
            }
        )

        assert result["bucket"] == "courtier-plugin-io"
        assert result["object_key"].startswith("plugin-outputs/")
        assert result["object_key"].endswith("/报告_annotated.docx")
        assert result["download_url"].startswith("http://minio/courtier-plugin-io/")
        assert result["size_bytes"] == len(b"docx-bytes")
        assert result["expires_in"] > 0
        assert uploaded["data"] == b"docx-bytes"
        assert uploaded["bucket"] == "courtier-plugin-io"

    async def test_storage_put_denied_without_permission(self, tmp_path: Path):
        proc = _make_process(permissions=[], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PUT,
                "params": {
                    "filename": "a.docx",
                    "data_b64": base64.b64encode(b"x").decode(),
                },
            }
        )

        assert result["error"]["code"] == -32602

    async def test_storage_put_denied_when_service_not_declared(self, tmp_path: Path):
        proc = _make_process(permissions=["write:storage"], host_services=[])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PUT,
                "params": {
                    "filename": "a.docx",
                    "data_b64": base64.b64encode(b"x").decode(),
                },
            }
        )

        assert result["error"]["code"] == -32602

    async def test_storage_put_unavailable_when_minio_unconfigured(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(
            "courtier.plugin.manager.get_settings",
            lambda: SimpleNamespace(minio_endpoint="", minio_bucket_docs="courtier-docs"),
        )
        proc = _make_process(permissions=["write:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PUT,
                "params": {
                    "filename": "a.docx",
                    "data_b64": base64.b64encode(b"x").decode(),
                },
            }
        )

        assert result["error"]["code"] == -32603


@pytest.mark.asyncio
class TestStoragePresignGetHostService:
    """storage.presign_get：仅中转 bucket 可签名，权限与 key 校验。"""

    def _manager(self, tmp_path: Path) -> ProcessManager:
        return ProcessManager(plugin_dir=tmp_path, extension_registry=object())

    def _patch(self, monkeypatch):
        import courtier.storage.client as storage_client_mod

        monkeypatch.setattr(
            "courtier.plugin.manager.get_settings",
            lambda: SimpleNamespace(
                minio_endpoint="minio:9000", minio_bucket_plugin_io="courtier-plugin-io"
            ),
        )
        monkeypatch.setattr(
            storage_client_mod,
            "get_presigned_url",
            lambda bucket, key, expires=3600: f"http://minio/{bucket}/{key}?e={expires}",
        )

    async def test_presign_transfer_bucket_allowed(self, tmp_path: Path, monkeypatch):
        self._patch(monkeypatch)
        proc = _make_process(permissions=["read:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PRESIGN_GET,
                "params": {"bucket": "courtier-plugin-io", "key": "out/annotate/x/a.docx"},
            }
        )

        assert result["download_url"].startswith("http://minio/courtier-plugin-io/out/annotate/")
        assert result["expires_in"] > 0
        assert result["object_key"] == "out/annotate/x/a.docx"

    async def test_presign_other_bucket_denied(self, tmp_path: Path, monkeypatch):
        self._patch(monkeypatch)
        proc = _make_process(permissions=["read:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PRESIGN_GET,
                "params": {"bucket": "courtier-docs", "key": "plugin-outputs/x/a.docx"},
            }
        )

        assert result["error"]["code"] == -32602

    async def test_presign_path_traversal_key_denied(self, tmp_path: Path, monkeypatch):
        self._patch(monkeypatch)
        proc = _make_process(permissions=["read:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        for bad_key in ("../escape", "out/../../x", "/absolute"):
            result = await handler(
                {
                    "method": METHOD_STORAGE_PRESIGN_GET,
                    "params": {"bucket": "courtier-plugin-io", "key": bad_key},
                }
            )
            assert result["error"]["code"] == -32602, bad_key

    async def test_presign_denied_without_permission(self, tmp_path: Path, monkeypatch):
        self._patch(monkeypatch)
        proc = _make_process(permissions=["write:storage"], host_services=["storage"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_STORAGE_PRESIGN_GET,
                "params": {"bucket": "courtier-plugin-io", "key": "out/x/a.docx"},
            }
        )

        assert result["error"]["code"] == -32602


@pytest.mark.asyncio
class TestTemplateStoreGetHostService:
    """template_store.get：插件经宿主 RPC 读取格式模板，DB 凭证不出宿主。"""

    def _manager(self, tmp_path: Path) -> ProcessManager:
        return ProcessManager(plugin_dir=tmp_path, extension_registry=object())

    def _patch_db(self, monkeypatch, rows):
        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _FakeDb:
            def session(self):
                return _FakeSession()

        class _FakeRepo:
            def __init__(self, table):
                pass

            async def get(self, session, template_id):
                return rows[0] if rows else None

            async def list(self, session, **filters):
                return rows

        import courtier.agent.api.db as api_db
        import courtier.db as db_mod

        monkeypatch.setattr(api_db, "get_db", lambda: _FakeDb())
        monkeypatch.setattr(db_mod, "CRUDRepository", _FakeRepo)

    async def test_template_get_default_allowed(self, tmp_path: Path, monkeypatch):
        self._patch_db(monkeypatch, rows=[SimpleNamespace(content={"version": 1})])
        proc = _make_process(permissions=["read:templates"], host_services=["template_store"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {"method": METHOD_TEMPLATE_STORE_GET, "params": {"doc_type": "通知"}}
        )

        assert result == {"version": 1}

    async def test_template_get_by_id_allowed(self, tmp_path: Path, monkeypatch):
        self._patch_db(monkeypatch, rows=[SimpleNamespace(content={"version": 2})])
        proc = _make_process(permissions=["read:templates"], host_services=["template_store"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {
                "method": METHOD_TEMPLATE_STORE_GET,
                "params": {"doc_type": "通知", "template_id": 3},
            }
        )

        assert result == {"version": 2}

    async def test_template_get_not_found_returns_none(self, tmp_path: Path, monkeypatch):
        self._patch_db(monkeypatch, rows=[])
        proc = _make_process(permissions=["read:templates"], host_services=["template_store"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {"method": METHOD_TEMPLATE_STORE_GET, "params": {"doc_type": "通知"}}
        )

        assert result is None

    async def test_template_get_denied_without_permission(self, tmp_path: Path):
        proc = _make_process(permissions=[], host_services=["template_store"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {"method": METHOD_TEMPLATE_STORE_GET, "params": {"doc_type": "通知"}}
        )

        assert result["error"]["code"] == -32602

    async def test_template_get_denied_when_service_not_declared(self, tmp_path: Path):
        proc = _make_process(permissions=["read:templates"], host_services=[])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler(
            {"method": METHOD_TEMPLATE_STORE_GET, "params": {"doc_type": "通知"}}
        )

        assert result["error"]["code"] == -32602

    async def test_template_get_requires_doc_type(self, tmp_path: Path):
        proc = _make_process(permissions=["read:templates"], host_services=["template_store"])
        handler = self._manager(tmp_path)._create_host_request_handler(proc)

        result = await handler({"method": METHOD_TEMPLATE_STORE_GET, "params": {}})

        assert result["error"]["code"] == -32602
