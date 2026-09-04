"""FileStore — maps opaque fileId strings to filesystem paths."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileInfo:
    file_id: str
    original_name: str
    stored_path: str  # filesystem path relative to upload_dir
    size_bytes: int
    owner: str = ""  # uploader username; empty for legacy records
    # Attachment kind ("document"/"image"/"audio"/"video"); "" for legacy
    # records — consumers fall back to extension-based inference.
    kind: str = ""
    # Media probe metadata (media plugin); None for documents/legacy records.
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


class FileStore:
    """Thread-safe file-id-to-path registry with JSON file persistence.

    Each uploaded file gets a short opaque id (file_xxxxxxxx) that
    the frontend uses as a reference.  The mapping is stored on disk
    so it survives restarts.
    """

    def __init__(self, storage_dir: str) -> None:
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "file_index.json"
        self._files: dict[str, FileInfo] = {}
        # asyncio.Lock protects in-memory dict operations.  File I/O
        # (_save_index / _load_index) is outside the lock.
        self._lock = asyncio.Lock()
        self._load_index()

    async def register(
        self,
        original_name: str,
        stored_path: str,
        size_bytes: int,
        owner: str = "",
        kind: str = "",
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> FileInfo:
        file_id = f"file_{secrets.token_hex(16)}"
        info = FileInfo(
            file_id=file_id,
            original_name=original_name,
            stored_path=stored_path,
            size_bytes=size_bytes,
            owner=owner,
            kind=kind,
            duration_seconds=duration_seconds,
            width=width,
            height=height,
        )
        async with self._lock:
            self._files[file_id] = info
            await self._save_index()
        return info

    async def resolve(self, file_id: str) -> FileInfo | None:
        async with self._lock:
            return self._files.get(file_id)

    @staticmethod
    def _safe_resolve(upload_dir: Path, stored_path: str) -> Path | None:
        """Resolve *stored_path* relative to *upload_dir*, rejecting escapes."""
        if not stored_path:
            return None
        p = Path(stored_path)
        if p.is_absolute():
            return None
        base = Path(upload_dir).resolve()
        target = (base / p).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target

    async def resolve_path(self, file_id: str, upload_dir: str) -> Path | None:
        """Resolve a fileId to an absolute filesystem path."""
        info = await self.resolve(file_id)
        if info is None:
            return None
        return self._safe_resolve(Path(upload_dir), info.stored_path)

    async def authorized_path(
        self,
        file_id: str,
        upload_dir: str,
        user: str,
        is_admin: bool,
    ) -> Path:
        """Resolve a fileId and verify ownership, returning the absolute path.

        Shared authorization semantics for every route handing a stored
        upload back to a client: 404 for unknown/deleted files, 403 for
        non-owners (legacy records with an empty owner stay readable by any
        logged-in user), admins always pass.
        """
        from fastapi import HTTPException

        info = await self.resolve(file_id)
        if info is None:
            raise HTTPException(404, f"文件不存在: {file_id}")
        if not is_admin and info.owner and info.owner != user:
            raise HTTPException(403, "无权访问该文件")
        path = await self.resolve_path(file_id, upload_dir)
        if path is None or not path.exists():
            raise HTTPException(404, f"文件不存在: {file_id}")
        return path

    async def delete_owned(self, owner: str, upload_dir: str) -> list[str]:
        """Remove every registry entry uploaded by *owner* and unlink its
        stored file (account-deletion cascade).  Returns the removed
        file_ids.  Entries with an empty owner (legacy) are never touched.
        """
        removed: list[str] = []
        async with self._lock:
            for fid, info in list(self._files.items()):
                if info.owner != owner:
                    continue
                self._files.pop(fid, None)
                removed.append(fid)
                try:
                    path = self._safe_resolve(upload_dir, info.stored_path)
                    if path is not None and path.is_file():
                        await asyncio.to_thread(path.unlink)
                except OSError:
                    logger.warning(
                        "Failed to unlink uploaded file %s", info.stored_path, exc_info=True
                    )
            if removed:
                await self._save_index()
        return removed

    # -- internal -----------------------------------------------------------

    async def _save_index(self) -> None:
        data = {
            fid: {
                "original_name": fi.original_name,
                "stored_path": fi.stored_path,
                "size_bytes": fi.size_bytes,
                "owner": fi.owner,
                "kind": fi.kind,
                "duration_seconds": fi.duration_seconds,
                "width": fi.width,
                "height": fi.height,
            }
            for fid, fi in self._files.items()
        }
        try:
            await asyncio.to_thread(
                self._index_path.write_text,
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save file index")

    def _load_index(self) -> None:
        if not self._index_path.exists():
            return
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to load file index")
            return
        for fid, d in raw.items():
            self._files[fid] = FileInfo(
                file_id=fid,
                original_name=d.get("original_name", ""),
                stored_path=d.get("stored_path", ""),
                size_bytes=d.get("size_bytes", 0),
                owner=d.get("owner", ""),
                kind=d.get("kind", ""),
                duration_seconds=d.get("duration_seconds"),
                width=d.get("width"),
                height=d.get("height"),
            )
