"""Правила медиа (4.11, 5.8): типы, лимиты, имена файлов, ключи объектов."""

import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from messunjerr.core.limits import LIMITS, MIB
from messunjerr.media.domain.rules import (
    AVATAR_PURPOSES,
    FILENAME_MAX_LENGTH,
    FORBIDDEN_EXTENSIONS,
    OCTET_STREAM,
    Kind,
    Purpose,
    extension_of,
    is_forbidden_extension,
    kind_for,
    max_bytes,
    normalize_content_type,
    object_key,
    sanitize_filename,
    signed_content_type,
)


# ----------------------------------------------------------------------------- типы содержимого
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("image/jpeg", "image/jpeg"),
        ("IMAGE/JPEG", "image/jpeg"),
        ("  image/png ; charset=binary", "image/png"),
        ("application/vnd.ms-excel", "application/vnd.ms-excel"),
        ("application/x-7z-compressed", "application/x-7z-compressed"),
        ("text/plain; q=0.8", "text/plain"),
    ],
)
def test_content_types_are_normalized(raw: str, expected: str) -> None:
    assert normalize_content_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "image",
        "image/",
        "/jpeg",
        "image/jp eg",
        "image//jpeg",
        "text/html\r\nX: y",
        "*/*",
        "a" * 300,
    ],
)
def test_malformed_content_types_are_refused(raw: str) -> None:
    assert normalize_content_type(raw) is None


@pytest.mark.parametrize(
    ("content_type", "kind"),
    [
        ("image/jpeg", Kind.IMAGE),
        ("image/png", Kind.IMAGE),
        ("image/webp", Kind.IMAGE),
        ("image/gif", Kind.IMAGE),
        ("image/svg+xml", Kind.FILE),
        ("image/bmp", Kind.FILE),
        ("application/pdf", Kind.FILE),
        ("text/html", Kind.FILE),
    ],
)
def test_kind_follows_the_declared_type_but_svg_is_never_an_image(
    content_type: str, kind: Kind
) -> None:
    assert kind_for(content_type) is kind


def test_files_are_stored_as_octet_stream_whatever_was_declared() -> None:
    # Хранилище не должно отдать загруженное как text/html или SVG: заявленный тип остаётся в карточке.
    assert signed_content_type(Kind.FILE, "text/html") == OCTET_STREAM
    assert signed_content_type(Kind.FILE, "application/pdf") == OCTET_STREAM
    assert signed_content_type(Kind.IMAGE, "image/jpeg") == "image/jpeg"


# ----------------------------------------------------------------------------- лимиты
def test_size_limits_follow_the_table_of_section_4_11() -> None:
    assert max_bytes(Purpose.AVATAR, Kind.IMAGE) == 5 * MIB == LIMITS.avatar_max_bytes
    assert max_bytes(Purpose.GROUP_AVATAR, Kind.IMAGE) == 5 * MIB
    assert max_bytes(Purpose.POST, Kind.IMAGE) == 10 * MIB == LIMITS.image_max_bytes
    assert max_bytes(Purpose.MESSAGE, Kind.IMAGE) == 10 * MIB
    assert max_bytes(Purpose.POST, Kind.FILE) == 25 * MIB == LIMITS.file_max_bytes
    assert max_bytes(Purpose.MESSAGE, Kind.FILE) == 25 * MIB


def test_avatar_purposes_are_exactly_avatar_and_group_avatar() -> None:
    assert {Purpose.AVATAR, Purpose.GROUP_AVATAR} == AVATAR_PURPOSES


# ----------------------------------------------------------------------------- имена файлов
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\Documents\\report.pdf", "report.pdf"),
        ("dir/sub/..//pic.png", "pic.png"),
        ("  spaced   name .txt ", "spaced name .txt"),
        ("tab\tand\nnewline.txt", "tab and newline.txt"),
        ("zero\u200bwidth.txt", "zerowidth.txt"),
        ("evil\u202egpj.exe", "evilgpj.exe"),  # RLO прячет расширение: символ удаляется
        ("null\x00byte.png", "nullbyte.png"),
        ("report.pdf.", "report.pdf"),
        ("...", "file"),
        ("   ", "file"),
        ("/", "file"),
        ("Привет мир.PNG", "Привет мир.PNG"),
    ],
)
def test_filenames_lose_paths_and_control_characters(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_long_names_are_shortened_but_keep_the_extension() -> None:
    result = sanitize_filename("a" * 400 + ".jpeg")
    assert len(result) == FILENAME_MAX_LENGTH
    assert result.endswith(".jpeg")


def test_long_names_without_extension_are_cut() -> None:
    assert len(sanitize_filename("b" * 400)) == FILENAME_MAX_LENGTH


@given(st.text(max_size=600))
def test_a_sanitized_name_is_never_empty_nor_dangerous(raw: str) -> None:
    result = sanitize_filename(raw)
    assert 1 <= len(result) <= FILENAME_MAX_LENGTH
    assert "/" not in result
    assert "\\" not in result
    assert result == result.strip(" .") or result == "file"
    assert all(ord(ch) >= 32 and ch != "\x7f" for ch in result)


# ----------------------------------------------------------------------------- расширения
@pytest.mark.parametrize(
    ("filename", "extension"),
    [
        ("photo.JPG", "jpg"),
        ("archive.tar.gz", "gz"),
        (".bashrc", ""),
        ("noextension", ""),
        ("trailingdot.", ""),
        ("a.b.c.exe", "exe"),
    ],
)
def test_extension_is_the_last_suffix_in_lower_case(filename: str, extension: str) -> None:
    assert extension_of(filename) == extension


@pytest.mark.parametrize("extension", sorted(FORBIDDEN_EXTENSIONS))
def test_every_forbidden_extension_is_caught_in_any_case(extension: str) -> None:
    assert is_forbidden_extension(f"setup.{extension}")
    assert is_forbidden_extension(f"SETUP.{extension.upper()}")
    assert is_forbidden_extension(sanitize_filename(f"setup.{extension}. "))


def test_the_forbidden_list_is_the_one_from_the_specification() -> None:
    assert {
        "exe", "bat", "cmd", "scr", "msi", "ps1", "js", "vbs", "jar", "apk"
    } == FORBIDDEN_EXTENSIONS  # fmt: skip


@pytest.mark.parametrize(
    "filename", ["photo.jpg", "doc.pdf", "notes.txt", "script.sh", "exe", "x.exe.png"]
)
def test_ordinary_names_are_not_forbidden(filename: str) -> None:
    assert not is_forbidden_extension(filename)


# ----------------------------------------------------------------------------- ключи
def test_object_key_is_private_and_unique_per_asset() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    assert object_key(first) == f"uploads/{first}/original"
    assert object_key(first) != object_key(second)
    assert not object_key(first).startswith("public/")
