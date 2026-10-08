"""Правила медиа (4.11, 5.8): назначения, допустимые типы, лимиты размера, имена файлов, ключи объектов.

Чистые функции и перечисления без ввода-вывода: их проверяют unit-тесты, а команды и ручки только
применяют результат.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from messunjerr.core.limits import LIMITS


class Purpose(StrEnum):
    AVATAR = "avatar"
    GROUP_AVATAR = "group_avatar"
    POST = "post"
    MESSAGE = "message"


class Kind(StrEnum):
    IMAGE = "image"
    FILE = "file"


class Status(StrEnum):
    """Жизненный цикл ресурса (5.8): `pending` → `uploaded` → `processing` → `ready` | `rejected`."""

    PENDING = "pending"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    REJECTED = "rejected"
    DELETED = "deleted"


class RejectReason(StrEnum):
    """Причина отклонения (5.8): размер ставит `complete`, остальное обработка (`process_media`)."""

    SIZE_MISMATCH = "size_mismatch"
    SIZE_EXCEEDS_LIMIT = "size_exceeds_limit"
    NOT_AN_IMAGE = "not_an_image"
    UNSUPPORTED_FORMAT = "unsupported_format"
    IMAGE_TOO_LARGE = "image_too_large"
    DECOMPRESSION_BOMB = "decompression_bomb"
    FORBIDDEN_TYPE = "forbidden_type"
    PROCESSING_FAILED = "processing_failed"


PURPOSES: tuple[str, ...] = tuple(item.value for item in Purpose)
KINDS: tuple[str, ...] = tuple(item.value for item in Kind)
STATUSES: tuple[str, ...] = tuple(item.value for item in Status)

AVATAR_PURPOSES = frozenset({Purpose.AVATAR, Purpose.GROUP_AVATAR})
AVATAR_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
IMAGE_CONTENT_TYPES = AVATAR_CONTENT_TYPES | {"image/gif"}
"""GIF допустим только в постах и сообщениях (4.11): аватар из него не делаем."""

FORBIDDEN_EXTENSIONS = frozenset(
    {"exe", "bat", "cmd", "scr", "msi", "ps1", "js", "vbs", "jar", "apk"}
)
"""Запрещённые расширения файлов (4.11)."""

OCTET_STREAM = "application/octet-stream"
FILENAME_MAX_LENGTH = 255
DEFAULT_FILENAME = "file"
IN_FLIGHT_STATUSES = (Status.PENDING, Status.UPLOADED, Status.PROCESSING)
"""Ресурсы, чей размер уже занят в квоте, хотя файл ещё не готов (резерв заявленного размера)."""

_TYPE_PART = r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
_CONTENT_TYPE = re.compile(rf"{_TYPE_PART}/{_TYPE_PART}")
_PATH_SEPARATORS = re.compile(r"[\\/]")
_WHITESPACE = re.compile(r"\s+")
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Управляющие, форматные (в том числе RLO, подменяющий расширение), суррогаты, разделители строк.
Неназначенные символы (`Cn`) не трогаем: таблица Unicode в Python может быть старее клиента."""


def normalize_content_type(raw: str) -> str | None:
    """`Image/JPEG; q=1` в `image/jpeg`; `None`, если это не тип вида `type/subtype`."""
    candidate = raw.split(";", 1)[0].strip().lower()
    return candidate if _CONTENT_TYPE.fullmatch(candidate) else None


def kind_for(content_type: str) -> Kind:
    """Изображение определяется по заявленному типу; окончательно решает содержимое (4.11)."""
    return Kind.IMAGE if content_type in IMAGE_CONTENT_TYPES else Kind.FILE


def signed_content_type(kind: Kind, content_type: str) -> str:
    """Тип, который подписывается в presigned PUT и сохраняется у объекта в хранилище.

    Изображения хранятся с заявленным типом. Остальные файлы всегда как `application/octet-stream`,
    чтобы хранилище никогда не отдало загруженное как `text/html` или SVG: заявленный тип остаётся
    в карточке ресурса, а выдача идёт с `Content-Disposition: attachment` (`attachment_disposition`).
    """
    return content_type if kind is Kind.IMAGE else OCTET_STREAM


def max_bytes(purpose: Purpose, kind: Kind) -> int:
    """Предел размера одного файла (4.11): аватар 5 МиБ, изображение 10 МиБ, файл 25 МиБ."""
    if purpose in AVATAR_PURPOSES:
        return LIMITS.avatar_max_bytes
    return LIMITS.image_max_bytes if kind is Kind.IMAGE else LIMITS.file_max_bytes


def sanitize_filename(raw: str) -> str:
    """Имя файла для хранения и показа: без путей, управляющих и форматных символов.

    - берётся последний сегмент пути (`C:\\dir\\a.png` и `../../a.png` дают `a.png`);
    - пробельные цепочки (в том числе табуляция и перевод строки) сжимаются в один пробел;
    - удаляются остальные управляющие символы и символы формата (RLO и подобные, которыми подменяют
      расширение); пробелы и точки по краям убираются (`a.exe.` это тоже `a.exe`);
    - слишком длинное имя укорачивается с сохранением расширения; пустое становится `file`.
    """
    name = unicodedata.normalize("NFC", raw)
    name = _PATH_SEPARATORS.split(name)[-1]
    # Сначала пробельные символы (табуляция и перевод строки это управляющие, но слова не склеивают).
    name = _WHITESPACE.sub(" ", name)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _STRIPPED_CATEGORIES)
    name = name.strip(" .")
    if not name:
        return DEFAULT_FILENAME
    if len(name) > FILENAME_MAX_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and 0 < len(extension) <= 16 and stem:
            name = stem[: FILENAME_MAX_LENGTH - len(extension) - 1] + "." + extension
        else:
            name = name[:FILENAME_MAX_LENGTH]
        name = name.rstrip(" .") or DEFAULT_FILENAME
    return name


def extension_of(filename: str) -> str:
    """Расширение в нижнем регистре без точки; пусто, если его нет (`.bashrc` расширения не имеет)."""
    stem, dot, extension = filename.rpartition(".")
    return extension.lower() if dot and stem else ""


def is_forbidden_extension(filename: str) -> bool:
    return extension_of(filename) in FORBIDDEN_EXTENSIONS


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition` для скачивания файла (RFC 6266 и RFC 5987).

    Имя целиком в `filename*` (UTF-8, процентное кодирование), а `filename` это запасное ASCII-имя
    для старых клиентов. Кавычки, обратная косая черта и `%` в запасном имени заменяются: значение
    попадает в заголовок ответа хранилища, и вставить в него чужой параметр или перевод строки нельзя
    (управляющих символов в очищенном имени нет, но здесь это ещё и проверено).
    """
    fallback = (
        "".join(ch if " " <= ch <= "~" and ch not in '"\\%' else "_" for ch in filename).strip()
        or DEFAULT_FILENAME
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


# --------------------------------------------------------------------------------- объекты в bucket
UPLOADS_PREFIX = "uploads/"
"""Закрытый префикс: оригинал загрузки `uploads/{asset_id}/original` и варианты не-аватаров."""

AVATARS_PREFIX = "public/avatars/"
"""Единственный публичный префикс (анонимное чтение разрешает SeaweedFS): варианты аватаров."""

WEBP = "image/webp"
IMAGE_MAX_PIXELS = LIMITS.image_max_pixels
"""Больше этого числа пикселей изображение отклоняется как `image_too_large` (4.11)."""
IMAGE_BOMB_PIXELS = 2 * IMAGE_MAX_PIXELS
"""Больше вдвое: уже `decompression_bomb` (так же делит порог сама Pillow: предупреждение и ошибка)."""

PUBLIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
"""Варианты аватаров не меняются (при смене аватара появляется новый `asset_id`): кэш на год."""
PRIVATE_CACHE_CONTROL = "private, max-age=300"
"""Ссылки на закрытые варианты и файлы живут десять минут: дольше браузеру их держать незачем."""


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """Один вариант изображения: имя в `variants` и `urls`, сторона в пикселях, имя объекта."""

    name: str
    size: int
    """У аватара сторона квадрата, у фото длинная сторона (меньше не масштабируется вверх)."""
    filename: str


AVATAR_VARIANTS = (VariantSpec("thumb", 64, "64.webp"), VariantSpec("medium", 256, "256.webp"))
PHOTO_VARIANTS = (
    VariantSpec("thumb", 320, "thumb.webp"),
    VariantSpec("medium", 1280, "medium.webp"),
)
VARIANT_NAMES = ("thumb", "medium")
ORIGINAL = "original"
"""Ключ в `variants`: оригинал сохранён и отдаётся (так делается у GIF, 4.11)."""


def variant_specs(purpose: Purpose) -> tuple[VariantSpec, ...]:
    return AVATAR_VARIANTS if purpose in AVATAR_PURPOSES else PHOTO_VARIANTS


def object_key(asset_id: uuid.UUID) -> str:
    """Ключ исходного объекта в bucket. Непубличный префикс: публичны только аватары (`public/…`)."""
    return f"{UPLOADS_PREFIX}{asset_id}/original"


def variant_key(asset_id: uuid.UUID, purpose: Purpose, spec: VariantSpec) -> str:
    """Ключ варианта: аватары в публичном префиксе, остальное рядом с оригиналом."""
    prefix = AVATARS_PREFIX if purpose in AVATAR_PURPOSES else UPLOADS_PREFIX
    return f"{prefix}{asset_id}/{spec.filename}"


def object_keys(asset_id: uuid.UUID, kind: Kind, purpose: Purpose) -> list[str]:
    """Все ключи, которые обработка могла создать для ресурса: оригинал и варианты его назначения.

    Удаление берёт их все, не заглядывая в `variants`: объекта, которого нет, хранилище не жалеет, а
    недописанный вариант упавшей обработки в карточке не записан, но в bucket мог остаться.
    """
    keys = [object_key(asset_id)]
    if kind is Kind.IMAGE:
        keys.extend(variant_key(asset_id, purpose, spec) for spec in variant_specs(purpose))
    return keys
