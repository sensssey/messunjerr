"""Клиент S3 на aiobotocore (4.11): presigned PUT, HEAD, начало объекта, удаление.

Два клиента на один набор ключей, различаются только адресом:

- внутренний (`S3_ENDPOINT_INTERNAL`, `http://seaweedfs:8333`) ходит по сети: HEAD, чтение, удаление;
- публичный (`S3_ENDPOINT_PUBLIC`, по умолчанию адрес сайта) лишь подписывает ссылки. Подпись SigV4
  считается локально и включает `Host`, поэтому ссылка должна быть выписана на тот адрес, с которого к
  хранилищу придёт браузер (на стенде это `https://messunjerr.localhost/media/…`: Caddy проксирует
  путь в SeaweedFS без переписывания, S4-05).

Настройки контрольных сумм `when_required` обязательны (итоги S4, п. 1): по умолчанию aiobotocore
по HTTPS отправляет тело как `aws-chunked`, и SeaweedFS сохраняет у объекта `Content-Encoding:
aws-chunked`, который браузер не распакует. Клиенты создаются лениво, при первом обращении, в цикле
событий, который их использует, и закрываются в `close()`.

У aiobotocore нет собственных аннотаций: типы клиента берутся из `types-aiobotocore-s3` (только для
pyright), а непротипизированные места библиотеки собраны в этом модуле.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from messunjerr.core.logs import get_logger
from messunjerr.media.domain.ports import (
    ListedObject,
    ObjectTooLargeError,
    PresignedUpload,
    StorageUnavailableError,
    StoredObject,
)

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_DELETE_CONCURRENCY = 8
_NETWORK_ERRORS = (BotoCoreError, aiohttp.ClientError, OSError, TimeoutError)


@dataclass(frozen=True, slots=True)
class Deadlines:
    """Сколько секунд на всю операцию с хранилищем, включая повторы клиента и разрешение имени.

    Тайм-ауты соединения и чтения у клиента относятся к одной попытке и не ограничивают целое: при
    остановленном хранилище разрешение имени в сети Docker и две попытки давали ответ API через
    двадцать с лишним секунд. Запрос человека должен кончаться `503` за считаные секунды.
    """

    head: float = 8.0
    read: float = 20.0
    delete: float = 60.0
    transfer: float = 60.0
    """Объект целиком (до 10 МиБ у изображений): чтение для обработки и запись вариантов."""


def is_missing_object(error: ClientError) -> bool:
    """Объекта нет. `NoSuchBucket` сюда не входит: это ошибка настройки, а не пустое место (ответ 404
    на `HEAD` без тела приходит с кодом «404» и от отсутствующего ключа неотличим)."""
    response = cast("dict[str, Any]", error.response)
    return str(response.get("Error", {}).get("Code", "")) in _MISSING_CODES


def is_unsatisfiable_range(error: ClientError) -> bool:
    """Просили диапазон у пустого объекта: S3 отвечает `416 InvalidRange` (проверено на SeaweedFS).

    Пустые объекты у нас бывают: на месте оригинала обработанного изображения лежит пустая заглушка.
    """
    response = cast("dict[str, Any]", error.response)
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code == "InvalidRange" or status == 416


class S3ObjectStorage:
    def __init__(
        self,
        *,
        internal_endpoint: str,
        public_endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        deadlines: Deadlines | None = None,
        page_size: int = 1000,
    ) -> None:
        self._internal_endpoint = internal_endpoint
        self._public_endpoint = public_endpoint
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._deadlines = deadlines or Deadlines()
        self._page_size = page_size
        self._session = get_session()
        self._stack: AsyncExitStack | None = None
        self._clients_pair: tuple[S3Client, S3Client] | None = None
        self._lock = asyncio.Lock()
        self._log = get_logger("messunjerr.media.s3")

    def _config(self) -> AioConfig:
        return AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
            max_pool_connections=20,
        )

    async def _clients(self) -> "tuple[S3Client, S3Client]":
        """Внутренний и публичный клиенты; создаются при первом обращении."""
        if self._clients_pair is not None:
            return self._clients_pair
        async with self._lock:
            if self._clients_pair is None:
                stack = AsyncExitStack()
                options: dict[str, Any] = {
                    "aws_access_key_id": self._access_key,
                    "aws_secret_access_key": self._secret_key,
                    "region_name": self._region,
                    "config": self._config(),
                }
                internal = await stack.enter_async_context(
                    self._session.create_client(
                        "s3", endpoint_url=self._internal_endpoint, **options
                    )
                )
                public = await stack.enter_async_context(
                    self._session.create_client("s3", endpoint_url=self._public_endpoint, **options)
                )
                self._stack = stack
                self._clients_pair = (cast("S3Client", internal), cast("S3Client", public))
            return self._clients_pair

    def _unavailable(self, action: str, error: Exception) -> StorageUnavailableError:
        # Ссылки, ключи доступа и тела ответов в журнал не пишем: только действие и тип ошибки.
        self._log.warning("storage_failed", action=action, error_type=type(error).__name__)
        return StorageUnavailableError(f"{action}: {type(error).__name__}")

    async def list_objects(self, prefix: str) -> AsyncIterator[list[ListedObject]]:
        internal, _ = await self._clients()
        token: str | None = None
        while True:
            options: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": self._page_size,
            }
            if token is not None:
                options["ContinuationToken"] = token
            try:
                async with asyncio.timeout(self._deadlines.read):
                    page = await internal.list_objects_v2(**options)
            except ClientError as error:
                raise self._unavailable("list", error) from error
            except _NETWORK_ERRORS as error:
                raise self._unavailable("list", error) from error
            objects: list[ListedObject] = []
            for item in page.get("Contents", []):
                key, modified = item.get("Key"), item.get("LastModified")
                if key is not None and modified is not None:
                    objects.append(
                        ListedObject(key=key, size=int(item.get("Size", 0)), modified_at=modified)
                    )
            if objects:
                yield objects
            if not page.get("IsTruncated"):
                return
            token = str(page["NextContinuationToken"])

    async def warm_up(self) -> None:
        await self._clients()

    async def presign_put(
        self, *, key: str, content_type: str, content_length: int, expires_in: int
    ) -> PresignedUpload:
        _, public = await self._clients()
        try:
            url = await public.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "ContentLength": content_length,
                    "IfNoneMatch": "*",  # запись один раз: второй PUT получает 412 (проверено на SeaweedFS 4.47)
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
            return PresignedUpload(
                url=url, headers={"Content-Type": content_type, "If-None-Match": "*"}
            )
        except (
            _NETWORK_ERRORS
        ) as error:  # подпись локальная; сюда попадёт лишь сбой настройки клиента
            raise self._unavailable("presign_put", error) from error

    async def head(self, key: str) -> StoredObject | None:
        internal, _ = await self._clients()
        try:
            async with asyncio.timeout(self._deadlines.head):
                response = await internal.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if is_missing_object(error):
                return None
            raise self._unavailable("head", error) from error
        except _NETWORK_ERRORS as error:
            raise self._unavailable("head", error) from error
        etag = str(response.get("ETag", "")).strip('"')
        return StoredObject(
            size=int(response["ContentLength"]),
            etag=etag or None,
            content_type=response.get("ContentType"),
        )

    async def read_head(self, key: str, length: int) -> bytes | None:
        internal, _ = await self._clients()
        try:
            async with asyncio.timeout(self._deadlines.read):
                response = await internal.get_object(
                    Bucket=self._bucket, Key=key, Range=f"bytes=0-{length - 1}"
                )
                async with response["Body"] as stream:
                    return bytes(await stream.read())
        except ClientError as error:
            if is_missing_object(error):
                return None
            if is_unsatisfiable_range(error):
                return b""  # объект есть, но пустой: диапазон в нём брать не из чего
            raise self._unavailable("read_head", error) from error
        except _NETWORK_ERRORS as error:
            raise self._unavailable("read_head", error) from error

    async def read_object(self, key: str, max_bytes: int) -> bytes | None:
        internal, _ = await self._clients()
        try:
            async with asyncio.timeout(self._deadlines.transfer):
                response = await internal.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as stream:
                    if int(response.get("ContentLength", 0)) > max_bytes:
                        raise ObjectTooLargeError(key)
                    data = bytes(await stream.read())
        except ClientError as error:
            if is_missing_object(error):
                return None
            raise self._unavailable("read", error) from error
        except _NETWORK_ERRORS as error:
            raise self._unavailable("read", error) from error
        if len(data) > max_bytes:  # длину ответ не назвал или назвал неверно
            raise ObjectTooLargeError(key)
        return data

    async def write_object(
        self, key: str, body: bytes, *, content_type: str, cache_control: str | None = None
    ) -> None:
        internal, _ = await self._clients()
        options: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if cache_control is not None:
            options["CacheControl"] = cache_control
        try:
            async with asyncio.timeout(self._deadlines.transfer):
                await internal.put_object(**options)
        except ClientError as error:
            raise self._unavailable("write", error) from error
        except _NETWORK_ERRORS as error:
            raise self._unavailable("write", error) from error

    async def presign_get(
        self,
        *,
        key: str,
        expires_in: int,
        content_type: str | None = None,
        content_disposition: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        _, public = await self._clients()
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        # Заголовки ответа задаёт сама ссылка: SeaweedFS подставляет их вместо сохранённых.
        if content_type is not None:
            params["ResponseContentType"] = content_type
        if content_disposition is not None:
            params["ResponseContentDisposition"] = content_disposition
        if cache_control is not None:
            params["ResponseCacheControl"] = cache_control
        try:
            return str(
                await public.generate_presigned_url(
                    "get_object", Params=params, ExpiresIn=expires_in, HttpMethod="GET"
                )
            )
        except _NETWORK_ERRORS as error:  # подпись локальная; сюда попадёт лишь сбой настройки
            raise self._unavailable("presign_get", error) from error

    async def delete_many(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        internal, _ = await self._clients()
        gate = asyncio.Semaphore(_DELETE_CONCURRENCY)

        async def delete_one(key: str) -> None:
            async with gate:
                try:
                    await internal.delete_object(Bucket=self._bucket, Key=key)
                except ClientError as error:
                    if not is_missing_object(error):
                        raise self._unavailable("delete", error) from error
                except _NETWORK_ERRORS as error:
                    raise self._unavailable("delete", error) from error

        # Дожидаемся всех удалений и только потом поднимаем первую ошибку: оборванные соседние
        # запросы не должны висеть в фоне, а повтор всё равно безопасен (удаление идемпотентно).
        try:
            async with asyncio.timeout(self._deadlines.delete):
                outcomes = await asyncio.gather(
                    *(delete_one(key) for key in keys), return_exceptions=True
                )
        except TimeoutError as error:
            raise self._unavailable("delete", error) from error
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._clients_pair = None
        if stack is not None:
            await stack.aclose()
