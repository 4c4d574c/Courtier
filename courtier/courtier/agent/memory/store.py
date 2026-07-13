"""MemoryStore — file-based cross-session knowledge storage."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)


def _json_default(obj: object) -> object:
    """Raise TypeError for non-JSON-serializable types instead of silently corrupting."""
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


class MemoryStore(Protocol):
    """跨会话记忆——只有不能从当前工作重新推导的知识才值得进入 memory."""

    async def get(self, key: str, namespace: str = "session") -> Any | None: ...
    async def set(self, key: str, value: Any, namespace: str = "session") -> None: ...
    async def delete(self, key: str, namespace: str = "session") -> None: ...
    async def list_keys(self, namespace: str = "session") -> list[str]: ...
    async def clear_namespace(self, namespace: str = "session") -> None: ...


class FileMemoryStore(MemoryStore):
    """File-system backed memory: each key is a JSON file under <root>/<namespace>/<key>.json."""

    def __init__(self, root_dir: str = ".agent_memory") -> None:
        self._root_dir = Path(root_dir).resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._dirs_created: set[str] = {str(self._root_dir)}
        self._lock = asyncio.Lock()

    @staticmethod
    def _sanitize(name: str) -> str:
        """Encode a namespace/key into a safe, reversible file path segment.

        Percent-encoding keeps every distinct (namespace, key) pair unique,
        avoiding the collisions caused by the old regex-based replacement.
        """
        return quote(name, safe="")

    def _ns_dir(self, namespace: str) -> Path:
        d = self._root_dir / self._sanitize(namespace)
        d_str = str(d)
        if d_str not in self._dirs_created:
            d.mkdir(parents=True, exist_ok=True)
            self._dirs_created.add(d_str)
        return d

    def _key_path(self, key: str, namespace: str) -> Path:
        return self._ns_dir(namespace) / f"{self._sanitize(key)}.json"

    async def get(self, key: str, namespace: str = "session") -> Any | None:
        async with self._lock:
            path = self._key_path(key, namespace)
            exists = await asyncio.to_thread(path.exists)
            if not exists:
                return None
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                return json.loads(text)
            except json.JSONDecodeError:
                logger.error("Corrupted memory file: %s", path)
                return None
            except OSError:
                logger.exception("Cannot read memory file: %s", path)
                return None

    async def set(self, key: str, value: Any, namespace: str = "session") -> None:
        async with self._lock:
            path = self._key_path(key, namespace)
            text = json.dumps(value, ensure_ascii=False, default=_json_default)
            tmp_path = path.with_suffix(".json.tmp")
            await asyncio.to_thread(tmp_path.write_text, text, encoding="utf-8")
            await asyncio.to_thread(tmp_path.rename, path)

    async def delete(self, key: str, namespace: str = "session") -> None:
        async with self._lock:
            path = self._key_path(key, namespace)
            exists = await asyncio.to_thread(path.exists)
            if exists:
                await asyncio.to_thread(path.unlink)

    async def list_keys(self, namespace: str = "session") -> list[str]:
        async with self._lock:
            ns_dir = self._ns_dir(namespace)

            def _list() -> list[str]:
                return [unquote(p.stem) for p in ns_dir.glob("*.json") if p.is_file()]

            return await asyncio.to_thread(_list)

    async def clear_namespace(self, namespace: str = "session") -> None:
        async with self._lock:
            ns_dir = self._ns_dir(namespace)
            ns_dir_str = str(ns_dir)

            def _clear() -> None:
                for p in ns_dir.glob("*.json"):
                    if p.is_file():
                        p.unlink()
                # Remove empty directory after clearing all files
                try:
                    remaining = list(ns_dir.iterdir())
                    if not remaining:
                        ns_dir.rmdir()
                except OSError:
                    pass

            await asyncio.to_thread(_clear)
            # Remove from cache so it will be recreated if needed
            self._dirs_created.discard(ns_dir_str)
