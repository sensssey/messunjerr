"""Хранилище в памяти: подставка для тестов и для приложения без настроенного S3.

`InMemoryObjectStorage` ведёт себя как S3 в том, что видят команды: `HEAD`, чтение начала, удаление
без ошибки для отсутствующего ключа. «Клиентскую» загрузку по presigned-ссылке имитирует метод `put`.
`UnconfiguredStorage` стоит, пока `S3_*` не заданы (разработка без хранилища, часть тестов): любое
обращение к нему отвечает «хранилище недоступно».
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from messunjerr.core.clock import utcnow
from messunjerr.media.domain.ports import (
    ListedObject,
    PresignedUpload,
    StorageUnavailableError,
    StoredObject,
)


@dataclass(frozen=True, slots=True)
class PresignedRequest:
    key: str
    content_type: str
    content_length: int
    expires_in: int


@dataclass(slots=True)
class InMemoryObjectStorage:
    base_url: str = "http://storage.test"
    bucket: str = "media"
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict[str, tuple[bytes, str]])
    presigned: list[PresignedRequest] = field(default_factory=list[PresignedRequest])
    deleted: list[str] = field(default_factory=list[str])
    modified: dict[str, datetime] = field(default_factory=dict[str, datetime])
    """Когда записан каждый объект; тесты состаривают объекты, подменяя значение."""
    page_size: int = 1000
    unavailable: bool = False
    """Если истина, любое обращение отвечает `StorageUnavailableError` (имитация сбоя хранилища)."""

    def _check(self) -> None:
        if self.unavailable:
            raise StorageUnavailableError("in-memory storage is switched off")

    def put(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        """Загрузка клиентом по presigned-ссылке (в тестах вместо настоящего `PUT`)."""
        self.objects[key] = (body, content_type)
        self.modified[key] = utcnow()

    def put_once(
        self, key: str, body: bytes, content_type: str = "application/octet-stream"
    ) -> bool:
        """`PUT` с `If-None-Match: *`, как в выданной ссылке: `False` (в хранилище `412`), если объект уже есть."""
        if key in self.objects:
            return False
        self.put(key, body, content_type)
        return True

    async def presign_put(
        self, *, key: str, content_type: str, content_length: int, expires_in: int
    ) -> PresignedUpload:
        self._check()
        self.presigned.append(PresignedRequest(key, content_type, content_length, expires_in))
        return PresignedUpload(
            url=f"{self.base_url}/{self.bucket}/{quote(key)}?X-Amz-Expires={expires_in}&X-Amz-Signature=test",
            headers={"Content-Type": content_type, "If-None-Match": "*"},
        )

    async def head(self, key: str) -> StoredObject | None:
        self._check()
        stored = self.objects.get(key)
        return None if stored is None else StoredObject(len(stored[0]), "etag", stored[1])

    async def read_head(self, key: str, length: int) -> bytes | None:
        self._check()
        stored = self.objects.get(key)
        return None if stored is None else stored[0][:length]

    async def delete_many(self, keys: Sequence[str]) -> None:
        self._check()
        for key in keys:
            self.objects.pop(key, None)
            self.modified.pop(key, None)
            self.deleted.append(key)

    async def list_objects(self, prefix: str) -> AsyncIterator[list[ListedObject]]:
        self._check()
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        for start in range(0, len(keys), self.page_size):
            self._check()
            yield [
                ListedObject(key, len(self.objects[key][0]), self.modified[key])
                for key in keys[start : start + self.page_size]
            ]

    async def warm_up(self) -> None:
        return None

    async def close(self) -> None:
        return None


class UnconfiguredStorage:
    """Заглушка без S3: приложение стартует, а ручки загрузки отвечают `503`."""

    async def presign_put(
        self, *, key: str, content_type: str, content_length: int, expires_in: int
    ) -> PresignedUpload:
        raise StorageUnavailableError("S3 is not configured (S3_ENDPOINT_INTERNAL, S3_ACCESS_KEY)")

    async def head(self, key: str) -> StoredObject | None:
        raise StorageUnavailableError("S3 is not configured")

    async def read_head(self, key: str, length: int) -> bytes | None:
        raise StorageUnavailableError("S3 is not configured")

    async def delete_many(self, keys: Sequence[str]) -> None:
        raise StorageUnavailableError("S3 is not configured")

    async def list_objects(self, prefix: str) -> AsyncIterator[list[ListedObject]]:
        raise StorageUnavailableError("S3 is not configured")
        yield []

    async def warm_up(self) -> None:
        return None

    async def close(self) -> None:
        return None
