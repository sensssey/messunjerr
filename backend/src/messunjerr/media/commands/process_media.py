"""Команда `process_media` (S5-06, S6-01): проверка и обработка загруженного файла, статусы `processing`
→ `ready` или `rejected`.

**Изображения** перекодируются в WebP-варианты без метаданных (`media.infra.images`): аватары 64 и
256 пикселей в публичный префикс, фото 320 и 1280 в закрытый. Оригинал с EXIF, в том числе с
геометкой, после успеха не остаётся: на его месте лежит пустой объект. Пустой, а не удалённый,
потому что ссылка на загрузку ещё может жить, а условная запись (`If-None-Match: *`) отвечает
`412`, пока ключ занят; удалённый ключ позволил бы положить на его место новый файл мимо всякого
учёта. Исключение GIF: оригинал хранится и отдаётся (анимацию мы не обрабатываем), варианты у него
статичные, из первого кадра.

**Файлы** (не изображения) проверяются только на исполняемую сигнатуру и хранятся как есть.

Три шага, и ни один не держит блокировку БД во время сети или вычислений:

1. короткая транзакция: ресурс `uploaded` (или зависший `processing`) становится `processing`;
2. чтение объекта, разбор и уменьшение изображения (в потоке), запись вариантов;
3. короткая транзакция: итог (`ready` либо `rejected` с причиной, событие в outbox); если за это время
   ресурс удалили, итог не записывается, а записанные варианты убираются.

Задача идемпотентна: повтор после сбоя или дубль постановки видят уже готовое состояние и ничего не
делают, а варианты пишутся по детерминированным ключам. Копия, которая закончила позже уже
готового ресурса, чужих вариантов не трогает (`Outcome.DUPLICATE`). Сбой хранилища поднимается наверх
(`StorageUnavailableError`), повторы решает обёртка задачи в `messunjerr.jobs`. Недоступное хранилище
не повод отклонять файл: ресурс остаётся в `processing`, его подбирает `reconcile_uploads`, а если
хранилище не вернётся за сутки, очистка удалит загрузку (`cleanup_pending_uploads`). `processing_failed`
ставится, когда объекта в хранилище нет и когда сама обработка упала на этом файле.

**«Ядовитый» файл.** Процесс, убитый посреди разбора (память), и задача, упёршаяся в тайм-аут (поток
Pillow отменить нельзя), не оставляют итога: ресурс висит в `processing`, и сверка раз в пять минут
ставила бы его заново, пока очистка не удалила бы его через сутки, а воркер всё это время падал бы
или грелся на нём. Поэтому перед разбором счётчик `processing_attempts` растёт, а после обычного
окончания разбора сбрасывается; если он уже достиг `MAX_DECODE_ATTEMPTS`, ресурс отклоняется как
`processing_failed`. Сбои хранилища счётчик не трогают: разбор к тому времени закончен.
"""

import asyncio
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messunjerr.core.clock import utcnow
from messunjerr.core.jobs import JobQueue
from messunjerr.core.logs import get_logger
from messunjerr.core.metrics import MEDIA_PROCESSING_SECONDS, MEDIA_REJECTED
from messunjerr.core.uow import UnitOfWork
from messunjerr.media.commands.queueing import enqueue_object_deletion
from messunjerr.media.domain.events import AssetProcessed, AssetRejected, record
from messunjerr.media.domain.ports import (
    ObjectStorage,
    ObjectTooLargeError,
    StorageUnavailableError,
)
from messunjerr.media.domain.rules import (
    AVATAR_PURPOSES,
    OCTET_STREAM,
    ORIGINAL,
    PUBLIC_CACHE_CONTROL,
    WEBP,
    Kind,
    Purpose,
    RejectReason,
    Status,
    max_bytes,
    variant_key,
    variant_specs,
)
from messunjerr.media.domain.sniff import HEAD_BYTES, ImageFormat, Verdict, judge
from messunjerr.media.infra.images import (
    DecodeBudget,
    ImageRejectedError,
    RenderedImage,
    decode_cost_mb,
    inspect_image,
    render_image,
)
from messunjerr.media.infra.models import AssetRow
from messunjerr.media.infra.repositories import AssetRepository

MAX_DECODE_ATTEMPTS = 3
"""Сколько раз разбор файла может начаться и не закончиться (убит процесс, тайм-аут), прежде чем
файл отклонят. Одного раза мало: воркер могли остановить при выкладке, а три подряд это файл."""


class Outcome(StrEnum):
    READY = "ready"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    """Ресурса нет, его удалили или он ещё не готов к обработке: делать нечего."""
    DUPLICATE = "duplicate"
    """Другая копия задачи (потерянный ключ в Redis, ручная постановка) уже довела ресурс до
    `ready`: то, что записали варианты этой копии, те же объекты, и убирать их нельзя."""


@dataclass(frozen=True, slots=True)
class ProcessMedia:
    asset_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Claim:
    asset_id: uuid.UUID
    object_key: str
    kind: Kind
    purpose: Purpose
    stored_size: int | None
    """Размер загруженного объекта, записанный при завершении загрузки."""


@dataclass(frozen=True, slots=True)
class _Stored:
    """Что записано в хранилище вместо загрузки: варианты изображения."""

    variants: dict[str, dict[str, Any]]
    """Для карточки: имя варианта → ключ, размеры и размер в байтах (`media.assets.variants`)."""
    width: int
    height: int
    """Размеры большего варианта: по ним клиент резервирует место под картинку."""
    total_size: int
    """Сколько места ресурс занимает в хранилище (идёт в квоту)."""
    keeps_original: bool
    keys: list[str]
    """Записанные варианты: их надо убрать, если ресурс удалили, пока шла обработка."""


def _reject_row(
    uow: UnitOfWork, row: AssetRow, reason: RejectReason, *, jobs: JobQueue, moment: datetime
) -> None:
    """Итог «отклонён»: причина, событие в outbox и удаление объектов после коммита."""
    row.status = Status.REJECTED
    row.reject_reason = reason
    row.processed_at = moment
    record(uow.outbox, AssetRejected(row.id, row.owner_id, reason.value))
    uow.after_commit(partial(enqueue_object_deletion, jobs, row.id))


async def _claim(
    sessionmaker: async_sessionmaker[AsyncSession],
    asset_id: uuid.UUID,
    *,
    jobs: JobQueue,
    moment: datetime,
) -> _Claim | Outcome:
    """Берёт ресурс в работу. Вместо заявки приходит итог, если работать не с чем или не над чем:
    ресурса нет или он не ждёт обработки (`SKIPPED`), либо файл уже `MAX_DECODE_ATTEMPTS` раз
    обрывал разбор и отклонён здесь же (`REJECTED`)."""
    async with UnitOfWork(sessionmaker) as uow:
        row = await AssetRepository(uow.session).get(asset_id, for_update=True)
        if row is None or row.status not in (Status.UPLOADED, Status.PROCESSING):
            return Outcome.SKIPPED
        if row.processing_attempts >= MAX_DECODE_ATTEMPTS:
            _reject_row(uow, row, RejectReason.PROCESSING_FAILED, jobs=jobs, moment=moment)
            await uow.commit()
            return Outcome.REJECTED
        row.status = Status.PROCESSING
        claim = _Claim(row.id, row.object_key, Kind(row.kind), Purpose(row.purpose), row.size_bytes)
        await uow.commit()
        return claim


async def _note_decode(
    sessionmaker: async_sessionmaker[AsyncSession], asset_id: uuid.UUID, *, started: bool
) -> None:
    """Отмечает начало разбора (счётчик растёт) или его обычное окончание (счётчик сброшен)."""
    async with UnitOfWork(sessionmaker) as uow:
        repository = AssetRepository(uow.session)
        if started:
            await repository.count_decode_attempt(asset_id)
        else:
            await repository.clear_decode_attempts(asset_id)
        await uow.commit()


async def _store_variants(
    claim: _Claim, rendered: RenderedImage, keeps_original: bool, storage: ObjectStorage
) -> _Stored:
    specs = {spec.name: spec for spec in variant_specs(claim.purpose)}
    cache_control = PUBLIC_CACHE_CONTROL if claim.purpose in AVATAR_PURPOSES else None
    variants: dict[str, dict[str, Any]] = {}
    keys: list[str] = []
    total = 0
    for variant in rendered.variants:
        key = variant_key(claim.asset_id, claim.purpose, specs[variant.name])
        # Подряд, не вместе: ошибка одной записи не оставляет в фоне недописанные соседние.
        await storage.write_object(
            key, variant.data, content_type=WEBP, cache_control=cache_control
        )
        variants[variant.name] = {
            "key": key,
            "width": variant.width,
            "height": variant.height,
            "size": len(variant.data),
        }
        keys.append(key)
        total += len(variant.data)
    if keeps_original:
        original_size = claim.stored_size or 0
        variants[ORIGINAL] = {"key": claim.object_key, "size": original_size}
        total += original_size
    largest = rendered.largest
    return _Stored(variants, largest.width, largest.height, total, keeps_original, keys)


def _rejected(reason: RejectReason) -> Verdict:
    return Verdict(accepted=False, reject_reason=reason)


async def _transform(
    claim: _Claim,
    image_format: ImageFormat,
    storage: ObjectStorage,
    budget: DecodeBudget,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> _Stored | Verdict:
    """Читает изображение целиком, делает варианты и записывает их. Отказ приходит вердиктом."""
    log = get_logger("messunjerr.media")
    try:
        body = await storage.read_object(claim.object_key, max_bytes(claim.purpose, claim.kind))
    except ObjectTooLargeError:
        return _rejected(RejectReason.SIZE_EXCEEDS_LIMIT)
    if body is None:  # объект пропал между завершением загрузки и обработкой
        return _rejected(RejectReason.PROCESSING_FAILED)
    try:
        # Заголовок читает Python-код Pillow (циклы по частям файла): в потоке, а не в цикле событий
        # воркера, иначе чужой тяжёлый файл останавливал бы и остальные задачи.
        header = await asyncio.to_thread(inspect_image, body, expected=image_format)
        cost = decode_cost_mb(header, expected=image_format, purpose=claim.purpose)
    except ImageRejectedError as error:
        return _rejected(error.reason)
    except Exception:
        log.exception("media_render_failed", asset_id=str(claim.asset_id))
        return _rejected(RejectReason.PROCESSING_FAILED)
    async with budget.reserve(cost):
        # Счётчик попыток: сбой БД здесь не вина файла, он идёт наверх и задачу повторят.
        await _note_decode(sessionmaker, claim.asset_id, started=True)
        try:
            rendered = await asyncio.to_thread(
                render_image, body, purpose=claim.purpose, expected=image_format
            )
        except ImageRejectedError as error:
            return _rejected(error.reason)
        except Exception:
            # Сбой самой обработки на этом файле. Повтор дал бы то же самое, поэтому человек получает
            # «не удалось обработать», а не вечную очередь; содержимое файла в журнал не попадает.
            log.exception("media_render_failed", asset_id=str(claim.asset_id))
            return _rejected(RejectReason.PROCESSING_FAILED)
        await _note_decode(sessionmaker, claim.asset_id, started=False)
    keeps_original = image_format is ImageFormat.GIF and claim.purpose not in AVATAR_PURPOSES
    return await _store_variants(claim, rendered, keeps_original, storage)


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    asset_id: uuid.UUID,
    verdict: Verdict,
    stored: _Stored | None,
    *,
    jobs: JobQueue,
    moment: datetime,
) -> Outcome:
    async with UnitOfWork(sessionmaker) as uow:
        row = await AssetRepository(uow.session).get(asset_id, for_update=True)
        if row is None:
            return Outcome.SKIPPED
        if row.status == Status.READY:
            return Outcome.DUPLICATE
        if row.status != Status.PROCESSING:
            return Outcome.SKIPPED
        row.processed_at = moment
        if verdict.accepted:
            row.status = Status.READY
            if stored is not None:
                row.content_type = (
                    WEBP  # тип основного варианта; GIF-оригинал отдельно в `variants`
                )
                row.width = stored.width
                row.height = stored.height
                row.variants = stored.variants
                row.size_bytes = stored.total_size
            record(uow.outbox, AssetProcessed(row.id, row.owner_id, row.purpose, row.kind))
            await uow.commit()
            return Outcome.READY
        reason = verdict.reject_reason or RejectReason.PROCESSING_FAILED
        _reject_row(uow, row, reason, jobs=jobs, moment=moment)
        await uow.commit()
        return Outcome.REJECTED


async def _scrub_original(storage: ObjectStorage, claim: _Claim) -> None:
    """Заменяет оригинал изображения пустым объектом: EXIF с геометкой не должен лежать в хранилище.

    Лучшее усилие: ресурс уже `ready`, а недосланную замену доделает суточная сверка объектов.
    """
    try:
        await storage.write_object(claim.object_key, b"", content_type=OCTET_STREAM)
    except StorageUnavailableError:
        get_logger("messunjerr.media").warning("media_scrub_deferred", asset_id=str(claim.asset_id))


async def _discard_variants(storage: ObjectStorage, keys: list[str]) -> None:
    """Ресурс удалили, пока шла обработка: записанные варианты никому не нужны.

    Сбой хранилища здесь не страшен: варианты подберёт удаление объектов ресурса и суточная сверка.
    """
    with suppress(StorageUnavailableError):
        await storage.delete_many(keys)


async def process_media(
    command: ProcessMedia,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    jobs: JobQueue,
    budget: DecodeBudget | None = None,
    now: datetime | None = None,
) -> Outcome:
    log = get_logger("messunjerr.media")
    started = time.perf_counter()
    claim = await _claim(sessionmaker, command.asset_id, jobs=jobs, moment=now or utcnow())
    if isinstance(claim, Outcome):
        if claim is Outcome.REJECTED:  # файл MAX_DECODE_ATTEMPTS раз обрывал разбор
            MEDIA_REJECTED.labels(reason=RejectReason.PROCESSING_FAILED.value).inc()
            log.error(
                "media_gave_up",
                asset_id=str(command.asset_id),
                attempts=MAX_DECODE_ATTEMPTS,
                hint="разбор файла не раз не доходил до конца (память или тайм-аут)",
            )
        return claim

    label = "error"
    try:
        head = await storage.read_head(claim.object_key, HEAD_BYTES)
        stored: _Stored | None = None
        if head is None:  # объект пропал между завершением загрузки и обработкой
            verdict = _rejected(RejectReason.PROCESSING_FAILED)
        else:
            verdict = judge(claim.kind, claim.purpose, head)
            if verdict.accepted and verdict.image_format is not None:  # принятое изображение
                result = await _transform(
                    claim, verdict.image_format, storage, budget or DecodeBudget(), sessionmaker
                )
                if isinstance(result, Verdict):
                    verdict = result
                else:
                    stored = result
        outcome = await _finish(
            sessionmaker, command.asset_id, verdict, stored, jobs=jobs, moment=now or utcnow()
        )
        label = outcome.value
        if stored is not None:
            if outcome is Outcome.READY:
                if not stored.keeps_original:
                    await _scrub_original(storage, claim)
            elif outcome is not Outcome.DUPLICATE:  # у дубля те же ключи, что у готового ресурса
                await _discard_variants(storage, stored.keys)
    finally:
        MEDIA_PROCESSING_SECONDS.labels(kind=claim.kind.value, outcome=label).observe(
            time.perf_counter() - started
        )
    if outcome is Outcome.REJECTED and verdict.reject_reason is not None:
        MEDIA_REJECTED.labels(reason=verdict.reject_reason.value).inc()
    log.info(
        "media_processed",
        asset_id=str(command.asset_id),
        outcome=outcome.value,
        reason=verdict.reject_reason.value if verdict.reject_reason else None,
    )
    return outcome
