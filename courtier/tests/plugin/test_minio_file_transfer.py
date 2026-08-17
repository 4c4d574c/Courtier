"""End-to-end MinIO file transfer: host rewrite → plugin download → plugin upload.

Runs against the compose MinIO (localhost:9000) with the restricted
``courtier-plugins`` account provisioned by scripts/minio_plugin_io.py.
Marked ``integration`` — excluded from the default suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from courtier_plugin_sdk import HostStorage, PluginRuntime, ToolResult, put_file, resolve_file
from courtier_plugin_sdk.files import parse_minio_ref

from courtier.agent.tools.registry import ToolRegistry
from courtier.config import Settings
from courtier.plugin import PluginSystem
from courtier.plugin.manager import PluginState

TOKEN = "minio-e2e-token"
# Dev-only restricted account created by scripts/minio_plugin_io.py; scope:
# the transfer bucket only, on the local compose MinIO.
PLUGIN_MINIO_SECRET = "dev-plugin-secret-7f3a9c2e"

pytestmark = pytest.mark.integration


def _minio_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 9000), timeout=2):
            return True
    except OSError:
        return False


class _FilePlugin(PluginRuntime):
    def _setup_handlers(self):
        runtime = self

        class FetchTool:
            name = "fetch"
            display_name = "Fetch"
            description = "download a file-ref and report content"
            parameters = {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "file to fetch",
                        "format": "file-ref",
                    }
                },
            }

            async def execute(self, file_path: str = ""):
                local = await resolve_file(file_path)
                data = Path(local).read_bytes()
                return ToolResult(success=True, data={"size": len(data), "head": repr(data[:16])})

        class StoreTool:
            name = "store"
            display_name = "Store"
            description = "upload bytes and mint a download URL via the host"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                with tempfile.NamedTemporaryFile(
                    suffix="_report.bin", prefix="e2e_", delete=False
                ) as tmp:
                    tmp.write(b"plugin-output-bytes")
                    tmp_path = Path(tmp.name)
                try:
                    ref = await put_file(tmp_path, filename="report.bin")
                finally:
                    tmp_path.unlink(missing_ok=True)
                bucket, key = parse_minio_ref(ref)
                client = runtime.host_service_client
                receipt = await HostStorage(client).presign_get(bucket, key)
                return ToolResult(
                    success=True,
                    data={"download_url": receipt["download_url"], "object_key": key},
                )

        self.register_tool(FetchTool())
        self.register_tool(StoreTool())


@pytest.fixture
async def file_plugin_server(monkeypatch):
    """SDK plugin server with restricted MinIO credentials in its env."""
    settings = Settings()
    monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", TOKEN)
    monkeypatch.setenv("MINIO_ENDPOINT", settings.minio_endpoint)
    monkeypatch.setenv("MINIO_ACCESS_KEY", "courtier-plugins")
    monkeypatch.setenv("MINIO_SECRET_KEY", PLUGIN_MINIO_SECRET)
    monkeypatch.setenv("MINIO_SECURE", "true" if settings.minio_secure else "false")
    # Reset the SDK's lazy client so the env above takes effect.
    from courtier_plugin_sdk import files

    monkeypatch.setattr(files, "_client", None)
    monkeypatch.setattr(files, "_base_workdir", None)

    runtime = _FilePlugin()
    task = asyncio.create_task(runtime.serve("127.0.0.1:0"))
    for _ in range(200):
        if runtime._server is not None and runtime._server.sockets:
            break
        await asyncio.sleep(0.01)
    port = runtime._server.sockets[0].getsockname()[1]
    yield port
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=5)


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins" / "file_plugin"
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(
        """name: file_plugin
version: "0.1.0"
api: "2.0"
description: "MinIO transfer e2e fixture"

dependencies:
  host_services: [storage]
  permissions: [read:storage, write:storage]
""",
        encoding="utf-8",
    )
    return d.parent


async def _wait_active(ps: PluginSystem, name: str, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state = ps.get_status().get(name, {}).get("state")
        if state == PluginState.ACTIVE.value:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{name} not ACTIVE (last={state})")
        await asyncio.sleep(0.05)


class TestMinioFileTransfer:
    @pytest.mark.asyncio
    async def test_upload_dir_file_reaches_plugin(
        self, plugins_dir, file_plugin_server, tmp_path, monkeypatch
    ):
        if not _minio_up():
            pytest.skip("compose MinIO not reachable")
        upload_dir = tmp_path / "uploads"
        doc = upload_dir / "sess-e2e" / "样例.docx"
        doc.parent.mkdir(parents=True)
        payload = b"fake-docx-content" * 100
        doc.write_bytes(payload)

        monkeypatch.setattr(
            "courtier.config.get_settings",
            lambda: SimpleNamespace(
                upload_dir=str(upload_dir), minio_bucket_plugin_io="courtier-plugin-io"
            ),
        )

        ps = PluginSystem(
            plugins_dir=plugins_dir,
            tool_registry=ToolRegistry(),
            endpoints={"file_plugin": ("127.0.0.1", file_plugin_server)},
            token=TOKEN,
        )
        await ps.start()
        try:
            await _wait_active(ps, "file_plugin")
            registry = ps._manager._extension_registry._tool_registry
            result = await registry.get("fetch").execute(
                on_progress=lambda *_: None, file_path=str(doc)
            )
            assert result.success is True, result.error
            assert result.data["size"] == len(payload)
        finally:
            await ps.shutdown()

    @pytest.mark.asyncio
    async def test_plugin_output_presigned_by_host(
        self, plugins_dir, file_plugin_server, tmp_path, monkeypatch
    ):
        if not _minio_up():
            pytest.skip("compose MinIO not reachable")
        monkeypatch.setattr(
            "courtier.config.get_settings",
            lambda: SimpleNamespace(
                upload_dir=str(tmp_path), minio_bucket_plugin_io="courtier-plugin-io"
            ),
        )

        ps = PluginSystem(
            plugins_dir=plugins_dir,
            tool_registry=ToolRegistry(),
            endpoints={"file_plugin": ("127.0.0.1", file_plugin_server)},
            token=TOKEN,
        )
        await ps.start()
        try:
            await _wait_active(ps, "file_plugin")
            registry = ps._manager._extension_registry._tool_registry
            result = await registry.get("store").execute(on_progress=lambda *_: None)
            assert result.success is True, result.error
            url = result.data["download_url"]
            assert "courtier-plugin-io" in url
            assert result.data["object_key"].startswith("out/")

            # The presigned URL actually serves the uploaded bytes.
            import urllib.request

            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.read() == b"plugin-output-bytes"
        finally:
            await ps.shutdown()
