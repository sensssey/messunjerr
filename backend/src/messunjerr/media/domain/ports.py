"""Порты медиа: объектное хранилище и «к чему ресурс привязан» (4.2, 4.11).

Хранилищем в проде служит S3 API (SeaweedFS на стенде, S3 провайдера на сервере); команды знают только
этот порт, поэтому их проверяют на подставном хранилище в памяти. К чему привязан ресурс (аватар
профиля, вложение поста или сообщения), знают контексты выше или ниже медиа; каждый из них реализует
`AssetUsage`, а корень приложения собирает их в один (`messunjerr.main`).
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class StorageUnavailableError(Exception):
    """Хранилище не ответило или ответило сбоем (5xx, обрыв, таймаут): операцию можно повторить."""


class ObjectTooLargeError(Exception):
    """Объект больше, чем разрешено читать целиком: повторять нечего, размер не изменится."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    size: int
    etag: str | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ListedObject:
    key: str
    size: int
    modified_at: datetime
    """Когда объект записан (с часовым поясом)."""


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """Ссылка на загрузку и заголовки, которые подпись требует от клиента."""

    url: str
    headers: dict[str, str]
    """`Content-Type` и `If-None-Match: *`; `Content-Length` клиент (браузер) ставит сам."""


class ObjectStorage(Protocol):
    async def presign_put(
        self, *, key: str, content_type: str, content_length: int, expires_in: int
    ) -> PresignedUpload:
        """Ссылка для `PUT` в хранилище. Подпись закрепляет `Content-Type` и точный `Content-Length`.

        Запись условная (`If-None-Match: *`): объект создаётся один раз, повторный `PUT` по той же
        ссылке получает `412`. Иначе клиент мог бы подменить содержимое после проверки (завершение
        загрузки смотрит на размер, обработка на сигнатуру).

        Считается локально, без обращения к сети. Хост ссылки это публичный адрес хранилища: подпись
        SigV4 включает `Host`, поэтому браузер должен прийти именно на него.
        """
        ...

    async def head(self, key: str) -> StoredObject | None:
        """Размер и метаданные объекта; `None`, если объекта нет."""
        ...

    async def read_head(self, key: str, length: int) -> bytes | None:
        """Первые `length` байт объекта; `None`, если объекта нет."""
        ...

    async def read_object(self, key: str, max_bytes: int) -> bytes | None:
        """Объект целиком; `None`, если его нет. Больше `max_bytes`: `ObjectTooLargeError`.

        Для обработки изображений: размер загрузки уже сверен с заявленным, а предел защищает
        память воркера, если в bucket окажется что-то другое.
        """
        ...

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        """Записывает объект (поверх существующего). Так пишут воркеры: варианты изображений и
        пустую заглушку на месте оригинала. Клиентские загрузки идут по `presign_put`."""
        ...

    async def presign_get(
        self,
        *,
        key: str,
        expires_in: int,
        content_type: str | None = None,
        content_disposition: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        """Ссылка для `GET`. Заголовки ответа задаёт сама ссылка (`response-content-*`), а не
        метаданные объекта: им верить нельзя (4.11, неподписанные заголовки). Считается локально."""
        ...

    async def delete_many(self, keys: Sequence[str]) -> None:
        """Удаляет объекты; отсутствующие не ошибка (повтор безопасен)."""
        ...

    def list_objects(self, prefix: str) -> AsyncIterator[list[ListedObject]]:
        """Объекты с префиксом страницами (до тысячи в странице) в порядке ключей."""
        ...

    async def warm_up(self) -> None:
        """Готовит клиента к работе при старте процесса, без сетевых обращений.

        Первое обращение к клиенту S3 синхронно читает описание сервиса (сотни миллисекунд): это не
        должно случиться посреди запроса человека, под транзакцией и блокировкой квоты.
        """
        ...

    async def close(self) -> None: ...


class AssetUsage(Protocol):
    async def is_attached(self, session: AsyncSession, asset_id: uuid.UUID) -> bool:
        """Привязан ли ресурс к аватару, посту или сообщению: такой удалять нельзя (`asset_in_use`)."""
        ...


class AssetAudience(Protocol):
    async def can_view(
        self, session: AsyncSession, *, asset_id: uuid.UUID, viewer_id: uuid.UUID
    ) -> bool:
        """Вправе ли зритель (не владелец) получить ссылки на ресурс, то есть видеть объект, к
        которому тот привязан: видимый пост, беседу, где зритель участник (5.8, `/urls`).

        Каждый контекст отвечает за свои привязки (посты S11, сообщения S14); пока их нет, ссылки
        получает только владелец.
        """
        ...
