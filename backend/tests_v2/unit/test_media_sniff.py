"""Тип файла по первым байтам (4.11, заглушка S5): сигнатуры, вердикты, ловушки."""

import pytest

from messunjerr.media.domain.rules import Kind, Purpose, RejectReason
from messunjerr.media.domain.sniff import (
    HEAD_BYTES,
    ImageFormat,
    is_unsupported_image,
    judge,
    looks_executable,
    sniff_image,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
HTML = b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>"
PE = (
    b"MZ"
    + b"\x90" * 58
    + (0x80).to_bytes(4, "little")
    + b"\x00" * 64
    + b"PE\x00\x00"
    + b"\x00" * 32
)
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 64
MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 64
BMP = b"BM" + b"\x36\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x28\x00" + b"\x00" * 64
TIFF = b"II*\x00\x08\x00\x00\x00" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (JPEG, ImageFormat.JPEG),
        (PNG, ImageFormat.PNG),
        (GIF, ImageFormat.GIF),
        (WEBP, ImageFormat.WEBP),
        (b"GIF87a" + b"\x00" * 10, ImageFormat.GIF),
        (SVG, None),
        (HTML, None),
        (PDF, None),
        (b"", None),
        (b"\xff\xd8", None),  # обрезанная сигнатура JPEG
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),  # RIFF, но не WebP
    ],
)
def test_image_format_comes_from_the_signature(head: bytes, expected: ImageFormat | None) -> None:
    assert sniff_image(head) is expected


@pytest.mark.parametrize(
    "head", [BMP, TIFF, HEIC, b"8BPS" + b"\x00" * 20, b"\x00\x00\x01\x00" + b"\x00" * 20]
)
def test_real_images_of_other_formats_are_recognised_as_unsupported(head: bytes) -> None:
    assert sniff_image(head) is None
    assert is_unsupported_image(head)


@pytest.mark.parametrize("head", [JPEG, PNG, SVG, HTML, PDF, b"BM is just text", b"", b"hello"])
def test_ordinary_files_are_not_mistaken_for_unsupported_images(head: bytes) -> None:
    assert not is_unsupported_image(head)


@pytest.mark.parametrize("head", [PE, ELF, MACHO, b"\xca\xfe\xba\xbe\x00\x00\x00\x34"])
def test_executables_are_detected(head: bytes) -> None:
    assert looks_executable(head)


def test_a_pe_header_beyond_the_inspected_head_is_a_known_limit() -> None:
    """Ограничение S5: смотрим только первые 4 КиБ. PE, чей заголовок лежит дальше, а заглушки DOS нет,
    проверку обходит; файл при этом хранится как `application/octet-stream` и отдаётся вложением."""
    far = (HEAD_BYTES + 100).to_bytes(4, "little")
    head = (b"MZ" + b"\x00" * 58 + far + b"\x00" * 100).ljust(HEAD_BYTES, b"\x00")
    assert not looks_executable(head)


@pytest.mark.parametrize(
    "head",
    [
        PDF,
        JPEG,
        SVG,
        b"MZ is a text file about nothing",
        b"#!/bin/sh\necho hi\n",
        b"",
        b"PK\x03\x04" + b"\x00" * 30,
    ],
)
def test_ordinary_files_are_not_taken_for_executables(head: bytes) -> None:
    assert not looks_executable(head)


# ----------------------------------------------------------------------------- вердикт
@pytest.mark.parametrize("purpose", list(Purpose))
@pytest.mark.parametrize(
    ("head", "mime"),
    [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")],
)
def test_jpeg_png_and_webp_are_accepted_as_images_for_every_purpose(
    purpose: Purpose, head: bytes, mime: str
) -> None:
    verdict = judge(Kind.IMAGE, purpose, head)
    assert verdict.accepted
    assert verdict.content_type == mime
    assert verdict.reject_reason is None


def test_the_type_comes_from_the_content_not_from_the_declaration() -> None:
    # Заявили JPEG, а внутри PNG: решает содержимое.
    assert judge(Kind.IMAGE, Purpose.POST, PNG).content_type == "image/png"


def test_gif_is_fine_in_posts_and_messages_but_not_as_an_avatar() -> None:
    assert judge(Kind.IMAGE, Purpose.POST, GIF).accepted
    assert judge(Kind.IMAGE, Purpose.MESSAGE, GIF).content_type == "image/gif"
    for purpose in (Purpose.AVATAR, Purpose.GROUP_AVATAR):
        verdict = judge(Kind.IMAGE, purpose, GIF)
        assert not verdict.accepted
        assert verdict.reject_reason is RejectReason.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("head", [SVG, HTML, PDF, b"just text", b"", b"\x00" * 100, PE])
def test_traps_under_an_image_type_are_not_an_image(head: bytes) -> None:
    verdict = judge(Kind.IMAGE, Purpose.POST, head)
    assert not verdict.accepted
    assert verdict.reject_reason is RejectReason.NOT_AN_IMAGE


@pytest.mark.parametrize("head", [BMP, TIFF, HEIC])
def test_unsupported_but_real_images_get_their_own_reason(head: bytes) -> None:
    verdict = judge(Kind.IMAGE, Purpose.POST, head)
    assert not verdict.accepted
    assert verdict.reject_reason is RejectReason.UNSUPPORTED_FORMAT


@pytest.mark.parametrize("head", [PE, ELF, MACHO])
def test_executables_are_refused_as_files(head: bytes) -> None:
    verdict = judge(Kind.FILE, Purpose.MESSAGE, head)
    assert not verdict.accepted
    assert verdict.reject_reason is RejectReason.FORBIDDEN_TYPE


@pytest.mark.parametrize("head", [PDF, SVG, HTML, JPEG, b"plain text", b"PK\x03\x04", b""])
def test_other_files_are_accepted_as_they_are(head: bytes) -> None:
    verdict = judge(Kind.FILE, Purpose.POST, head)
    assert verdict.accepted
    assert verdict.content_type is None  # у файла остаётся заявленный тип
