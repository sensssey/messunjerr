"""Обработка изображений на Pillow (S6-01, S6-08): варианты, EXIF, ориентация, лимиты, «бомбы».

Изображения строятся в тестах самой Pillow, готовых файлов нет. Ловушки из плана: EXIF с геометкой,
«бомба» (маленький файл с огромным растром), подмена формата, битые и обрезанные файлы.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# (в заглушках Pillow профиль sRGB не типизирован)

import asyncio
import io
import random
import struct
import zlib
from typing import Any

import pytest
from PIL import Image, ImageCms, ImageDraw, PngImagePlugin

from messunjerr.media.domain.rules import Kind, Purpose, RejectReason
from messunjerr.media.domain.sniff import ImageFormat, judge
from messunjerr.media.infra.images import (
    DecodeBudget,
    ImageHeader,
    ImageRejectedError,
    decode_cost_mb,
    inspect_image,
    render_image,
)

RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
WEBP_CHUNKS = {"VP8 ", "VP8L", "VP8X", "ALPH"}
"""Куски WebP без метаданных: картинка и её прозрачность. EXIF, XMP, ICCP и ANIM сюда не входят."""


# ----------------------------------------------------------------------------- построители
def quadrants(size: tuple[int, int] = (400, 300)) -> Image.Image:
    """Четыре цвета по четвертям: по ним видно поворот и отражение."""
    width, height = size
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width // 2 - 1, height // 2 - 1), fill=RED)
    draw.rectangle((width // 2, 0, width - 1, height // 2 - 1), fill=GREEN)
    draw.rectangle((0, height // 2, width // 2 - 1, height - 1), fill=BLUE)
    draw.rectangle((width // 2, height // 2, width - 1, height - 1), fill=YELLOW)
    return image


def encode(image: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def gps_exif(orientation: int = 1) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SecretMaker"
    exif[0x0110] = "SecretModel"
    exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (55.0, 45.0, 21.0), 3: "E", 4: (37.0, 37.0, 4.0)}
    return exif


def webp_chunks(data: bytes) -> list[str]:
    """Имена кусков контейнера RIFF: по ним видно, остались ли в WebP EXIF, XMP или профиль."""
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WEBP"
    names: list[str] = []
    offset = 12
    while offset + 8 <= len(data):
        names.append(data[offset : offset + 4].decode("ascii"))
        size = struct.unpack_from("<I", data, offset + 4)[0]
        offset += 8 + size + (size & 1)
    return names


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def pixel(image: Image.Image, x: int, y: int) -> tuple[int, ...]:
    value = image.getpixel((x, y))
    assert isinstance(value, tuple)
    return value


def level(image: Image.Image, x: int, y: int) -> int:
    value = image.getpixel((x, y))
    assert isinstance(value, int)
    return value


def classify(color: tuple[int, ...]) -> str:
    """Ближайший из четырёх цветов: сжатие с потерями цвет сдвигает, но не меняет."""
    palette = {"R": RED, "G": GREEN, "B": BLUE, "Y": YELLOW}
    return min(
        palette,
        key=lambda name: sum((a - b) ** 2 for a, b in zip(color[:3], palette[name], strict=True)),
    )


def quarters(image: Image.Image) -> str:
    """Цвета четвертей: левая верхняя, правая верхняя, левая нижняя, правая нижняя."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    points = [
        (width // 4, height // 4),
        (3 * width // 4, height // 4),
        (width // 4, 3 * height // 4),
        (3 * width // 4, 3 * height // 4),
    ]
    return "".join(classify(pixel(rgb, x, y)) for x, y in points)


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload))
    )


def png_bomb(width: int, height: int) -> bytes:
    """Настоящий однобитный PNG: заголовок называет огромный растр, а сжатые нули занимают килобайты."""
    row = b"\x00" * (1 + (width + 7) // 8)
    compressor = zlib.compressobj(9)
    stream = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", stream)
        + png_chunk(b"IEND", b"")
    )


def rgb_profile(primaries: list[tuple[float, float, float]], gamma: float) -> bytes:
    """ICC-профиль RGB по матрице и степенной кривой (версия 2): такой строит любой редактор."""

    def s15(value: float) -> bytes:
        return struct.pack(">i", round(value * 65536))

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \x00\x00\x00\x00" + s15(x) + s15(y) + s15(z)

    ascii_text = b"test\x00"
    description = (
        b"desc\x00\x00\x00\x00"
        + struct.pack(">I", len(ascii_text))
        + ascii_text
        + struct.pack(">IIHB", 0, 0, 0, 0)
        + b"\x00" * 67
    )
    curve = b"curv\x00\x00\x00\x00" + struct.pack(">IH", 1, round(gamma * 256)) + b"\x00\x00"
    tags: dict[bytes, bytes] = {
        b"desc": description,
        b"cprt": b"text\x00\x00\x00\x00test\x00",
        b"wtpt": xyz(0.9642, 1.0, 0.8249),
        b"rXYZ": xyz(*primaries[0]),
        b"gXYZ": xyz(*primaries[1]),
        b"bXYZ": xyz(*primaries[2]),
        b"rTRC": curve,
        b"gTRC": curve,
        b"bTRC": curve,
    }
    table = bytearray(struct.pack(">I", len(tags)))
    body = bytearray()
    start = 128 + 4 + 12 * len(tags)
    for signature, content in tags.items():
        table += signature + struct.pack(">II", start + len(body), len(content))
        body += content + b"\x00" * (-len(content) % 4)
    header = bytearray(128)
    struct.pack_into(">I", header, 0, 128 + len(table) + len(body))
    header[8:12] = bytes([2, 0x40, 0, 0])
    header[12:24] = b"mntrRGB XYZ "
    struct.pack_into(">6H", header, 24, 2026, 1, 1, 0, 0, 0)
    header[36:40] = b"acsp"
    header[68:80] = s15(0.9642) + s15(1.0) + s15(0.8249)
    return bytes(header) + bytes(table) + bytes(body)


DISPLAY_P3 = rgb_profile(
    [(0.5151, 0.2412, -0.0011), (0.2920, 0.6922, 0.0419), (0.1571, 0.0666, 0.7841)], 2.2
)


# ----------------------------------------------------------------------------- метаданные
@pytest.mark.parametrize("purpose", [Purpose.POST, Purpose.AVATAR])
def test_exif_with_a_geotag_never_reaches_the_variants(purpose: Purpose) -> None:
    source = encode(quadrants(), "JPEG", exif=gps_exif(), quality=90)
    exif = Image.open(io.BytesIO(source)).getexif()
    assert exif.get_ifd(0x8825)[1] == "N"  # геометка в исходнике есть: тест не проходит вхолостую
    assert b"SecretModel" in source

    rendered = render_image(source, purpose=purpose, expected=ImageFormat.JPEG)

    assert len(rendered.variants) == 2
    for variant in rendered.variants:
        assert set(webp_chunks(variant.data)) <= WEBP_CHUNKS
        assert b"Secret" not in variant.data
        assert b"Exif" not in variant.data
        assert len(decode(variant.data).getexif()) == 0


def test_png_text_exif_and_profile_chunks_do_not_survive() -> None:
    text = PngImagePlugin.PngInfo()
    text.add_text("Author", "SecretName")
    source = encode(quadrants(), "PNG", pnginfo=text, exif=gps_exif(), icc_profile=DISPLAY_P3)
    assert b"SecretName" in source
    assert b"SecretMaker" in source

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.PNG)

    for variant in rendered.variants:
        assert set(webp_chunks(variant.data)) <= WEBP_CHUNKS
        assert b"Secret" not in variant.data
        assert "ICCP" not in webp_chunks(variant.data)


@pytest.mark.parametrize(
    ("orientation", "expected", "swapped"),
    [
        (1, "RGBY", False),
        (2, "GRYB", False),  # отражение по горизонтали
        (3, "YBGR", False),  # поворот на 180°
        (4, "BYRG", False),  # отражение по вертикали
        (5, "RBGY", True),  # транспонирование
        (6, "BRYG", True),  # поворот на 90° по часовой стрелке
        (7, "YGBR", True),  # отражение по побочной диагонали
        (8, "GYRB", True),  # поворот на 90° против часовой стрелки
    ],
)
@pytest.mark.parametrize("purpose", [Purpose.POST, Purpose.AVATAR])
def test_exif_orientation_is_applied_to_the_pixels(
    purpose: Purpose, orientation: int, expected: str, swapped: bool
) -> None:
    exif = Image.Exif()
    exif[0x0112] = orientation
    source = encode(quadrants(), "JPEG", exif=exif, quality=95)

    rendered = render_image(source, purpose=purpose, expected=ImageFormat.JPEG)

    assert quarters(decode(rendered.largest.data)) == expected
    if purpose is Purpose.POST:
        assert (rendered.largest.width, rendered.largest.height) == (
            (300, 400) if swapped else (400, 300)
        )


@pytest.mark.parametrize(
    ("fmt", "expected_format", "options"),
    [("PNG", ImageFormat.PNG, {}), ("WEBP", ImageFormat.WEBP, {"quality": 95})],
)
def test_orientation_in_png_and_webp_is_applied_too(
    fmt: str, expected_format: ImageFormat, options: dict[str, Any]
) -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    source = encode(quadrants(), fmt, exif=exif, **options)

    rendered = render_image(source, purpose=Purpose.POST, expected=expected_format)

    assert quarters(decode(rendered.largest.data)) == "BRYG"


# ----------------------------------------------------------------------------- размеры
@pytest.mark.parametrize(
    ("size", "thumb", "medium"),
    [
        ((4000, 3000), (320, 240), (1280, 960)),
        ((3000, 4000), (240, 320), (960, 1280)),
        ((1280, 1280), (320, 320), (1280, 1280)),
        ((800, 600), (320, 240), (800, 600)),  # вверх не растягивается
        ((200, 100), (200, 100), (200, 100)),
        ((10000, 1), (320, 1), (1280, 1)),  # полоса не обнуляет сторону
    ],
)
def test_photo_variants_keep_proportions_and_never_upscale(
    size: tuple[int, int], thumb: tuple[int, int], medium: tuple[int, int]
) -> None:
    source = encode(Image.new("RGB", size, (40, 90, 160)), "PNG")

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.PNG)

    assert [(v.name, v.width, v.height) for v in rendered.variants] == [
        ("thumb", *thumb),
        ("medium", *medium),
    ]
    for variant in rendered.variants:
        image = Image.open(io.BytesIO(variant.data))
        assert image.format == "WEBP"
        assert image.size == (variant.width, variant.height)


def test_a_large_jpeg_is_reduced_by_the_decoder_and_still_comes_out_right() -> None:
    source = encode(quadrants((4000, 3000)), "JPEG", quality=90)

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.JPEG)

    assert [(v.width, v.height) for v in rendered.variants] == [(320, 240), (1280, 960)]
    assert quarters(decode(rendered.largest.data)) == "RGBY"


@pytest.mark.parametrize("size", [(400, 300), (300, 400), (50, 50), (3, 2), (10000, 1)])
def test_avatar_variants_are_exact_squares(size: tuple[int, int]) -> None:
    source = encode(Image.new("RGB", size, (200, 10, 10)), "PNG")

    rendered = render_image(source, purpose=Purpose.AVATAR, expected=ImageFormat.PNG)

    assert [(v.name, v.width, v.height) for v in rendered.variants] == [
        ("thumb", 64, 64),
        ("medium", 256, 256),
    ]


def test_group_avatar_gets_the_same_variants_as_an_avatar() -> None:
    source = encode(quadrants(), "JPEG")

    sizes = {
        purpose: [
            (v.width, v.height)
            for v in render_image(source, purpose=purpose, expected=ImageFormat.JPEG).variants
        ]
        for purpose in (Purpose.AVATAR, Purpose.GROUP_AVATAR)
    }

    assert sizes[Purpose.AVATAR] == sizes[Purpose.GROUP_AVATAR] == [(64, 64), (256, 256)]


def test_avatar_is_the_centered_square_of_the_frame() -> None:
    # Слева красное, посередине зелёный квадрат 300×300, справа синее: после обрезки остаётся зелёное.
    image = Image.new("RGB", (600, 300), RED)
    draw = ImageDraw.Draw(image)
    draw.rectangle((150, 0, 449, 299), fill=GREEN)
    draw.rectangle((450, 0, 599, 299), fill=BLUE)

    rendered = render_image(encode(image, "PNG"), purpose=Purpose.AVATAR, expected=ImageFormat.PNG)

    medium = decode(rendered.largest.data).convert("RGB")
    corners = {
        classify(pixel(medium, x, y)) for x, y in ((10, 10), (245, 10), (10, 245), (245, 245))
    }
    assert corners == {"G"}


# ----------------------------------------------------------------------------- режимы и форматы
def test_png_with_transparency_keeps_alpha() -> None:
    image = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
    ImageDraw.Draw(image).rectangle((0, 0, 99, 99), fill=(0, 0, 255, 0))

    rendered = render_image(encode(image, "PNG"), purpose=Purpose.POST, expected=ImageFormat.PNG)

    medium = decode(rendered.largest.data)
    assert medium.mode == "RGBA"
    assert pixel(medium, 20, 50)[3] == 0
    assert pixel(medium, 180, 50)[3] == 255


def test_palette_png_with_transparency_becomes_rgba() -> None:
    palette = Image.new("P", (50, 50))
    palette.putpalette([0, 0, 0, 255, 0, 0] + [0] * (254 * 3))
    palette.paste(1, (0, 0, 25, 50))
    source = encode(palette, "PNG", transparency=0)

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.PNG)

    medium = decode(rendered.largest.data)
    assert medium.mode == "RGBA"
    assert pixel(medium, 40, 10)[3] == 0
    red, _green, blue, alpha = pixel(medium, 5, 10)
    assert red > 240
    assert blue < 15
    assert alpha == 255


def test_sixteen_bit_gray_png_is_scaled_not_clipped() -> None:
    image = Image.new("I;16", (64, 64))
    for x in range(64):
        for y in range(64):
            image.putpixel((x, y), x * 1000)

    rendered = render_image(encode(image, "PNG"), purpose=Purpose.POST, expected=ImageFormat.PNG)

    gray = decode(rendered.largest.data).convert("L")
    left, middle, right = (level(gray, x, 10) for x in (2, 32, 60))
    assert left < 30
    assert 100 < middle < 150
    assert 200 < right < 255


def test_cmyk_jpeg_is_converted_to_rgb() -> None:
    cmyk_red = Image.new("CMYK", (80, 60), (0, 255, 255, 0))

    rendered = render_image(
        encode(cmyk_red, "JPEG"), purpose=Purpose.POST, expected=ImageFormat.JPEG
    )

    assert classify(pixel(decode(rendered.largest.data).convert("RGB"), 10, 10)) == "R"


def test_animation_is_flattened_to_the_first_frame() -> None:
    frames = [Image.new("RGB", (60, 40), color) for color in (RED, GREEN, BLUE)]
    gif = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    webp = encode(
        frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100, lossless=True
    )
    assert getattr(Image.open(io.BytesIO(gif)), "n_frames", 1) == 3
    assert getattr(Image.open(io.BytesIO(webp)), "n_frames", 1) == 3

    for source, fmt in ((gif, ImageFormat.GIF), (webp, ImageFormat.WEBP)):
        rendered = render_image(source, purpose=Purpose.POST, expected=fmt)
        for variant in rendered.variants:
            image = decode(variant.data)
            assert getattr(image, "n_frames", 1) == 1
            assert classify(pixel(image.convert("RGB"), 5, 5)) == "R"


def test_a_gif_is_refused_for_an_avatar_by_the_signature_check() -> None:
    gif = encode(quadrants(), "GIF")

    verdict = judge(Kind.IMAGE, Purpose.AVATAR, gif[:4096])

    assert (verdict.accepted, verdict.reject_reason) == (False, RejectReason.UNSUPPORTED_FORMAT)


# ----------------------------------------------------------------------------- цвет
def test_a_wide_gamut_profile_is_converted_to_srgb() -> None:
    # Те же числа в Display P3 это более насыщенный цвет, чем в sRGB: после приведения красный
    # канал растёт относительно зелёного. Без приведения разница осталась бы прежней.
    image = Image.new("RGB", (64, 64), (200, 100, 100))
    plain = render_image(encode(image, "PNG"), purpose=Purpose.POST, expected=ImageFormat.PNG)
    tagged = render_image(
        encode(image, "PNG", icc_profile=DISPLAY_P3), purpose=Purpose.POST, expected=ImageFormat.PNG
    )

    base = pixel(decode(plain.largest.data).convert("RGB"), 10, 10)
    converted = pixel(decode(tagged.largest.data).convert("RGB"), 10, 10)

    assert abs(base[0] - 200) < 8
    assert converted[0] - converted[1] > base[0] - base[1] + 15
    assert "ICCP" not in webp_chunks(tagged.largest.data)


def test_a_profile_keeps_the_alpha_channel() -> None:
    image = Image.new("RGBA", (64, 64), (200, 100, 100, 77))

    rendered = render_image(
        encode(image, "PNG", icc_profile=DISPLAY_P3), purpose=Purpose.POST, expected=ImageFormat.PNG
    )

    assert pixel(decode(rendered.largest.data), 10, 10)[3] == 77


def test_a_broken_profile_is_ignored_not_fatal() -> None:
    image = Image.new("RGB", (64, 64), (10, 200, 10))

    rendered = render_image(
        encode(image, "PNG", icc_profile=b"definitely not an icc profile"),
        purpose=Purpose.POST,
        expected=ImageFormat.PNG,
    )

    assert classify(pixel(decode(rendered.largest.data).convert("RGB"), 10, 10)) == "G"


def test_the_builtin_srgb_profile_does_not_change_colors() -> None:
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image = Image.new("RGB", (64, 64), (200, 30, 30))

    rendered = render_image(
        encode(image, "PNG", icc_profile=srgb), purpose=Purpose.POST, expected=ImageFormat.PNG
    )

    result = pixel(decode(rendered.largest.data).convert("RGB"), 10, 10)
    assert all(abs(a - b) < 8 for a, b in zip(result, (200, 30, 30), strict=True))


# ----------------------------------------------------------------------------- ловушки: лимиты
@pytest.mark.parametrize(
    ("width", "height", "reason"),
    [
        (5000, 5000, None),  # ровно 25 мегапикселей допустимо
        (5001, 5000, RejectReason.IMAGE_TOO_LARGE),
        (8000, 4000, RejectReason.IMAGE_TOO_LARGE),
        (7071, 7071, RejectReason.IMAGE_TOO_LARGE),  # чуть меньше 50 мегапикселей
        (7072, 7072, RejectReason.DECOMPRESSION_BOMB),  # чуть больше
        (20000, 20000, RejectReason.DECOMPRESSION_BOMB),
    ],
)
def test_pixel_limits_are_checked_from_the_header_without_decoding(
    width: int, height: int, reason: RejectReason | None
) -> None:
    source = png_bomb(width, height)
    assert len(source) < 300_000  # файл маленький, растр огромный: это и есть «бомба»

    if reason is None:
        assert inspect_image(source, expected=ImageFormat.PNG) == ImageHeader(width, height)
        return
    with pytest.raises(ImageRejectedError) as caught:
        inspect_image(source, expected=ImageFormat.PNG)
    assert caught.value.reason is reason
    with pytest.raises(ImageRejectedError) as caught_render:
        render_image(source, purpose=Purpose.POST, expected=ImageFormat.PNG)
    assert caught_render.value.reason is reason


def test_a_jpeg_header_that_claims_a_gigantic_raster_is_a_bomb() -> None:
    data = bytearray(encode(Image.new("RGB", (16, 16), RED), "JPEG"))
    start = data.index(b"\xff\xc0\x00\x11\x08")  # начало кадра: маркер, длина, точность
    struct.pack_into(">HH", data, start + 5, 60000, 60000)  # высота и ширина

    with pytest.raises(ImageRejectedError) as caught:
        render_image(bytes(data), purpose=Purpose.POST, expected=ImageFormat.JPEG)

    assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


def test_a_gif_header_that_claims_a_gigantic_screen_is_a_bomb() -> None:
    data = bytearray(encode(Image.new("RGB", (16, 16), RED), "GIF"))
    struct.pack_into("<HH", data, 6, 60000, 60000)  # логический экран

    with pytest.raises(ImageRejectedError) as caught:
        render_image(bytes(data), purpose=Purpose.POST, expected=ImageFormat.GIF)

    assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


@pytest.mark.parametrize(
    ("width", "height", "reason"),
    [
        (6000, 5000, RejectReason.IMAGE_TOO_LARGE),
        (16383, 16383, RejectReason.DECOMPRESSION_BOMB),
    ],
)
def test_a_webp_header_that_claims_a_large_raster_is_rejected(
    width: int, height: int, reason: RejectReason
) -> None:
    data = bytearray(encode(Image.new("RGB", (16, 16), RED), "WEBP", quality=50))
    start = data.index(b"VP8 ") + 8  # после заголовка куска: метка кадра, код начала, размеры
    struct.pack_into("<HH", data, start + 6, width, height)

    with pytest.raises(ImageRejectedError) as caught:
        render_image(bytes(data), purpose=Purpose.POST, expected=ImageFormat.WEBP)

    assert caught.value.reason is reason


# ----------------------------------------------------------------------------- ловушки: наводнение частями
def png_with_chunks(private_chunks: int, idat_pieces: int = 1) -> bytes:
    """Маленькая картинка 64×64, перед данными `private_chunks` пустых приватных чанков."""
    header = struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)
    noise = random.Random(7)  # случайные байты не сжимаются: потоку хватает на тысячи кусков
    raw = b"".join(b"\x00" + noise.randbytes(64 * 3) for _ in range(64))
    packed = zlib.compress(raw)
    size = max(1, -(-len(packed) // idat_pieces))
    pieces = [packed[i : i + size] for i in range(0, len(packed), size)]
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"prVt", b"") * private_chunks
        + b"".join(png_chunk(b"IDAT", piece) for piece in pieces)
        + png_chunk(b"IEND", b"")
    )


def progressive_jpeg() -> bytes:
    return encode(quadrants((320, 240)), "JPEG", quality=80, progressive=True)


def flood_of_empty_scans(jpeg: bytes, count: int) -> bytes:
    """Тот же снимок, где последний скан заменён `count` пустыми (маркер и заголовок без данных)."""
    last = jpeg.rindex(b"\xff\xda")
    length = struct.unpack(">H", jpeg[last + 2 : last + 4])[0]
    empty = jpeg[last : last + 2 + length]
    return jpeg[:last] + empty * count + jpeg[jpeg.rindex(b"\xff\xd9") :]


def test_an_ordinary_progressive_jpeg_has_few_scans_and_is_processed() -> None:
    source = progressive_jpeg()
    assert 1 < source.count(b"\xff\xda") < 30

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.JPEG)

    assert quarters(decode(rendered.largest.data)) == "RGBY"


def test_a_jpeg_with_thousands_of_scans_is_a_bomb_before_any_decoding() -> None:
    # Опыт: 10 000 пустых сканов (файл 243 КиБ) Pillow разбирала 27 секунд, по 2,7 мс на скан.
    source = flood_of_empty_scans(progressive_jpeg(), 3000)
    assert len(source) < 100_000

    for call in (
        lambda: inspect_image(source, expected=ImageFormat.JPEG),
        lambda: render_image(source, purpose=Purpose.POST, expected=ImageFormat.JPEG),
    ):
        with pytest.raises(ImageRejectedError) as caught:
            call()
        assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


def test_millions_of_empty_jpeg_segments_are_stopped_without_collecting_them() -> None:
    # Опыт: 6 миллионов пустых сегментов APP1 (файл 22 МиБ) занимали 774 МБ памяти и 9,5 секунды.
    jpeg = encode(quadrants((64, 64)), "JPEG")
    source = jpeg[:2] + b"\xff\xe1\x00\x02" * 400_000 + jpeg[2:]
    assert len(source) == len(jpeg) + 1_600_000

    with pytest.raises(ImageRejectedError) as caught:
        inspect_image(source, expected=ImageFormat.JPEG)

    assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


def test_hundreds_of_thousands_of_empty_png_chunks_are_a_bomb() -> None:
    # Опыт: 2 миллиона пустых приватных чанков (файл 23 МиБ) занимали 205 МБ и 9 секунд.
    source = png_with_chunks(private_chunks=200_000)
    assert len(source) > 2_000_000

    with pytest.raises(ImageRejectedError) as caught:
        inspect_image(source, expected=ImageFormat.PNG)

    assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


def test_a_png_with_a_few_thousand_idat_chunks_is_still_fine() -> None:
    # libpng по умолчанию режет сжатый поток на куски по 8 КиБ: PNG на 10 МиБ это около 1300 чанков.
    source = png_with_chunks(private_chunks=20, idat_pieces=6000)
    assert source.count(b"IDAT") >= 3000  # не меньше трёх тысяч настоящих кусков данных

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.PNG)

    assert (rendered.largest.width, rendered.largest.height) == (64, 64)


def test_a_gif_with_a_flood_of_extension_blocks_is_a_bomb() -> None:
    # Опыт: 500 000 блоков-комментариев (файл 2,4 МиБ) Pillow разбирала 18 секунд.
    gif = encode(Image.new("P", (8, 8), 0), "GIF")
    table_end = 13 + 3 * 2 ** ((gif[10] & 7) + 1)  # заголовок и глобальная таблица цветов
    source = gif[:table_end] + b"\x21\xfe\x01A\x00" * 100_000 + gif[table_end:]

    with pytest.raises(ImageRejectedError) as caught:
        inspect_image(source, expected=ImageFormat.GIF)

    assert caught.value.reason is RejectReason.DECOMPRESSION_BOMB


def test_a_flood_is_cut_off_quickly() -> None:
    import time

    source = png_with_chunks(private_chunks=200_000)
    started = time.perf_counter()
    with pytest.raises(ImageRejectedError):
        inspect_image(source, expected=ImageFormat.PNG)

    assert time.perf_counter() - started < 1.0  # без предела разбор занял бы секунды


# ----------------------------------------------------------------------------- ловушки: не изображение
def test_truncated_and_corrupt_files_are_not_images() -> None:
    jpeg = encode(quadrants(), "JPEG")
    png = encode(quadrants(), "PNG")
    corrupt_png = png[:60] + b"\xff" * 80 + png[140:]
    cases = [
        (jpeg[:300], ImageFormat.JPEG),
        (png[: len(png) // 2], ImageFormat.PNG),
        (corrupt_png, ImageFormat.PNG),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 200, ImageFormat.JPEG),
        (b"GIF89a" + b"\x00" * 10, ImageFormat.GIF),
    ]
    for source, fmt in cases:
        with pytest.raises(ImageRejectedError) as caught:
            render_image(source, purpose=Purpose.POST, expected=fmt)
        assert caught.value.reason is RejectReason.NOT_AN_IMAGE


def test_a_file_is_opened_only_as_the_format_the_signature_named() -> None:
    png = encode(quadrants(), "PNG")

    with pytest.raises(ImageRejectedError) as caught:
        render_image(png, purpose=Purpose.POST, expected=ImageFormat.JPEG)

    assert caught.value.reason is RejectReason.NOT_AN_IMAGE


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b"<html><script>alert(1)</script></html>",
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n",  # EPS открыл бы Ghostscript
    ],
)
def test_text_formats_are_not_images_under_any_declared_format(payload: bytes) -> None:
    for fmt in ImageFormat:
        with pytest.raises(ImageRejectedError) as caught:
            render_image(payload, purpose=Purpose.POST, expected=fmt)
        assert caught.value.reason is RejectReason.NOT_AN_IMAGE


# ----------------------------------------------------------------------------- память
def test_decode_cost_grows_with_pixels_and_format() -> None:
    header = ImageHeader(5000, 5000)
    costs = {fmt: decode_cost_mb(header, expected=fmt, purpose=Purpose.POST) for fmt in ImageFormat}

    assert costs[ImageFormat.WEBP] > costs[ImageFormat.PNG] > costs[ImageFormat.JPEG]
    avatar_jpeg = decode_cost_mb(header, expected=ImageFormat.JPEG, purpose=Purpose.AVATAR)
    assert avatar_jpeg < costs[ImageFormat.JPEG]
    small = decode_cost_mb(ImageHeader(100, 100), expected=ImageFormat.WEBP, purpose=Purpose.POST)
    assert 0 < small < 20


async def test_the_budget_lets_small_jobs_run_together_and_makes_big_ones_wait() -> None:
    budget = DecodeBudget(limit_mb=100)
    started: list[str] = []
    gates = {name: asyncio.Event() for name in "ab"}

    async def job(name: str, cost: int) -> None:
        async with budget.reserve(cost):
            started.append(name)
            if name in gates:
                await gates[name].wait()

    first = asyncio.create_task(job("a", 60))
    second = asyncio.create_task(job("b", 30))  # 60 + 30 помещается
    third = asyncio.create_task(job("c", 30))  # 60 + 30 + 30 нет: ждёт
    await asyncio.sleep(0.05)
    assert started == ["a", "b"]
    assert budget.used_mb == 90

    gates["b"].set()
    await second
    await asyncio.sleep(0.05)
    assert started == ["a", "b", "c"]  # освободилось место: третья пошла, первая ещё держит свои 60

    gates["a"].set()
    await asyncio.gather(first, third)
    assert budget.used_mb == 0


async def test_an_image_bigger_than_the_whole_budget_runs_alone() -> None:
    budget = DecodeBudget(limit_mb=50)
    running = 0
    peak = 0

    async def job(cost: int) -> None:
        nonlocal running, peak
        async with budget.reserve(cost):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(job(400), job(400), job(10))

    assert peak == 1  # 400 больше бюджета: идёт только в одиночку, и мелкая задача не вклинивается


async def test_a_cancelled_waiter_does_not_leak_budget() -> None:
    budget = DecodeBudget(limit_mb=50)
    release = asyncio.Event()

    async def holder() -> None:
        async with budget.reserve(40):
            await release.wait()

    async def waiter() -> None:
        async with budget.reserve(40):
            pytest.fail("должен был ждать")

    hold = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    wait = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait

    assert budget.used_mb == 40
    release.set()
    await hold
    assert budget.used_mb == 0


# ----------------------------------------------------------------------------- настоящие фото с причудами
def test_a_multi_picture_jpeg_is_processed_as_its_first_picture() -> None:
    # Такие файлы дают некоторые телефоны (MPO: основной кадр и дополнительные): формат "MPO", а не "JPEG".
    first = quadrants((200, 100))
    second = Image.new("RGB", (200, 100), (9, 9, 9))
    source = encode(first, "MPO", save_all=True, append_images=[second], quality=92)
    assert Image.open(io.BytesIO(source)).format == "MPO"

    rendered = render_image(source, purpose=Purpose.POST, expected=ImageFormat.JPEG)

    assert (rendered.largest.width, rendered.largest.height) == (200, 100)
    assert quarters(decode(rendered.largest.data)) == "RGBY"


def test_a_multi_picture_jpeg_is_also_read_at_reduced_scale() -> None:
    # Дефект из ревью: `draft` включался только для формата "JPEG", а у MPO формат другой, и снимок
    # в 24 МП читался целиком, хотя оценка памяти считала его уменьшенным.
    from messunjerr.media.infra.images import _draft_jpeg  # pyright: ignore[reportPrivateUsage]

    source = encode(quadrants((2400, 1800)), "MPO", save_all=True, append_images=[quadrants()])

    with Image.open(io.BytesIO(source), formats=["JPEG"]) as image:
        assert image.format == "MPO"
        _draft_jpeg(image, (300, 225))  # просит вдвое больше цели: 600×450
        assert image.size == (600, 450)  # декодер отдаёт четверть: ширина и высота меньше вчетверо


@pytest.mark.parametrize("purpose", [Purpose.POST, Purpose.AVATAR])
def test_garbage_in_the_exif_block_does_not_reject_a_good_photo(purpose: Purpose) -> None:
    # EXIF бывает битым (редакторы, обрезка по пути): снимок от этого не становится негодным.
    source = encode(quadrants(), "JPEG", exif=b"Exif\x00\x00" + bytes(range(7, 80)), quality=90)

    rendered = render_image(source, purpose=purpose, expected=ImageFormat.JPEG)

    assert quarters(decode(rendered.largest.data)) == "RGBY"
