import asyncio
from io import BytesIO
from pathlib import Path

from courtier.config import Settings

from .base import ObjectInfo, StorageBackend
from .cache import FileCache
from .minio_backend import MinioStorage

__all__ = [
    "ObjectInfo",
    "StorageBackend",
    "MinioStorage",
    "FileCache",
    "get_storage",
    "get_cache",
    "get_cached_file",
]

_storage: StorageBackend | None = None
_cache: FileCache | None = None
_storage_async_lock = asyncio.Lock()
_cache_async_lock = asyncio.Lock()


def _make_storage() -> StorageBackend:
    """Create a new MinioStorage from settings."""
    settings = Settings()
    return MinioStorage(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _make_cache() -> FileCache:
    """Create a new FileCache from settings."""
    settings = Settings()
    return FileCache(settings.cache_dir)


def get_storage() -> StorageBackend:
    """Non-async version for sync contexts. Not safe for concurrent first-use in threads."""
    global _storage
    if _storage is None:
        _storage = _make_storage()
    return _storage


def get_cache() -> FileCache:
    """Non-async version for sync contexts. Not safe for concurrent first-use in threads."""
    global _cache
    if _cache is None:
        _cache = _make_cache()
    return _cache


async def get_storage_async() -> StorageBackend:
    global _storage
    async with _storage_async_lock:
        if _storage is None:
            _storage = _make_storage()
    return _storage


async def get_cache_async() -> FileCache:
    global _cache
    async with _cache_async_lock:
        if _cache is None:
            _cache = _make_cache()
    return _cache


async def get_cached_file(
    bucket: str, key: str, stream: bool = False
) -> BytesIO | Path:
    cache = await get_cache_async()
    cached = cache.get(key)
    if cached:
        if stream:
            return BytesIO(cached.read_bytes())
        return cached
    storage = await get_storage_async()
    data = await storage.get(bucket, key)
    path = cache.put(key, data)
    if stream:
        return BytesIO(data)
    return path
