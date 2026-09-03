"""Простое ограничение частоты запросов к внешним API."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Ограничитель «не чаще N раз в секунду» — глобально и на каждый чат.

    Реализован как минимальный интервал между вызовами: этого достаточно,
    чтобы не упираться в 429 и flood wait обеих платформ.
    """

    def __init__(self, global_rate: float, per_chat_rate: float) -> None:
        self._global_interval = 1.0 / global_rate if global_rate > 0 else 0.0
        self._chat_interval = 1.0 / per_chat_rate if per_chat_rate > 0 else 0.0
        self._global_lock = asyncio.Lock()
        self._global_next = 0.0
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._chat_next: defaultdict[int, float] = defaultdict(float)

    async def acquire(self, chat_id: int) -> None:
        """Дождаться разрешения на отправку в указанный чат."""
        if self._chat_interval:
            async with self._chat_locks[chat_id]:
                await self._wait(self._chat_next, chat_id, self._chat_interval)
        if self._global_interval:
            async with self._global_lock:
                now = time.monotonic()
                delay = self._global_next - now
                if delay > 0:
                    await asyncio.sleep(delay)
                    now = time.monotonic()
                self._global_next = now + self._global_interval

    @staticmethod
    async def _wait(table: defaultdict[int, float], key: int, interval: float) -> None:
        now = time.monotonic()
        delay = table[key] - now
        if delay > 0:
            await asyncio.sleep(delay)
            now = time.monotonic()
        table[key] = now + interval
