"""Обработка изображений (4.11, S6-01): из загруженного файла получаются безопасные WebP-варианты.

Чистые функции над байтами: ни сети, ни БД. Тяжёлые (процессор и память), поэтому команда зовёт
`render_image` в потоке (`asyncio.to_thread`): Pillow освобождает GIL в декодере, кодере и при
изменении размера. Сколько декодирований идёт одновременно, решает `DecodeBudget`.

Что гарантируется:

- **Тип по содержимому.** Файл открывается только как тот формат, который назвала проверка
  сигнатуры (`formats=[…]`): остальные форматы Pillow (EPS через Ghostscript, SGI, XBM и прочие)
  не пробуются. Любая ошибка разбора это `not_an_image`.
- **Лимит пикселей до декодирования.** Размер читается из заголовка: больше 25 мегапикселей это
  `image_too_large`, больше 50 это `decompression_bomb` (маленький файл с огромным растром).
  Проверку делаем сами, поэтому `Image.MAX_IMAGE_PIXELS` отключён: иначе между порогами Pillow
  выдавала бы предупреждения, а не отказ.
- **Только пиксели.** Результат кодируется заново, метаданные не передаются: EXIF (в том числе
  геометка и модель устройства), XMP, IPTC, миниатюры и ICC-профиль не попадают в варианты. Ориентация
  из EXIF применяется к пикселям до того, как EXIF отбрасывается. Цвета из профиля перед этим
  приводятся к sRGB (иначе снимки в Display P3 теряли бы насыщенность).
- **Анимация не обрабатывается.** Берётся первый кадр.
- **Память под контролем.** У JPEG при чтении сразу уменьшается масштаб (`draft`), прочие форматы
  сначала сжимаются «коробочным» уменьшением (`reducing_gap`), а не целиком фильтром Ланцоша.
  Остаток (весь растр PNG и GIF, а у WebP несколько копий) учитывает `DecodeBudget`.
- **Структура файла не наводняется.** Размер растра ограничен, а число частей нет: JPEG из тысяч
  сканов (2,7 мс на скан, файл в 243 КиБ разбирается 27 с), миллионы пустых сегментов APP в JPEG
  (22 МиБ дают 774 МБ памяти: Pillow копит их списком), миллионы пустых чанков PNG (205 МБ и 9 с),
  сотни тысяч блоков-расширений GIF (17 с на 2,4 МиБ). Поэтому сканов JPEG допускается не больше
  `_JPEG_MAX_SCANS`, а разбор файла Pillow (его циклы по сегментам, чанкам и блокам написаны на
  Python) может обратиться к файлу не больше `_MAX_PARSER_READS` раз: настоящие изображения
  укладываются в тысячи обращений. Сверх лимита это `decompression_bomb`.

Варианты (`media.domain.rules`): аватар 64 и 256 пикселей, квадрат по центру кадра; фото 320 и 1280
по длинной стороне, пропорции сохраняются, вверх не растягиваются.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# (в заглушках Pillow часть аргументов зависит от необязательного numpy)

import asyncio
import io
import math
import struct
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageCms, ImageOps

from messunjerr.media.domain.rules import (
    AVATAR_PURPOSES,
    AVATAR_VARIANTS,
    IMAGE_BOMB_PIXELS,
    IMAGE_MAX_PIXELS,
    PHOTO_VARIANTS,
    Purpose,
    RejectReason,
    VariantSpec,
)
from messunjerr.media.domain.sniff import ImageFormat

# Размер проверяется в `_check_dimensions` до декодирования. Свой порог нужен, чтобы граница
# «слишком большое» и «бомба» была нашей (25 и 50 мегапикселей), а не границей предупреждения Pillow.
Image.MAX_IMAGE_PIXELS = None

_FORMAT_NAMES: Final = {
    ImageFormat.JPEG: "JPEG",
    ImageFormat.PNG: "PNG",
    ImageFormat.WEBP: "WEBP",
    ImageFormat.GIF: "GIF",
}
_RESAMPLE: Final = Image.Resampling.LANCZOS
_REDUCING_GAP: Final = 2.0
_PHOTO_QUALITY: Final = 80
_AVATAR_QUALITY: Final = 85
_WEBP_METHOD: Final = 4
_MAX_ICC_BYTES: Final = 2 * 1024 * 1024
"""Профиль больше этого размера не читаем: цвета не стоят разбора чужого сложного файла."""
_JPEG_MAX_SCANS: Final = 100
"""Маркеров начала скана в JPEG. Обычный прогрессивный снимок даёт 10-20 сканов, непрогрессивный 1-3."""
_MAX_PARSER_READS: Final = 50_000
"""Обращений к файлу при разборе одного изображения. Настоящему файлу хватает сотен (JPEG) и тысяч
(PNG с IDAT по 8 КиБ на 10 МиБ: около 4 тысяч), хотя бы и в 25 МП; наводнение мелкими частями
упирается в предел за доли секунды и без накопления памяти."""
_JPEG_FORMATS: Final = ("JPEG", "MPO")
"""Так Pillow называет JPEG: MPO (несколько снимков в одном файле, его дают некоторые телефоны)
открывается тем же разбором, но с другим `format`."""
_RESIZABLE_MODES: Final = frozenset({"RGB", "RGBA", "L", "LA", "CMYK"})
_ALPHA_MODES: Final = frozenset({"LA", "PA", "La", "RGBa", "RGBA"})
_DECODE_ERRORS: Final = (OSError, SyntaxError, ValueError, EOFError, OverflowError, struct.error)
"""Так Pillow сообщает об обрезанном, битом и неправдоподобном файле (`UnidentifiedImageError`
это `OSError`)."""

# Сколько мегабайт памяти уходит на мегапиксель при декодировании (замер на изображениях по 24 МП,
# округлено вверх, Pillow 12.3). У WebP декодер держит несколько полных копий кадра.
_MEMORY_MB_PER_MEGAPIXEL: Final = {
    ImageFormat.JPEG: 6,
    ImageFormat.PNG: 10,
    ImageFormat.GIF: 8,
    ImageFormat.WEBP: 16,
}
_AVATAR_JPEG_MB_PER_MEGAPIXEL: Final = 2
"""Аватар из JPEG читается с уменьшенным масштабом декодера (`draft`) и почти ничего не стоит."""
_MEMORY_OVERHEAD_MB: Final = 8
DEFAULT_DECODE_BUDGET_MB: Final = 400
"""Хватает на одно изображение в 25 МП любого формата либо на два-три средних."""


class ImageRejectedError(Exception):
    """Файл не годится как изображение. `reason` уйдёт в `reject_reason` ресурса."""

    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ImageHeader:
    """Что сказано в заголовке файла: размер растра, до его декодирования."""

    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class RenderedVariant:
    name: str
    """`thumb` или `medium` (ключ в `variants` ресурса и в `urls`)."""
    data: bytes
    """WebP без метаданных."""
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class RenderedImage:
    variants: tuple[RenderedVariant, ...]
    """От меньшего к большему."""

    @property
    def largest(self) -> RenderedVariant:
        return self.variants[-1]


class _StructureFloodError(Exception):
    """В файле слишком много мелких частей (сегментов, чанков, блоков): разбор не продолжаем."""


class _CountedBytes(io.BytesIO):
    """Файл в памяти, который считает обращения парсера (`read`) и обрывает наводнение частями.

    Pillow разбирает сегменты JPEG, чанки PNG и блоки GIF циклами на Python: по одному-двум чтениям на
    часть. Миллионы пустых частей в файле на мегабайты стоили бы десятки секунд и сотни мегабайт
    (списки частей Pillow копит), а исключение ниже останавливает цикл через `_MAX_PARSER_READS`.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self._reads = 0

    def read(self, size: int | None = -1, /) -> bytes:
        self._reads += 1
        if self._reads > _MAX_PARSER_READS:
            raise _StructureFloodError
        return super().read(size)


def _open(data: bytes, expected: ImageFormat) -> Image.Image:
    """Открывает `data` как `expected` (и только как он); сканов JPEG не больше `_JPEG_MAX_SCANS`."""
    if expected is ImageFormat.JPEG and data.count(b"\xff\xda") > _JPEG_MAX_SCANS:
        raise ImageRejectedError(RejectReason.DECOMPRESSION_BOMB)
    return Image.open(_CountedBytes(data), formats=[_FORMAT_NAMES[expected]])


@contextmanager
def _rejecting() -> Generator[None]:
    """Переводит ошибки Pillow в `ImageRejectedError` с причиной из каталога 5.8."""
    try:
        yield
    except ImageRejectedError:
        raise
    except (Image.DecompressionBombError, _StructureFloodError) as error:
        raise ImageRejectedError(RejectReason.DECOMPRESSION_BOMB) from error
    except MemoryError as error:
        # Не хватило памяти на разрешённый размер: для нас это слишком большое изображение. Повтор
        # дал бы то же самое, а воркер упёрся бы в предел памяти снова и снова.
        raise ImageRejectedError(RejectReason.IMAGE_TOO_LARGE) from error
    except _DECODE_ERRORS as error:
        raise ImageRejectedError(RejectReason.NOT_AN_IMAGE) from error


def inspect_image(data: bytes, *, expected: ImageFormat) -> ImageHeader:
    """Размер изображения по заголовку; пиксели не декодируются, поэтому вызов дешёвый.

    Бросает `ImageRejectedError`, если файл не открывается как `expected` или растр больше лимита.
    """
    with _rejecting(), _open(data, expected) as image:
        _check_dimensions(image)
        return ImageHeader(*image.size)


def render_image(data: bytes, *, purpose: Purpose, expected: ImageFormat) -> RenderedImage:
    """Варианты изображения `data`, которое проверка сигнатуры признала форматом `expected`.

    Бросает `ImageRejectedError` с причиной отказа; остальные исключения это сбой самой обработки
    (команда ставит им `processing_failed`).
    """
    with _rejecting(), _open(data, expected) as image:
        _check_dimensions(image)
        profile = _icc_profile(image)
        if purpose in AVATAR_PURPOSES:
            return _render_avatar(image, profile)
        return _render_photo(image, profile)


def decode_cost_mb(header: ImageHeader, *, expected: ImageFormat, purpose: Purpose) -> int:
    """Оценка памяти (МБ), которую займёт декодирование и уменьшение этого изображения."""
    per_megapixel = (
        _AVATAR_JPEG_MB_PER_MEGAPIXEL
        if purpose in AVATAR_PURPOSES and expected is ImageFormat.JPEG
        else _MEMORY_MB_PER_MEGAPIXEL[expected]
    )
    return math.ceil(header.pixels / 1_000_000 * per_megapixel) + _MEMORY_OVERHEAD_MB


class DecodeBudget:
    """Не даёт одновременным декодированиям съесть память воркера.

    Очередь `media` берёт две задачи разом (4.12), и два тяжёлых изображения по 25 МП (WebP до
    400 МБ каждое) вместе не поместились бы в контейнер. Задача резервирует оценку своей памяти и ждёт,
    пока бюджет позволит; изображение дороже всего бюджета идёт в одиночку.
    """

    def __init__(self, limit_mb: int = DEFAULT_DECODE_BUDGET_MB) -> None:
        self._limit = limit_mb
        self._used = 0
        self._changed = asyncio.Condition()

    @property
    def used_mb(self) -> int:
        return self._used

    @asynccontextmanager
    async def reserve(self, cost_mb: int) -> AsyncGenerator[None]:
        cost = min(cost_mb, self._limit)
        async with self._changed:
            await self._changed.wait_for(
                lambda: self._used == 0 or self._used + cost <= self._limit
            )
            self._used += cost
        try:
            yield
        finally:
            async with self._changed:
                self._used -= cost
                self._changed.notify_all()


def _check_dimensions(image: Image.Image) -> None:
    width, height = image.size
    if width < 1 or height < 1:
        raise ImageRejectedError(RejectReason.NOT_AN_IMAGE)
    pixels = width * height
    if pixels > IMAGE_BOMB_PIXELS:
        raise ImageRejectedError(RejectReason.DECOMPRESSION_BOMB)
    if pixels > IMAGE_MAX_PIXELS:
        raise ImageRejectedError(RejectReason.IMAGE_TOO_LARGE)


def _icc_profile(image: Image.Image) -> bytes | None:
    profile = image.info.get("icc_profile")
    if isinstance(profile, bytes) and 0 < len(profile) <= _MAX_ICC_BYTES:
        return profile
    return None


def _scale_down(value: int) -> float:
    """16-битное значение в 8-битное (для серых PNG с глубиной 16 бит)."""
    return value / 256


def _make_resizable(image: Image.Image) -> Image.Image:
    """Режим, в котором изображение масштабируется без искажений.

    Палитровые и однобитные изображения при масштабировании Pillow ближайшим соседом портит, а
    16-битные серые обрезает до белого, поэтому их переводим в RGB(A) или 8 бит до масштабирования.
    """
    mode = image.mode
    if mode in _RESIZABLE_MODES:
        return image
    if mode.startswith("I") or mode == "F":
        return image.point(_scale_down).convert("L")
    has_alpha = mode in _ALPHA_MODES or "transparency" in image.info
    return image.convert("RGBA" if has_alpha else "RGB")


def _to_srgb(image: Image.Image, profile: bytes | None) -> Image.Image:
    """RGB или RGBA в sRGB: по профилю файла, если он есть и разбирается; серое и CMYK приводятся."""
    mode = image.mode
    if profile is not None and mode in ("RGB", "RGBA", "CMYK"):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            converted = ImageCms.profileToProfile(
                image,
                source,
                ImageCms.createProfile("sRGB"),
                outputMode="RGBA" if mode == "RGBA" else "RGB",
            )
        except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
            converted = None  # профиль битый или не подходит режиму: цвета оставляем как есть
        if converted is not None:
            return converted
    if mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if mode == "LA" else "RGB")


def _upright(image: Image.Image) -> Image.Image:
    """Применяет ориентацию из EXIF к пикселям (метка при этом исчезает из копии).

    Ориентация это необязательные метаданные: битый блок EXIF (редактор, обрезка по пути) не повод
    отклонять снимок, он просто остаётся как есть.
    """
    image.load()  # ошибки самого файла (обрыв, битые данные) всплывают здесь, а не прячутся ниже
    try:
        return ImageOps.exif_transpose(image)
    except Exception:
        return image.copy()


def _encode(image: Image.Image, spec: VariantSpec, quality: int) -> RenderedVariant:
    buffer = io.BytesIO()
    # Метаданных нет: Pillow пишет в WebP только то, что передано в `exif=`, `icc_profile=` и `xmp=`.
    image.save(buffer, format="WEBP", quality=quality, method=_WEBP_METHOD)
    return RenderedVariant(spec.name, buffer.getvalue(), image.width, image.height)


def _draft_jpeg(image: Image.Image, target: tuple[int, int]) -> None:
    """Просит декодер JPEG отдавать картинку в 2, 4 или 8 раз меньше, но не меньше удвоенной цели.

    `Image.thumbnail` делает то же сам, но по размеру коробки, а не по результату с сохранением
    пропорций, поэтому для снимка 6000×4000 и коробки 1280 масштаб оставался полным.
    """
    if image.format in _JPEG_FORMATS:
        image.draft(None, (target[0] * 2, target[1] * 2))


def _render_photo(image: Image.Image, profile: bytes | None) -> RenderedImage:
    thumb_spec, medium_spec = PHOTO_VARIANTS
    width, height = image.size
    scale = min(1.0, medium_spec.size / max(width, height))
    _draft_jpeg(image, (max(1, round(width * scale)), max(1, round(height * scale))))
    prepared = _make_resizable(image)
    prepared.thumbnail((medium_spec.size, medium_spec.size), _RESAMPLE, reducing_gap=_REDUCING_GAP)
    medium = _to_srgb(_upright(prepared), profile)
    thumb = medium.copy()
    thumb.thumbnail((thumb_spec.size, thumb_spec.size), _RESAMPLE, reducing_gap=_REDUCING_GAP)
    return RenderedImage(
        (_encode(thumb, thumb_spec, _PHOTO_QUALITY), _encode(medium, medium_spec, _PHOTO_QUALITY))
    )


def _render_avatar(image: Image.Image, profile: bytes | None) -> RenderedImage:
    thumb_spec, medium_spec = AVATAR_VARIANTS
    # С запасом вдвое над большим вариантом по обеим сторонам: обрезка по центру отбрасывает часть кадра.
    _draft_jpeg(image, (medium_spec.size, medium_spec.size))
    prepared = _make_resizable(image)
    width, height = prepared.size
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    box = (left, top, left + side, top + side)
    # Центральный квадрат при повороте на 90° остаётся тем же квадратом, поэтому ориентацию
    # применяем уже к маленьким готовым картинкам.
    variants: list[RenderedVariant] = []
    for spec in (thumb_spec, medium_spec):
        square = prepared.resize(
            (spec.size, spec.size), _RESAMPLE, box=box, reducing_gap=_REDUCING_GAP
        )
        variants.append(_encode(_to_srgb(_upright(square), profile), spec, _AVATAR_QUALITY))
    return RenderedImage(tuple(variants))
