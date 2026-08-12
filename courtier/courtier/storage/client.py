"""MinIO object storage client wrapper (singleton).

Used by the resource library to store original uploaded files.  All
operations are synchronous (the minio SDK is blocking) — call them from
async code via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from minio import Minio

logger = logging.getLogger(__name__)

_client: Minio | None = None
_lock = threading.Lock()


def _get_settings():
    """延迟导入 Settings，避免循环导入。"""
    from courtier.config import Settings

    return Settings()


def get_minio_client() -> Minio:
    """获取全局 MinIO 客户端单例（线程安全）。"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                settings = _get_settings()
                _client = Minio(
                    settings.minio_endpoint,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=settings.minio_secure,
                )
    return _client


def ensure_bucket(bucket: str) -> None:
    """确保 Bucket 存在（不存在则创建）。"""
    client = get_minio_client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def put_object(
    bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
) -> None:
    """上传字节数据到指定 Bucket。"""
    import io

    ensure_bucket(bucket)
    get_minio_client().put_object(
        bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
    )


def get_object(bucket: str, key: str) -> bytes:
    """下载对象字节。"""
    response = get_minio_client().get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def object_exists(bucket: str, key: str) -> bool:
    """判断对象是否存在（含 bucket 不存在的情况）。"""
    try:
        client = get_minio_client()
        if not client.bucket_exists(bucket):
            return False
        client.stat_object(bucket, key)
        return True
    except Exception:
        return False


def get_presigned_url(bucket: str, key: str, expires: int = 3600) -> str:
    """生成临时访问 URL（expires 单位：秒）。"""
    return get_minio_client().presigned_get_object(bucket, key, expires=timedelta(seconds=expires))


def remove_object(bucket: str, key: str) -> None:
    """删除对象。"""
    get_minio_client().remove_object(bucket, key)
