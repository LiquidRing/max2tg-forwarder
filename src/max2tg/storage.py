"""Слой доступа к состоянию: привязки чатов и соответствия сообщений."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import SecretBox
from .db import Binding, MaxAccount, MessageMap, TelegramSession


class Storage:
    """Репозиторий поверх БД состояния."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secrets: SecretBox | None = None,
    ) -> None:
        self._sessions = session_factory
        self._secrets = secrets or SecretBox(None)

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        """Фабрика сессий — нужна миграции для точечных обновлений."""
        return self._sessions

    # ---------- аккаунты MAX ----------

    async def add_account(self, owner_id: int, nickname: str) -> MaxAccount:
        """Завести аккаунт MAX для пользователя (до авторизации)."""
        async with self._sessions() as session, session.begin():
            account = MaxAccount(owner_id=owner_id, nickname=nickname, enabled=True)
            session.add(account)
            await session.flush()
            await session.refresh(account)
            return account

    async def save_account_token(
        self,
        account_id: int,
        token: str | None,
        max_user_id: int | None = None,
        nickname: str | None = None,
        phone: str | None = None,
    ) -> None:
        """Сохранить данные авторизованного аккаунта; токен шифруется."""
        async with self._sessions() as session, session.begin():
            account = await session.get(MaxAccount, account_id)
            if account is None:
                return
            if token is not None:
                account.token = self._secrets.encrypt(token)
            if max_user_id is not None:
                account.max_user_id = max_user_id
            if nickname:
                account.nickname = nickname
            if phone:
                account.phone = phone

    async def get_account(self, account_id: int) -> MaxAccount | None:
        """Аккаунт по идентификатору."""
        async with self._sessions() as session:
            return await session.get(MaxAccount, account_id)

    async def account_token(self, account_id: int) -> str | None:
        """Расшифрованный токен аккаунта."""
        account = await self.get_account(account_id)
        return self._secrets.decrypt(account.token) if account else None

    async def list_accounts(self, owner_id: int | None = None) -> list[MaxAccount]:
        """Аккаунты пользователя или все сразу (для запуска моста)."""
        async with self._sessions() as session:
            query = select(MaxAccount).order_by(MaxAccount.id)
            if owner_id is not None:
                query = query.where(MaxAccount.owner_id == owner_id)
            return list(await session.scalars(query))

    async def claim_orphan_accounts(self, owner_id: int) -> list[int]:
        """Закрепить за пользователем аккаунты, оставшиеся без владельца.

        Так выглядит наследство одиночной установки: аккаунт есть, а кому он
        принадлежит — неизвестно, потому что раньше владелец был один.
        """
        async with self._sessions() as session, session.begin():
            orphans = list(
                await session.scalars(select(MaxAccount).where(MaxAccount.owner_id == 0))
            )
            for account in orphans:
                account.owner_id = owner_id
            return [int(account.id) for account in orphans]

    async def remove_account(self, account_id: int) -> bool:
        """Удалить аккаунт вместе с его привязками."""
        async with self._sessions() as session, session.begin():
            account = await session.get(MaxAccount, account_id)
            if account is None:
                return False
            await session.execute(delete(Binding).where(Binding.account_id == account_id))
            await session.execute(delete(MessageMap).where(MessageMap.account_id == account_id))
            await session.delete(account)
            return True

    # ---------- пользовательская сессия Telegram ----------

    async def save_tg_session(self, owner_id: int, session_string: str | None) -> None:
        """Сохранить строку сессии Telethon (в зашифрованном виде)."""
        async with self._sessions() as session, session.begin():
            record = await session.get(TelegramSession, owner_id)
            encrypted = self._secrets.encrypt(session_string)
            if record is None:
                session.add(TelegramSession(owner_id=owner_id, session=encrypted))
            else:
                record.session = encrypted

    async def get_tg_session(self, owner_id: int) -> str | None:
        """Строка сессии Telethon пользователя."""
        async with self._sessions() as session:
            record = await session.get(TelegramSession, owner_id)
            return self._secrets.decrypt(record.session) if record else None

    # ---------- привязки ----------

    async def bind(
        self,
        tg_chat_id: int,
        max_chat_id: int,
        title: str | None,
        icon_url: str | None = None,
        account_id: int = 0,
    ) -> None:
        """Привязать группу Telegram к чату MAX (перезаписывая прежние связи)."""
        async with self._sessions() as session, session.begin():
            await session.execute(delete(Binding).where(Binding.tg_chat_id == tg_chat_id))
            # Идентификатор чата уникален лишь внутри аккаунта, поэтому старая
            # связь ищется по паре «аккаунт + чат».
            await session.execute(
                delete(Binding).where(
                    Binding.account_id == account_id,
                    Binding.max_chat_id == max_chat_id,
                )
            )
            session.add(
                Binding(
                    tg_chat_id=tg_chat_id,
                    account_id=account_id,
                    max_chat_id=max_chat_id,
                    title=title,
                    icon_url=icon_url,
                    enabled=True,
                )
            )

    async def remember_icon(self, tg_chat_id: int, icon_url: str | None) -> None:
        """Запомнить, какая картинка уже стоит аватаром группы."""
        await self.update_binding(tg_chat_id, icon_url=icon_url)

    async def update_binding(
        self,
        tg_chat_id: int,
        title: str | None = None,
        icon_url: str | None = None,
    ) -> None:
        """Обновить сохранённые сведения о группе."""
        async with self._sessions() as session, session.begin():
            binding = await session.scalar(select(Binding).where(Binding.tg_chat_id == tg_chat_id))
            if binding is None:
                return
            if title is not None:
                binding.title = title
            if icon_url is not None:
                binding.icon_url = icon_url

    async def set_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        """Приостановить или возобновить пересылку, сохранив привязку."""
        async with self._sessions() as session, session.begin():
            binding = await session.scalar(select(Binding).where(Binding.tg_chat_id == tg_chat_id))
            if binding is None:
                return False
            binding.enabled = enabled
            return True

    async def unbind(self, tg_chat_id: int) -> bool:
        """Снять привязку с группы Telegram. Возвращает True, если связь была."""
        async with self._sessions() as session, session.begin():
            existing = await session.scalar(select(Binding).where(Binding.tg_chat_id == tg_chat_id))
            if existing is None:
                return False
            await session.delete(existing)
            return True

    async def get_by_tg(self, tg_chat_id: int) -> Binding | None:
        """Найти привязку по чату Telegram."""
        async with self._sessions() as session:
            return await session.scalar(select(Binding).where(Binding.tg_chat_id == tg_chat_id))

    async def get_by_max(self, max_chat_id: int, account_id: int = 0) -> Binding | None:
        """Найти привязку по чату MAX конкретного аккаунта."""
        async with self._sessions() as session:
            return await session.scalar(
                select(Binding).where(
                    Binding.account_id == account_id,
                    Binding.max_chat_id == max_chat_id,
                )
            )

    async def list_bindings(self, account_id: int | None = None) -> list[Binding]:
        """Привязки аккаунта или все существующие."""
        async with self._sessions() as session:
            query = select(Binding).order_by(Binding.created_at)
            if account_id is not None:
                query = query.where(Binding.account_id == account_id)
            return list(await session.scalars(query))

    # ---------- соответствия сообщений ----------

    async def remember(
        self,
        *,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
        max_message_id: str,
        direction: str,
        account_id: int = 0,
    ) -> None:
        """Запомнить пару идентификаторов одного и того же сообщения.

        Стирается только точно такая же пара: у длинного сообщения Telegram
        частей в MAX несколько, и затирать их друг другом нельзя.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(MessageMap).where(
                    MessageMap.tg_chat_id == tg_chat_id,
                    MessageMap.tg_message_id == tg_message_id,
                    MessageMap.max_message_id == str(max_message_id),
                )
            )
            session.add(
                MessageMap(
                    tg_chat_id=tg_chat_id,
                    tg_message_id=tg_message_id,
                    account_id=account_id,
                    max_chat_id=max_chat_id,
                    max_message_id=str(max_message_id),
                    direction=direction,
                )
            )

    async def find_tg_message(
        self, max_chat_id: int, max_message_id: str, account_id: int = 0
    ) -> int | None:
        """Первое сообщение Telegram, соответствующее сообщению MAX."""
        async with self._sessions() as session:
            return await session.scalar(
                select(MessageMap.tg_message_id)
                .where(
                    MessageMap.account_id == account_id,
                    MessageMap.max_chat_id == max_chat_id,
                    MessageMap.max_message_id == str(max_message_id),
                )
                .order_by(MessageMap.id)
                .limit(1)
            )

    async def find_max_message(self, tg_chat_id: int, tg_message_id: int) -> str | None:
        """Сообщение MAX, соответствующее сообщению Telegram."""
        async with self._sessions() as session:
            return await session.scalar(
                select(MessageMap.max_message_id).where(
                    MessageMap.tg_chat_id == tg_chat_id,
                    MessageMap.tg_message_id == tg_message_id,
                )
            )

    async def count_messages(self, tg_chat_id: int) -> int:
        """Сколько сообщений мост уже перенёс в эту группу и из неё."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(MessageMap.id).where(MessageMap.tg_chat_id == tg_chat_id)
            )
            return len(list(rows))

    async def count_incoming_since(self, max_chat_id: int, since: int, account_id: int = 0) -> int:
        """Сколько сообщений пришло из MAX в этот чат начиная с указанного момента.

        Нужно самопроверке: направление MAX → Telegram подтверждается только
        сообщением, отправленным другим устройством, — своё сервер не пушит.
        """
        async with self._sessions() as session:
            rows = await session.scalars(
                select(MessageMap.id).where(
                    MessageMap.account_id == account_id,
                    MessageMap.max_chat_id == max_chat_id,
                    MessageMap.direction == "max2tg",
                    MessageMap.created_at >= since,
                )
            )
            return len(list(rows))

    async def is_mirrored_from_tg(
        self, max_chat_id: int, max_message_id: str, account_id: int = 0
    ) -> bool:
        """Это сообщение MAX создано самим мостом (эхо из Telegram)?"""
        async with self._sessions() as session:
            found = await session.scalar(
                select(MessageMap.id)
                .where(
                    MessageMap.account_id == account_id,
                    MessageMap.max_chat_id == max_chat_id,
                    MessageMap.max_message_id == str(max_message_id),
                    MessageMap.direction == "tg2max",
                )
                .limit(1)
            )
            return found is not None
