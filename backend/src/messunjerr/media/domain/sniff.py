"""Определение типа файла по первым байтам (4.11): заявленному `content_type` и расширению не верим.

Это заглушка обработки S5: она по сигнатуре решает, годится ли файл, и ничего не перекодирует.
Настоящую обработку на Pillow (перекодирование, EXIF, варианты, лимит мегапикселей) делает S6.
Чистые функции: на вход первые байты объекта (хватает 4 КиБ), на выходе вердикт.
"""

from dataclasses import dataclass
from enum import StrEnum

from messunjerr.media.domain.rules import AVATAR_PURPOSES, Kind, Purpose, RejectReason

HEAD_BYTES = 4096
"""Сколько первых байт нужно для вердикта."""

_PE_OFFSET_FIELD = 0x3C
_EXECUTABLE_MAGICS = (
    b"\x7fELF",  # ELF
    b"\xfe\xed\xfa\xce",  # Mach-O 32 бит
    b"\xfe\xed\xfa\xcf",  # Mach-O 64 бита
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # Mach-O fat или класс Java
)


class ImageFormat(StrEnum):
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    GIF = "gif"


IMAGE_MIME: dict[ImageFormat, str] = {
    ImageFormat.JPEG: "image/jpeg",
    ImageFormat.PNG: "image/png",
    ImageFormat.WEBP: "image/webp",
    ImageFormat.GIF: "image/gif",
}


def sniff_image(head: bytes) -> ImageFormat | None:
    """JPEG, PNG, WebP или GIF по сигнатуре; `None` для всего остального."""
    if head.startswith(b"\xff\xd8\xff"):
        return ImageFormat.JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageFormat.PNG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ImageFormat.GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ImageFormat.WEBP
    return None


_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif", b"avis")


def is_unsupported_image(head: bytes) -> bool:
    """Настоящее изображение другого формата (BMP, TIFF, ICO, HEIC, AVIF, JPEG XL, PSD, JPEG 2000).

    SVG, HTML и прочий текст сюда не попадают: это не изображение (`not_an_image`), как и всё
    неопознанное.
    """
    if head.startswith((b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00", b"8BPS", b"\xff\x0a")):
        return True
    if head.startswith(b"BM") and head[6:10] == b"\x00\x00\x00\x00":  # BMP: «BM», размер, нули
        return True
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return True
    return head.startswith((b"\x00\x00\x00\x0cJXL ", b"\x00\x00\x00\x0cjP  "))


def looks_executable(head: bytes) -> bool:
    """Исполняемый файл по сигнатуре: Windows PE, ELF, Mach-O, класс Java (4.11, `forbidden_type`)."""
    if head.startswith(_EXECUTABLE_MAGICS):
        return True
    if head.startswith(b"MZ"):
        if b"This program cannot be run in DOS mode" in head:
            return True
        end = _PE_OFFSET_FIELD + 4
        if len(head) >= end:
            offset = int.from_bytes(head[_PE_OFFSET_FIELD:end], "little")
            return head[offset : offset + 4] == b"PE\x00\x00"
    return False


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool
    content_type: str | None = None
    """Тип по содержимому для принятого изображения; у файла `None` (остаётся заявленный)."""
    reject_reason: RejectReason | None = None


def judge(kind: Kind, purpose: Purpose, head: bytes) -> Verdict:
    """Годится ли файл с такими первыми байтами для `kind` и `purpose`."""
    if kind is Kind.FILE:
        if looks_executable(head):
            return Verdict(accepted=False, reject_reason=RejectReason.FORBIDDEN_TYPE)
        return Verdict(accepted=True)
    detected = sniff_image(head)
    if detected is None:
        reason = (
            RejectReason.UNSUPPORTED_FORMAT
            if is_unsupported_image(head)
            else RejectReason.NOT_AN_IMAGE
        )
        return Verdict(accepted=False, reject_reason=reason)
    if purpose in AVATAR_PURPOSES and detected is ImageFormat.GIF:
        return Verdict(accepted=False, reject_reason=RejectReason.UNSUPPORTED_FORMAT)
    return Verdict(accepted=True, content_type=IMAGE_MIME[detected])
