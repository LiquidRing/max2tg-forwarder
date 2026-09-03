"""Вспомогательные функции по работе с файлами и вложениями."""

from __future__ import annotations

import gzip
import io
import logging
import mimetypes
import re
from typing import Final

logger = logging.getLogger("media")

_UNSAFE: Final = re.compile(r"[^\w.\- ]+", re.UNICODE)

#: Сигнатуры форматов, которые Telegram принимает без преобразования.
JPEG_MAGIC: Final = bytes.fromhex("ffd8ff")
PNG_MAGIC: Final = bytes.fromhex("89504e470d0a1a0a")

#: Сигнатура gzip: анимированные стикеры приходят Lottie-JSON под сжатием.
GZIP_MAGIC: Final = bytes.fromhex("1f8b")

#: Telegram принимает стикером картинку со стороной ровно 512 пикселей.
STICKER_SIDE: Final = 512


def safe_filename(name: str | None, default: str = "file.bin") -> str:
    """Привести имя файла к безопасному виду."""
    if not name:
        return default
    cleaned = _UNSAFE.sub("_", name.strip()).strip("._ ")
    return cleaned[:120] or default


def guess_extension(mime_type: str | None, default: str = ".bin") -> str:
    """Расширение по MIME-типу."""
    if not mime_type:
        return default
    if mime_type == "image/jpeg":
        return ".jpg"
    return mimetypes.guess_extension(mime_type) or default


def guess_mime(filename: str, default: str = "application/octet-stream") -> str:
    """MIME-тип по имени файла."""
    return mimetypes.guess_type(filename)[0] or default


def human_size(size: int | None) -> str:
    """Размер в человекочитаемом виде."""
    if size is None:
        return "?"
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if value < 1024 or unit == "ГиБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГиБ"


def to_telegram_photo(data: bytes) -> bytes | None:
    """Привести картинку к виду, который Telegram примет как аватар.

    JPEG и PNG проходят как есть; остальное (в том числе WEBP, который MAX
    отдаёт для части чатов) конвертируется, если доступен Pillow.
    """
    if data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC):
        return data
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow недоступен — картинка неподдерживаемого формата пропущена")
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()
    except Exception:
        logger.warning("Не удалось преобразовать картинку в JPEG", exc_info=True)
        return None


def to_telegram_sticker(data: bytes) -> bytes | None:
    """Привести картинку к формату стикера Telegram: WEBP со стороной 512.

    MAX отдаёт стикеры обычными PNG, а Bot API принимает стикером только WEBP
    нужного размера — иначе картинка уйдёт как обычное фото.
    """
    if data.startswith(GZIP_MAGIC):
        # Анимация Lottie в gzip — это готовый .tgs, Telegram примет её как есть.
        return data
    stripped = data.lstrip()
    if stripped.startswith(b"{"):
        # aiohttp прозрачно распаковывает Content-Encoding, поэтому Lottie
        # доезжает обычным JSON. Формат .tgs — тот же JSON под gzip.
        return gzip.compress(stripped)
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                return None
            width, height = image.size
            if not width or not height:
                return None
            scale = STICKER_SIDE / max(width, height)
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            buffer = io.BytesIO()
            resample = Image.Resampling.LANCZOS
            image.convert("RGBA").resize(size, resample).save(buffer, format="WEBP")
            return buffer.getvalue()
    except Exception:
        logger.warning("Не удалось подготовить стикер", exc_info=True)
        return None


def webp_to_png(data: bytes) -> bytes | None:
    """Сконвертировать статичный стикер WEBP в PNG.

    MAX не понимает WEBP, поэтому статичные стикеры Telegram переносятся как
    картинки PNG. Если Pillow недоступен или кадр анимированный — None.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                return None
            buffer = io.BytesIO()
            image.convert("RGBA").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:
        return None


def render_qr_png(url: str) -> bytes | None:
    """Нарисовать QR-код ссылки авторизации MAX.

    Пользователь подключает аккаунт из переписки с ботом, поэтому код нужен
    картинкой, а не ASCII-графикой в консоли сервера.
    """
    try:
        import qrcode
    except ImportError:
        return None
    try:
        code = qrcode.QRCode(box_size=8, border=2)
        code.add_data(url)
        code.make(fit=True)
        buffer = io.BytesIO()
        code.make_image().save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        logger.warning("Не удалось нарисовать QR-код", exc_info=True)
        return None
