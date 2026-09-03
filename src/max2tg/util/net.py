"""Определение прокси для обеих Telegram-сессий.

Ни `aiohttp` (Bot API), ни Telethon (MTProto) не читают системные настройки
прокси сами, поэтому адрес берётся из конфигурации моста или из окружения.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("net")

#: Размер блока при чтении тела ответа.
CHUNK_SIZE = 64 * 1024

#: Переменные окружения, в которых обычно лежит адрес прокси.
PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

#: Схема URL -> тип прокси в терминах python-socks.
PROXY_SCHEMES = {
    "http": "http",
    "https": "http",
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
}


def resolve_proxy_url(configured: str | None) -> str | None:
    """Адрес прокси: сначала настройка моста, затем окружение."""
    if configured:
        return configured
    for name in PROXY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def parse_proxy(url: str | None) -> dict[str, Any] | None:
    """Разобрать URL прокси в словарь, который понимает Telethon.

    Telethon работает по MTProto поверх TCP, поэтому ему нужен не URL, а
    описание прокси в формате python-socks.
    """
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"http://{url}")
    proxy_type = PROXY_SCHEMES.get(parsed.scheme.lower())
    if proxy_type is None:
        logger.warning("Неизвестная схема прокси %r — Telethon пойдёт напрямую", parsed.scheme)
        return None
    if not parsed.hostname or not parsed.port:
        logger.warning("В адресе прокси %r нет хоста или порта", url)
        return None
    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": int(parsed.port),
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


async def read_limited(response: Any, limit: int) -> bytes | None:
    """Прочитать тело ответа целиком, но не больше ``limit`` байт.

    ``response.content.read(n)`` отдаёт лишь то, что уже пришло в буфер, поэтому
    для файлов его использовать нельзя: получаются обрезанные данные, которые
    выглядят валидными по сигнатуре, но не открываются декодером.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
