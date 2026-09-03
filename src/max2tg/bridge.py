"""Оркестратор: принимает нормализованные сообщения и отдаёт их второй платформе."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .adapters.base import PlatformAdapter
from .models import NormalizedMessage, Platform
from .storage import Storage

logger = logging.getLogger("bridge")

#: Размер очереди на один чат. При переполнении сообщения не теряются —
#: продюсер ждёт, тем самым притормаживая чтение с быстрой стороны.
QUEUE_SIZE = 100


class Bridge:
    """Связующее звено между адаптерами.

    Каждому чату-источнику соответствует своя очередь и свой воркер: порядок
    сообщений внутри чата сохраняется, а медленная загрузка вложения в одном
    чате не тормозит остальные.
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._adapters: dict[Platform, PlatformAdapter] = {}
        self._queues: dict[tuple[Platform, int, int], asyncio.Queue[NormalizedMessage]] = {}
        self._workers: dict[tuple[Platform, int, int], asyncio.Task[None]] = {}
        self._closing = False

    def register(self, adapter: PlatformAdapter) -> None:
        """Зарегистрировать адаптер платформы."""
        self._adapters[adapter.platform] = adapter

    async def submit(self, message: NormalizedMessage) -> None:
        """Поставить сообщение в очередь на пересылку."""
        if self._closing:
            return
        key = (message.source, message.account_id, message.source_chat_id)
        queue = self._queues.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=QUEUE_SIZE)
            self._queues[key] = queue
            self._workers[key] = asyncio.create_task(
                self._worker(queue), name=f"bridge-{message.source.value}-{key[2]}"
            )
        await queue.put(message)

    async def close(self) -> None:
        """Дождаться разбора очередей и остановить воркеры."""
        self._closing = True
        for queue in self._queues.values():
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(queue.join(), timeout=10)
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

    # --------------------------------------------------------------------- #

    async def _worker(self, queue: asyncio.Queue[NormalizedMessage]) -> None:
        while True:
            message = await queue.get()
            try:
                await self._process(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] не удалось переслать сообщение", message.trace_id)
                await self._report_failure(message)
            finally:
                queue.task_done()

    async def _process(self, message: NormalizedMessage) -> None:
        route = await self._resolve_route(message)
        if route is None:
            return
        target_platform, target_chat_id = route
        adapter = self._adapters.get(target_platform)
        if adapter is None:
            logger.error("Нет адаптера для платформы %s", target_platform)
            return

        reply_to = await self._resolve_reply(message)

        if message.is_edit:
            existing = await self._find_counterpart(message)
            if existing is not None and await adapter.edit(message, target_chat_id, existing):
                logger.info(
                    "[%s] правка %s -> %s", message.trace_id, message.source.value, existing
                )
                return
            message.notes.append("сообщение изменено в источнике")

        if message.is_empty:
            return

        # Повторный push того же сообщения (например, после переподключения)
        # не должен приводить ко второй копии в чате.
        if await self._find_counterpart(message) is not None:
            logger.info(
                "[%s] пропускаем повтор %s#%s",
                message.trace_id,
                message.source.value,
                message.source_message_id,
            )
            return

        delivered = await adapter.deliver(message, target_chat_id, reply_to)
        logger.info(
            "[%s] %s#%s -> %s: %d сообщ., %d влож.",
            message.trace_id,
            message.source.value,
            message.source_message_id,
            target_platform.value,
            len(delivered),
            len(message.attachments),
        )
        await self._remember(message, target_chat_id, delivered)

    async def _resolve_route(self, message: NormalizedMessage) -> tuple[Platform, int] | None:
        if message.source is Platform.MAX:
            binding = await self._storage.get_by_max(message.source_chat_id, message.account_id)
            if binding is None or not binding.enabled:
                return None
            return Platform.TELEGRAM, binding.tg_chat_id
        binding = await self._storage.get_by_tg(message.source_chat_id)
        if binding is None or not binding.enabled:
            return None
        # Обратный маршрут несёт аккаунт получателя: адаптер MAX по нему
        # выбирает, через какую сессию отправлять.
        message.account_id = binding.account_id
        return Platform.MAX, binding.max_chat_id

    async def _resolve_reply(self, message: NormalizedMessage) -> str | None:
        if not message.reply_to_source_id:
            return None
        return await self._lookup_counterpart(message, message.reply_to_source_id)

    async def _find_counterpart(self, message: NormalizedMessage) -> str | None:
        """Найти идентификатор того же сообщения на стороне получателя."""
        return await self._lookup_counterpart(message, message.source_message_id)

    async def _lookup_counterpart(self, message: NormalizedMessage, source_id: str) -> str | None:
        if message.source is Platform.MAX:
            tg_id = await self._storage.find_tg_message(
                message.source_chat_id, source_id, message.account_id
            )
            return str(tg_id) if tg_id is not None else None
        try:
            tg_message_id = int(source_id)
        except ValueError:
            return None
        return await self._storage.find_max_message(message.source_chat_id, tg_message_id)

    async def _remember(
        self, message: NormalizedMessage, target_chat_id: int, delivered: list[str]
    ) -> None:
        for target_id in delivered:
            if message.source is Platform.MAX:
                await self._storage.remember(
                    tg_chat_id=target_chat_id,
                    tg_message_id=int(target_id),
                    max_chat_id=message.source_chat_id,
                    max_message_id=message.source_message_id,
                    direction="max2tg",
                    account_id=message.account_id,
                )
            else:
                await self._storage.remember(
                    tg_chat_id=message.source_chat_id,
                    tg_message_id=int(message.source_message_id),
                    max_chat_id=target_chat_id,
                    max_message_id=target_id,
                    direction="tg2max",
                    account_id=message.account_id,
                )

    async def _report_failure(self, message: NormalizedMessage) -> None:
        """Сообщить о сбое в группу Telegram — она же интерфейс пользователя."""
        telegram = self._adapters.get(Platform.TELEGRAM)
        if telegram is None:
            return
        if message.source is Platform.MAX:
            binding = await self._storage.get_by_max(message.source_chat_id, message.account_id)
            chat_id = binding.tg_chat_id if binding else None
        else:
            chat_id = message.source_chat_id
        if chat_id is None:
            return
        with suppress(Exception):
            await telegram.send_service(
                chat_id,
                f"⚠️ Не удалось переслать сообщение (трассировка {message.trace_id}). "
                "Подробности — в логах моста.",
            )
