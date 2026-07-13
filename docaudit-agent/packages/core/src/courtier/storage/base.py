from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO


@dataclass(frozen=True)
class ObjectInfo:
    name: str
    size: int
    content_type: str
    bucket: str


class StorageBackend(ABC):
    @abstractmethod
    async def save(
        self, bucket: str, key: str, data: bytes | BytesIO, content_type: str = ""
    ) -> None:
        pass

    @abstractmethod
    async def get(self, bucket: str, key: str) -> bytes:
        pass

    @abstractmethod
    async def delete(self, bucket: str, key: str) -> None:
        pass

    @abstractmethod
    async def presigned_url(
        self, bucket: str, key: str, expires: int = 3600, inline: bool = False
    ) -> str:
        pass

    @abstractmethod
    async def exists(self, bucket: str, key: str) -> bool:
        pass

    @abstractmethod
    async def exists_bucket(self, bucket: str) -> bool:
        pass

    @abstractmethod
    async def create_bucket(self, bucket: str) -> None:
        pass
