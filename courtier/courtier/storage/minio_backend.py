import asyncio
from datetime import timedelta
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from .base import StorageBackend


class MinioStorage(StorageBackend):
    def __init__(
        self, endpoint: str, access_key: str, secret_key: str, secure: bool = False
    ):
        self._endpoint = endpoint
        self._secure = secure
        self._client = Minio(endpoint, access_key, secret_key, secure=secure)

    async def save(
        self, bucket: str, key: str, data: bytes | BytesIO, content_type: str = ""
    ) -> None:
        await asyncio.to_thread(self._save_sync, bucket, key, data, content_type)

    def _save_sync(
        self, bucket: str, key: str, data: bytes | BytesIO, content_type: str
    ) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)
        if isinstance(data, bytes):
            data = BytesIO(data)
        data.seek(0, 2)
        size = data.tell()
        data.seek(0)
        self._client.put_object(bucket, key, data, size, content_type=content_type)

    async def get(self, bucket: str, key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, bucket, key)

    def _get_sync(self, bucket: str, key: str) -> bytes:
        resp = self._client.get_object(bucket, key)
        try:
            return resp.data
        finally:
            resp.close()
            resp.release_conn()

    async def delete(self, bucket: str, key: str) -> None:
        await asyncio.to_thread(self._client.remove_object, bucket, key)

    async def presigned_url(
        self, bucket: str, key: str, expires: int = 3600, inline: bool = False
    ) -> str:
        extra_params: dict[str, str | list[str] | tuple[str]] = {}
        if inline:
            extra_params["response-content-disposition"] = "inline"
        return await asyncio.to_thread(
            self._client.presigned_get_object,
            bucket,
            key,
            timedelta(seconds=expires),
            extra_query_params=extra_params if extra_params else None,
        )

    async def exists(self, bucket: str, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.stat_object, bucket, key)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            raise

    async def exists_bucket(self, bucket: str) -> bool:
        return await asyncio.to_thread(self._client.bucket_exists, bucket)

    async def create_bucket(self, bucket: str) -> None:
        if not await self.exists_bucket(bucket):
            await asyncio.to_thread(self._client.make_bucket, bucket)
