"""Plugin-side file transfer over the shared MinIO transfer bucket.

Cross-machine deployments share no filesystem with the host, so
file-bearing tool arguments arrive as ``minio://bucket/key`` references:
the host PUTs the file to the transfer bucket before dispatch (rewriting
the original upload-dir path at the proxy boundary), and the plugin
downloads it on demand with its own restricted credentials via
:func:`resolve_file`.  Plugin-produced files go back the same way via
:func:`put_file`.

Configuration comes from the plugin's own environment:

- ``MINIO_ENDPOINT`` / ``MINIO_ACCESS_KEY`` / ``MINIO_SECRET_KEY`` /
  ``MINIO_SECURE`` — the restricted account (transfer bucket only).
- ``MINIO_BUCKET_PLUGIN_IO`` — transfer bucket name (default
  ``courtier-plugin-io``).
- ``COURTIER_PLUGIN_WORKDIR`` — base directory for downloads (default:
  system temp).  Files downloaded inside a ``tool.execute`` dispatch live
  in a per-request subdirectory that is removed when the call finishes.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MINIO_SCHEME = "minio://"

ENV_WORKDIR = "COURTIER_PLUGIN_WORKDIR"
ENV_BUCKET = "MINIO_BUCKET_PLUGIN_IO"
DEFAULT_BUCKET = "courtier-plugin-io"

_plugin_name = "plugin"
_request_workdir: ContextVar[Path | None] = ContextVar("courtier_plugin_workdir", default=None)

_client: Any = None
_base_workdir: Path | None = None
# Downloads made outside a tool.execute dispatch (tests, ad-hoc use) get
# their own dirs and are swept at process exit.
_adhoc_dirs: list[Path] = []


def configure(*, plugin_name: str) -> None:
    """Set the plugin name used as the ``out/<name>/`` key prefix."""
    global _plugin_name
    _plugin_name = plugin_name or "plugin"


def _minio_client() -> Any:
    """Lazy Minio client from the plugin environment (restricted account)."""
    global _client
    if _client is None:
        endpoint = os.environ.get("MINIO_ENDPOINT")
        access_key = os.environ.get("MINIO_ACCESS_KEY")
        secret_key = os.environ.get("MINIO_SECRET_KEY")
        missing = [
            name
            for name, value in (
                ("MINIO_ENDPOINT", endpoint),
                ("MINIO_ACCESS_KEY", access_key),
                ("MINIO_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "plugin file transfer requires environment variables: "
                + ", ".join(missing)
                + " (restricted transfer-bucket account)"
            )
        from minio import Minio  # lazy import — only needed when files move

        secure = os.environ.get("MINIO_SECURE", "").strip().lower() in ("1", "true", "yes", "on")
        _client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    return _client


def _bucket() -> str:
    return os.environ.get(ENV_BUCKET) or DEFAULT_BUCKET


def _base_dir() -> Path:
    global _base_workdir
    if _base_workdir is None:
        root = os.environ.get(ENV_WORKDIR)
        _base_workdir = (
            Path(root) if root else Path(tempfile.gettempdir()) / f"courtier-{_plugin_name}"
        )
        _base_workdir.mkdir(parents=True, exist_ok=True)
    return _base_workdir


def _sweep_adhoc() -> None:
    for d in _adhoc_dirs:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_sweep_adhoc)


@asynccontextmanager
async def request_workdir() -> AsyncIterator[Path]:
    """Per-request download dir, removed when the dispatch finishes.

    ``PluginRuntime._default_tool_execute`` wraps every tool call in this
    context; ``resolve_file`` drops downloads here.
    """
    workdir = _base_dir() / f"req-{uuid.uuid4().hex[:12]}"
    workdir.mkdir(parents=True, exist_ok=True)
    token = _request_workdir.set(workdir)
    try:
        yield workdir
    finally:
        _request_workdir.reset(token)
        shutil.rmtree(workdir, ignore_errors=True)


def _current_download_dir() -> Path:
    workdir = _request_workdir.get()
    if workdir is not None:
        return workdir
    workdir = _base_dir() / f"adhoc-{uuid.uuid4().hex[:12]}"
    workdir.mkdir(parents=True, exist_ok=True)
    _adhoc_dirs.append(workdir)
    return workdir


def parse_minio_ref(value: str) -> tuple[str, str]:
    """Split ``minio://bucket/key`` into ``(bucket, key)``."""
    rest = value[len(MINIO_SCHEME) :]
    bucket, sep, key = rest.partition("/")
    if not sep or not bucket or not key:
        raise ValueError(f"invalid minio reference: {value!r}")
    return bucket, key


async def resolve_file(value: str) -> str:
    """Resolve a tool argument to a local file path.

    ``minio://bucket/key`` references are downloaded into the current
    request workdir and the local path is returned.  Anything else (a
    plain local path from same-machine development) passes through
    unchanged.
    """
    if not isinstance(value, str) or not value.startswith(MINIO_SCHEME):
        return value
    bucket, key = parse_minio_ref(value)
    # Defense in depth: the restricted plugin account only has the transfer
    # bucket anyway, but refuse early so a misconfigured IAM policy cannot
    # silently widen what plugins may read.
    if bucket != _bucket():
        raise ValueError(
            f"minio reference bucket {bucket!r} is not the transfer bucket"
        )
    client = _minio_client()

    download_dir = _current_download_dir()
    filename = Path(key).name or "download.bin"
    target = download_dir / filename
    counter = 1
    while target.exists():
        target = download_dir / f"{counter}-{filename}"
        counter += 1

    await asyncio.to_thread(_download, client, bucket, key, target)
    logger.info("Downloaded %s%s/%s -> %s", MINIO_SCHEME, bucket, key, target)
    return str(target)


def _download(client: Any, bucket: str, key: str, target: Path) -> None:
    resp = client.get_object(bucket, key)
    try:
        with open(target, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    finally:
        resp.close()
        resp.release_conn()


async def put_file(
    local_path: str | Path,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """Upload a plugin-produced file to the transfer bucket.

    Returns the ``minio://bucket/key`` reference; the host reads the
    object with its own credentials and mints frontend download URLs via
    the ``storage.presign_get`` host service.
    """
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(f"put_file: not a regular file: {path}")
    name = filename or path.name
    bucket = _bucket()
    key = f"out/{_plugin_name}/{uuid.uuid4().hex}/{name}"
    ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    client = _minio_client()
    await asyncio.to_thread(_upload, client, bucket, key, path, ctype)
    ref = f"{MINIO_SCHEME}{bucket}/{key}"
    logger.info("Uploaded %s -> %s", path, ref)
    return ref


def _upload(client: Any, bucket: str, key: str, path: Path, content_type: str) -> None:
    client.fput_object(bucket, key, str(path), content_type=content_type)
