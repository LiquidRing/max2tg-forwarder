"""Общий интерфейс адаптера платформы."""

from __future__ import annotations

from typing import Protocol

from ..models import NormalizedMessage, Platform, RemoteChat


class PlatformAdapter(Protocol):
    """Адаптер умеет доставлять нормализованные сообщения в свою платформу.

    Адаптеры не знают друг о друге: связывает их только :mod:`max2tg.bridge`.
    """

    platform: Platform

    async def deliver(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        reply_to_target_id: str | None,
    ) -> list[str]:
        """Отправить сообщение и вернуть идентификаторы созданных сообщений."""
        ...

    async def edit(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        target_message_id: str,
    ) -> bool:
        """Отредактировать ранее доставленное сообщение. False — если не вышло."""
        ...

    async def send_service(self, chat_id: int, text: str) -> None:
        """Отправить служебное уведомление моста."""
        ...


class RemoteDirectory(Protocol):
    """Справочник чатов удалённой платформы.

    Нужен командам управления в Telegram, чтобы показать список чатов MAX и
    проверить существование чата при привязке. Через этот протокол адаптеры
    остаются независимыми друг от друга.
    """

    async def list_chats(self, account_id: int, query: str | None = None) -> list[RemoteChat]:
        """Список чатов аккаунта, опционально отфильтрованный по подстроке."""
        ...

    async def resolve_chat(self, account_id: int, chat_id: int) -> RemoteChat | None:
        """Найти чат аккаунта по идентификатору."""
        ...

    async def fetch_avatar(self, account_id: int, chat: RemoteChat) -> bytes | None:
        """Скачать картинку чата, если она есть."""
        ...

    async def import_history(self, account_id: int, chat_id: int, limit: int) -> int:
        """Передать мосту последние сообщения чата. Возвращает их количество."""
        ...
