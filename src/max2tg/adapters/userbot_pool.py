"""Пул пользовательских сессий Telegram.

Автосоздание групп идёт от имени самого пользователя, поэтому сессия у каждого
своя. Строки сессий лежат в базе в зашифрованном виде, а здесь держатся живые
клиенты, чтобы не переподключаться на каждую команду.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..storage import Storage
from .telegram_userbot import TelegramUserbot

logger = logging.getLogger("userbot-pool")


class UserbotPool:
    """Сессии Telegram по владельцам."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self._settings = settings
        self._storage = storage
        self._bots: dict[int, TelegramUserbot] = {}

    @property
    def configured(self) -> bool:
        """Заданы ли api_id и api_hash — без них вход невозможен."""
        return bool(self._settings.tg_api_id and self._settings.tg_api_hash)

    async def get(self, owner_id: int) -> TelegramUserbot:
        """Сессия пользователя: из памяти либо поднятая из базы."""
        existing = self._bots.get(owner_id)
        if existing is not None:
            return existing

        session_string = await self._storage.get_tg_session(owner_id)

        async def remember(value: str) -> None:
            await self._storage.save_tg_session(owner_id, value)
            logger.info("Сессия Telegram пользователя %s сохранена", owner_id)

        bot = TelegramUserbot(self._settings, session_string, remember)
        self._bots[owner_id] = bot
        return bot

    async def authorized(self, owner_id: int) -> TelegramUserbot | None:
        """Готовая к работе сессия пользователя или ``None``."""
        if not self.configured:
            return None
        bot = await self.get(owner_id)
        return bot if await bot.is_authorized() else None

    async def forget(self, owner_id: int) -> None:
        """Забыть сессию пользователя (выход)."""
        bot = self._bots.pop(owner_id, None)
        if bot is not None:
            await bot.disconnect()
        await self._storage.save_tg_session(owner_id, None)

    async def stop(self) -> None:
        """Закрыть все живые сессии."""
        for bot in list(self._bots.values()):
            await bot.disconnect()
        self._bots.clear()
