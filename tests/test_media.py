"""Проверка подготовки картинок."""

from __future__ import annotations

import io

from max2tg.util.media import (
    human_size,
    safe_filename,
    to_telegram_photo,
    to_telegram_sticker,
)


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _webp_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 128, 255)).save(buffer, format="WEBP")
    return buffer.getvalue()


def test_png_passes_through_unchanged() -> None:
    data = _png_bytes()
    assert to_telegram_photo(data) is data


def test_webp_is_converted_to_jpeg() -> None:
    result = to_telegram_photo(_webp_bytes())
    assert result is not None
    assert result.startswith(bytes.fromhex("ffd8ff"))


def test_broken_image_is_rejected() -> None:
    assert to_telegram_photo(b"not an image at all") is None


def test_filename_is_sanitised() -> None:
    assert safe_filename("../../секрет.pdf") == "секрет.pdf"
    assert safe_filename(None) == "file.bin"


def test_human_size() -> None:
    assert human_size(512) == "512 Б"
    assert human_size(2 * 1024 * 1024) == "2.0 МиБ"
    assert human_size(None) == "?"


def test_gzip_lottie_passes_through_as_tgs() -> None:
    """Анимация MAX уже сжата gzip — это готовый .tgs, трогать её не нужно."""
    import gzip

    data = gzip.compress(b'{"v":"5.5","w":512,"h":512,"layers":[]}')
    assert to_telegram_sticker(data) is data


def test_static_sticker_is_resized_to_webp() -> None:
    """Статичный стикер приводится к WEBP со стороной 512."""
    import io

    from PIL import Image

    result = to_telegram_sticker(_png_bytes())
    assert result is not None
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "WEBP"
        assert max(image.size) == 512


def test_plain_lottie_json_is_packed_into_tgs() -> None:
    """Распакованный Lottie заворачивается обратно в gzip — это и есть .tgs."""
    import gzip

    payload = b'{"v":"5.5","w":512,"h":512,"fr":60,"layers":[]}'
    result = to_telegram_sticker(payload)
    assert result is not None
    assert result.startswith(bytes.fromhex("1f8b"))
    assert gzip.decompress(result) == payload
