"""Настройка логирования."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Сконфигурировать корневой логгер."""
    _force_utf8_output()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Библиотеки слишком многословны на DEBUG.
    for noisy in ("aiosqlite", "websockets.client", "aiohttp.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _force_utf8_output() -> None:
    """Перевести stdout/stderr в UTF-8.

    На Windows консоль по умолчанию в cp1251: эмодзи в сообщениях моста и
    блочные символы QR-кода иначе роняют вывод с UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # поток без поддержки перенастройки
            continue
