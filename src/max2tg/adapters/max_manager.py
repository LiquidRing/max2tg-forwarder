"""Менеджер аккаунтов MAX: несколько сессий в одном процессе.

Мост обслуживает многих пользователей, и у каждого может быть несколько
аккаунтов MAX. Менеджер держит по одной сессии на аккаунт и подставляет нужную
всякий раз, когда мосту или команде требуется работа «от имени» аккаунта.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ..config import Settings
from ..models import NormalizedMessage, Platform, RemoteChat
from ..storage import Storage
from .max_adapter import MaxAdapter

logger = logging.getLogger("max-manager")


class MaxAccountManager:
    """Пул сессий MAX, общий для всех пользователей моста.

    Для моста это обычный адаптер платформы: он не знает, что за ним стоит
    несколько аккаунтов.
    """

    platform = Platform.MAX

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        on_message: Callable[[NormalizedMessage], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._on_message = on_message
        self._sessions: dict[int, MaxAdapter] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        #: Сессии, которые ещё входят в MAX (ждут сканирования QR-кода).
        self._pending: dict[int, MaxAdapter] = {}
        self._on_lost: Callable[[int], Awaitable[None]] | None = None

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #

    async def start_all(self) -> int:
        """Поднять сессии всех аккаунтов, у которых есть сохранённый токен."""
        await self._purge_unfinished()
        started = 0
        for account in await self._storage.list_accounts():
            if not account.enabled or not account.token:
                continue
            token = await self._storage.account_token(account.id)
            if not token:
                logger.warning(
                    "Токен аккаунта %s не читается — пропускаем (проверьте MASTER_KEY)",
                    account.id,
                )
                continue
            try:
                await self.start_account(account.id, token, account.nickname)
                started += 1
            except Exception:
                logger.exception("Не удалось поднять аккаунт MAX %s", account.id)
        return started

    async def _purge_unfinished(self) -> None:
        """Убрать аккаунты, вход в которые так и не завершили.

        Строка заводится в момент /max_add, а токен появляется только после
        сканирования QR-кода. Брошенный вход оставлял запись без токена: она
        занимала слот из ``MAX_ACCOUNTS_PER_USER`` и ничего не делала. Аккаунты
        с привязками не трогаем — у них просто нет токена (нужен повторный вход).
        """
        for account in await self._storage.list_accounts():
            if account.token or await self._storage.list_bindings(account.id):
                continue
            await self._storage.remove_account(account.id)
            logger.info(
                "Убран недоведённый аккаунт %s (%s): вход в MAX не был завершён",
                account.id,
                account.nickname,
            )

    async def start_account(
        self,
        account_id: int,
        token: str | None,
        nickname: str = "MAX",
        qr_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> MaxAdapter:
        """Подключить один аккаунт и начать слушать его события."""
        existing = self._sessions.get(account_id)
        if existing is not None:
            return existing

        session = MaxAdapter(
            self._settings,
            self._storage,
            self._on_message,
            account_id=account_id,
            token=token,
            nickname=nickname,
            password=await self._password_for(account_id),
        )
        session.set_qr_callback(qr_callback)
        # Пока вход не завершён, сессию нужно уметь остановить снаружи: иначе
        # брошенный вход повторяет попытки, пока не перезапустят мост.
        self._pending[account_id] = session
        try:
            await session.start()
        finally:
            self._pending.pop(account_id, None)
        session.set_qr_callback(None)

        self._sessions[account_id] = session
        self._tasks[account_id] = asyncio.create_task(
            self._run(account_id, session), name=f"max-account-{account_id}"
        )
        logger.info("Аккаунт MAX %s (%s) подключён", account_id, session.nickname)
        return session

    async def abort_login(self, account_id: int) -> None:
        """Прервать вход, который так и не завершился, и закрыть его соединение."""
        session = self._pending.pop(account_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.stop(), timeout=10.0)

    async def _password_for(self, account_id: int) -> str | None:
        """Пароль двухфакторной защиты MAX для аккаунта.

        ``MAX_PASSWORD`` из .env — пароль аккаунта владельца моста, поэтому он
        подходит только аккаунтам администраторов. Остальным его показывать
        нельзя: MAX получил бы чужой пароль при входе в чужой аккаунт.
        """
        if not self._settings.max_password:
            return None
        account = await self._storage.get_account(account_id)
        if account is not None and account.owner_id in self._settings.tg_admin_ids:
            return self._settings.max_password
        return None

    def set_lost_handler(self, handler: Callable[[int], Awaitable[None]] | None) -> None:
        """Кому сообщить, что сессия аккаунта оборвалась и сама не поднимется."""
        self._on_lost = handler

    async def _run(self, account_id: int, session: MaxAdapter) -> None:
        """Слушать события аккаунта, сообщая, если слушать больше некому.

        Обрывы сокета pyromax чинит сам; сюда управление возвращается, только
        когда цикл событий закончился совсем. Раньше это оставалось незамеченным:
        аккаунт молча переставал работать до перезапуска всего моста.
        """
        try:
            await session.run()
            logger.warning("Сессия аккаунта MAX %s закончила слушать события", account_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сессия аккаунта MAX %s завершилась ошибкой", account_id)

        # Дальше — только если сессию никто не останавливал намеренно.
        if self._sessions.get(account_id) is not session:
            return
        self._sessions.pop(account_id, None)
        self._tasks.pop(account_id, None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.stop(), timeout=10.0)
        if self._on_lost is not None:
            with contextlib.suppress(Exception):
                await self._on_lost(account_id)

    async def reconnect_account(self, account_id: int) -> MaxAdapter:
        """Поднять сессию заново по сохранённому токену — как при запуске моста."""
        account = await self._storage.get_account(account_id)
        token = await self._storage.account_token(account_id)
        if account is None or not token:
            raise RuntimeError("нет сохранённого токена — подключите аккаунт заново: /max_add")

        await self.stop_account(account_id)
        try:
            # Просроченный токен превращает вход в ожидание QR-кода, которого никто
            # не увидит, поэтому ждём ограниченное время и вход закрываем.
            return await asyncio.wait_for(
                self.start_account(account_id, token, account.nickname), timeout=90.0
            )
        except TimeoutError:
            await self.abort_login(account_id)
            raise RuntimeError(
                "MAX не принял сохранённый вход — подключите аккаунт заново: /max_add"
            ) from None

    async def stop_account(self, account_id: int) -> None:
        """Отключить аккаунт и освободить его сессию.

        Сначала останавливается сама сессия (иначе pyromax переподключит её
        после отмены задачи), затем задача слушателя — но не дольше положенного:
        библиотека умеет проглатывать отмену, а зависшая остановка блокирует
        команду, из которой её вызвали.
        """
        task = self._tasks.pop(account_id, None)
        session = self._sessions.pop(account_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.stop(), timeout=15.0)
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=10.0)

    async def stop(self) -> None:
        """Остановить все сессии."""
        for account_id in list(self._sessions):
            await self.stop_account(account_id)

    def session(self, account_id: int) -> MaxAdapter | None:
        """Живая сессия аккаунта, если она поднята."""
        return self._sessions.get(account_id)

    @property
    def active_accounts(self) -> list[int]:
        """Идентификаторы подключённых аккаунтов."""
        return list(self._sessions)

    # ------------------------------------------------------------------ #
    # Работа «от имени» аккаунта
    # ------------------------------------------------------------------ #

    async def deliver(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        reply_to_target_id: str | None,
    ) -> list[str]:
        """Отправить сообщение в MAX через сессию нужного аккаунта."""
        session = self._require(message.account_id)
        return await session.deliver(message, target_chat_id, reply_to_target_id)

    async def edit(
        self, message: NormalizedMessage, target_chat_id: int, target_message_id: str
    ) -> bool:
        """Отредактировать сообщение в MAX через сессию нужного аккаунта."""
        session = self._sessions.get(message.account_id)
        if session is None:
            return False
        return await session.edit(message, target_chat_id, target_message_id)

    async def send_service(self, chat_id: int, text: str) -> None:
        """Служебное сообщение — через любую живую сессию."""
        for session in self._sessions.values():
            await session.send_service(chat_id, text)
            return

    async def list_chats(self, account_id: int, query: str | None = None) -> list[RemoteChat]:
        """Чаты конкретного аккаунта."""
        return await self._require(account_id).list_chats(query)

    async def resolve_chat(self, account_id: int, chat_id: int) -> RemoteChat | None:
        """Найти чат в конкретном аккаунте."""
        return await self._require(account_id).resolve_chat(chat_id)

    async def fetch_avatar(self, account_id: int, chat: RemoteChat) -> bytes | None:
        """Скачать картинку чата средствами нужного аккаунта."""
        session = self._sessions.get(account_id)
        return await session.fetch_avatar(chat) if session else None

    async def import_history(self, account_id: int, chat_id: int, limit: int) -> int:
        """Перенести историю чата из нужного аккаунта."""
        session = self._sessions.get(account_id)
        return await session.import_history(chat_id, limit) if session else 0

    def _require(self, account_id: int) -> MaxAdapter:
        session = self._sessions.get(account_id)
        if session is None:
            raise RuntimeError(f"Аккаунт MAX {account_id} не подключён")
        return session
