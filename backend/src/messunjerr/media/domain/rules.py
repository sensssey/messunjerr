"""Правила медиа (4.11, 5.8): назначения, допустимые типы, лимиты размера, имена файлов, ключи объектов.

Чистые функции и перечисления без ввода-вывода: их проверяют unit-тесты, а команды и ручки только
применяют результат.
"""

import re
import unicodedata
import uuid
from enum import StrEnum

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
    """Причина отклонения (5.8). Часть причин ставит обработка изображений (S6)."""

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
    в карточке ресурса, а выдачу с `Content-Disposition: attachment` делает S6.
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


UPLOADS_PREFIX = "uploads/"
"""Под этим префиксом лежат загруженные оригиналы: `uploads/{asset_id}/original`."""


def object_key(asset_id: uuid.UUID) -> str:
    """Ключ исходного объекта в bucket. Непубличный префикс: публичны только `public/…` (S6)."""
    return f"{UPLOADS_PREFIX}{asset_id}/original"
