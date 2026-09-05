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
from minio.error import S3Error

logger = logging.getLogger(__name__)

_client: Minio | None = None
_lock = threading.Lock()


def _get_settings():
    """延迟导入，避免循环导入；读共享快照而非重建 Settings。"""
    from courtier.config import get_settings

    return get_settings()


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


def invalidate_minio_client() -> None:
    """Drop the singleton so the next get_minio_client() rebuilds from the
    current settings snapshot (connection hot-reload seam).  The minio SDK
    client holds no open resources — dropping the reference is enough."""
    global _client
    with _lock:
        _client = None


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


def fput_object(
    bucket: str, key: str, path: str, content_type: str = "application/octet-stream"
) -> None:
    """流式上传本地文件到指定 Bucket（大文件走磁盘，不进内存）。"""
    ensure_bucket(bucket)
    get_minio_client().fput_object(bucket, key, path, content_type=content_type)


def get_object(bucket: str, key: str) -> bytes:
    """下载对象字节。"""
    response = get_minio_client().get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def object_exists(bucket: str, key: str) -> bool:
    """判断对象是否存在（含 bucket 不存在的情况）。

    只有确定性的 404 回答 False；MinIO 宕机/鉴权/网络故障向上抛出——
    调用方必须把存储故障当作错误，而不是"对象不存在"。
    """
    try:
        client = get_minio_client()
        if not client.bucket_exists(bucket):
            return False
        client.stat_object(bucket, key)
        return True
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket"):
            return False
        raise


def get_presigned_url(bucket: str, key: str, expires: int = 3600) -> str:
    """生成临时访问 URL（expires 单位：秒）。"""
    return get_minio_client().presigned_get_object(bucket, key, expires=timedelta(seconds=expires))


def remove_object(bucket: str, key: str) -> None:
    """删除对象。"""
    get_minio_client().remove_object(bucket, key)
