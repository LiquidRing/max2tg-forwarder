"""Пользовательская сессия Telegram: создание групп и папки моста.

Bot API не умеет ни создавать группы, ни складывать их в папку — это доступно
только клиентскому протоколу. Поэтому команда ``/sync`` работает через отдельную
сессию Telethon, входящую от имени владельца моста.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telethon import TelegramClient, functions, types, utils
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    UserNotMutualContactError,
)
from telethon.sessions import StringSession

from ..config import Settings
from ..util.net import parse_proxy, resolve_proxy_url

logger = logging.getLogger("userbot")

#: Права, которые выдаются боту в созданной группе. Администратор получает все
#: сообщения даже при включённом privacy mode, поэтому это не просто удобство.
BOT_ADMIN_RIGHTS = types.ChatAdminRights(
    change_info=False,
    post_messages=True,
    edit_messages=True,
    delete_messages=True,
    ban_users=False,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    manage_call=False,
    anonymous=False,
    other=True,
)

#: Идентификаторы папок: 0 занят «Все чаты», 1 — архивом.
FOLDER_ID_MIN = 2
FOLDER_ID_MAX = 255

#: Предел длины названия группы в Telegram.
TITLE_LIMIT = 128

#: Название папки Telegram ограничено двенадцатью символами.
FOLDER_TITLE_LIMIT = 12


class UserbotNotConfigured(RuntimeError):
    """Сессия Telegram не настроена или не авторизована."""


@dataclass(slots=True)
class GroupState:
    """Фактическое состояние группы Telegram."""

    title: str
    has_photo: bool


@dataclass(slots=True)
class CreatedGroup:
    """Созданная супергруппа."""

    chat_id: int
    title: str


class TelegramUserbot:
    """Тонкая обёртка над Telethon для задач, недоступных боту."""

    def __init__(
        self,
        settings: Settings,
        session_string: str | None = None,
        on_session_saved: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        #: Сессия конкретного пользователя: в общем сервисе файл на диске
        #: не годится, строка сессии лежит в базе в зашифрованном виде.
        self._session_string = session_string
        self._on_session_saved = on_session_saved
        self._client: TelegramClient | None = None
        self._lock = asyncio.Lock()
        #: Незавершённый вход: номер и выданный сервером хеш кода.
        self._login_phone: str | None = None
        self._code_hash: str | None = None

    @property
    def configured(self) -> bool:
        """Заданы ли api_id и api_hash."""
        return bool(self._settings.tg_api_id and self._settings.tg_api_hash)

    def _build_client(self) -> TelegramClient:
        if not self.configured:
            raise UserbotNotConfigured(
                "Не заданы TG_API_ID и TG_API_HASH — получите их на my.telegram.org"
            )
        proxy_url = resolve_proxy_url(self._settings.tg_userbot_proxy or self._settings.tg_proxy)
        proxy = parse_proxy(proxy_url)
        if proxy:
            logger.info("MTProto через прокси %s:%s", proxy["addr"], proxy["port"])
        return TelegramClient(
            StringSession(self._session_string),
            int(self._settings.tg_api_id or 0),
            str(self._settings.tg_api_hash),
            proxy=proxy,
        )

    async def connect(self) -> TelegramClient:
        """Подключиться существующей сессией (без интерактивного входа)."""
        client = await self._raw_connect()
        if not await client.is_user_authorized():
            raise UserbotNotConfigured(
                "Сессия Telegram не авторизована — выполните /login в личке с ботом "
                "или `uv run max2tg login` в консоли"
            )
        return client

    async def _raw_connect(self) -> TelegramClient:
        """Подключиться, не требуя авторизации: нужно самому процессу входа."""
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            client = self._build_client()
            await client.connect()
            self._client = client
            if await client.is_user_authorized():
                me = await client.get_me()
                logger.info("Пользовательская сессия Telegram: %s", utils.get_display_name(me))
            return client

    async def is_authorized(self) -> bool:
        """Готова ли сессия к работе."""
        if not self.configured:
            return False
        try:
            client = await self._raw_connect()
            return bool(await client.is_user_authorized())
        except Exception:
            logger.debug("Не удалось проверить состояние сессии", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Вход по шагам (используется командой /login)
    # ------------------------------------------------------------------ #

    async def request_code(self, phone: str) -> None:
        """Запросить код подтверждения на указанный номер."""
        client = await self._raw_connect()
        sent = await client.send_code_request(phone)
        self._login_phone = phone
        self._code_hash = sent.phone_code_hash
        logger.info("Код подтверждения отправлен на %s", _mask_phone(phone))

    async def submit_code(self, code: str) -> str:
        """Отправить код. Возвращает "done" или "password" (нужен пароль 2FA)."""
        if not self._login_phone or not self._code_hash:
            raise UserbotNotConfigured("Вход не начат — сначала /login")
        client = await self._raw_connect()
        try:
            await client.sign_in(
                phone=self._login_phone, code=code, phone_code_hash=self._code_hash
            )
        except SessionPasswordNeededError:
            return "password"
        await self._finish_login()
        return "done"

    async def submit_password(self, password: str) -> None:
        """Завершить вход паролем двухфакторной защиты."""
        client = await self._raw_connect()
        await client.sign_in(password=password)
        await self._finish_login()

    async def owner_id(self) -> int | None:
        """Идентификатор Telegram, которому принадлежит сессия."""
        client = await self._raw_connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return int(me.id) if me is not None else None

    async def whoami(self) -> str:
        """Имя владельца авторизованной сессии."""
        client = await self._raw_connect()
        return str(utils.get_display_name(await client.get_me()))

    async def _finish_login(self) -> None:
        """Запомнить успешный вход: строку сессии сохраняет владелец пула."""
        self._login_phone = None
        self._code_hash = None
        if self._client is not None:
            self._session_string = str(self._client.session.save())
            if self._on_session_saved is not None:
                await self._on_session_saved(self._session_string)
        logger.info("Пользовательская сессия Telegram авторизована")

    async def disconnect(self) -> None:
        """Закрыть сессию."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def login_interactive(self) -> str:
        """Интерактивный вход из консоли. Возвращает строку сессии."""
        client = self._build_client()
        await client.start(phone=self._settings.tg_phone)
        me = await client.get_me()
        session_string = str(client.session.save())
        logger.info(
            "Вход выполнен: %s (id=%s)",
            utils.get_display_name(me),
            getattr(me, "id", "?"),
        )
        await client.disconnect()
        self._session_string = session_string
        if self._on_session_saved is not None:
            await self._on_session_saved(session_string)
        return session_string

    # ------------------------------------------------------------------ #
    # Группы
    # ------------------------------------------------------------------ #

    async def create_group(self, title: str, bot_username: str, about: str = "") -> CreatedGroup:
        """Создать супергруппу, пригласить туда бота и выдать ему права админа."""
        client = await self.connect()
        safe_title = title.strip()[:TITLE_LIMIT] or "MAX chat"

        result = await client(
            functions.channels.CreateChannelRequest(
                title=safe_title,
                about=about[:255],
                megagroup=True,
            )
        )
        channel = result.chats[0]
        chat_id = utils.get_peer_id(channel)
        logger.info("Создана группа «%s» (%s)", safe_title, chat_id)

        bot = await client.get_entity(bot_username)
        try:
            await client(functions.channels.InviteToChannelRequest(channel, [bot]))
        except UserNotMutualContactError:
            logger.warning("Бот %s не добавлен в «%s» автоматически", bot_username, safe_title)
        # Права администратора обходят privacy mode: без них бот не увидит
        # сообщения группы и пересылка Telegram -> MAX работать не будет.
        await client(
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=bot,
                admin_rights=BOT_ADMIN_RIGHTS,
                rank="bridge",
            )
        )
        return CreatedGroup(chat_id=chat_id, title=safe_title)

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        markdown: bool = False,
    ) -> int:
        """Написать в чат от имени владельца сессии (используется самопроверкой)."""
        client = await self.connect()
        sent = await client.send_message(
            await client.get_entity(chat_id),
            text,
            reply_to=reply_to,
            parse_mode="md" if markdown else None,
        )
        return int(sent.id)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Отредактировать своё сообщение — проверка переноса правок."""
        client = await self.connect()
        await client.edit_message(await client.get_entity(chat_id), message_id, text)

    async def send_file(
        self,
        chat_id: int,
        data: bytes,
        file_name: str,
        caption: str = "",
        force_document: bool = False,
        voice_note: bool = False,
        video_note: bool = False,
    ) -> int:
        """Отправить один файл от имени владельца сессии."""
        client = await self.connect()
        stream = io.BytesIO(data)
        stream.name = file_name
        sent = await client.send_file(
            await client.get_entity(chat_id),
            stream,
            caption=caption,
            force_document=force_document,
            voice_note=voice_note,
            video_note=video_note,
        )
        return int(sent.id)

    async def send_album(
        self, chat_id: int, files: list[tuple[bytes, str]], caption: str = ""
    ) -> list[int]:
        """Отправить альбом — проверка сборки медиагруппы на стороне моста."""
        client = await self.connect()
        streams = []
        for data, file_name in files:
            stream = io.BytesIO(data)
            stream.name = file_name
            streams.append(stream)
        sent = await client.send_file(await client.get_entity(chat_id), streams, caption=caption)
        messages = sent if isinstance(sent, list) else [sent]
        return [int(item.id) for item in messages]

    async def set_group_photo(self, chat_id: int, data: bytes) -> None:
        """Поставить группе аватар из готовых байтов картинки."""
        client = await self.connect()
        entity = await client.get_entity(chat_id)
        uploaded = await client.upload_file(data, file_name="avatar.jpg")
        photo = types.InputChatUploadedPhoto(file=uploaded)
        if isinstance(entity, types.Chat):
            await client(functions.messages.EditChatPhotoRequest(chat_id=entity.id, photo=photo))
            return
        await client(functions.channels.EditPhotoRequest(channel=entity, photo=photo))

    async def group_state(self, chat_id: int) -> GroupState | None:
        """Фактическое состояние группы: название и наличие аватара.

        Нужно синхронизации: база помнит лишь то, что мост когда-то сделал, а
        название и картинку могли поменять или удалить вручную.
        """
        client = await self.connect()
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            logger.info("Группа %s недоступна", chat_id, exc_info=True)
            return None
        photo = getattr(entity, "photo", None)
        return GroupState(
            title=str(getattr(entity, "title", "") or ""),
            has_photo=photo is not None and not isinstance(photo, types.ChatPhotoEmpty),
        )

    async def rename_group(self, chat_id: int, title: str) -> None:
        """Переименовать группу под текущее название чата MAX."""
        client = await self.connect()
        entity = await client.get_entity(chat_id)
        name = title[:TITLE_LIMIT]
        # Мост создаёт супергруппы, но привязать можно и обычную группу,
        # созданную руками, — у неё свой набор запросов.
        if isinstance(entity, types.Chat):
            await client(functions.messages.EditChatTitleRequest(chat_id=entity.id, title=name))
            return
        await client(functions.channels.EditTitleRequest(channel=entity, title=name))

    # ------------------------------------------------------------------ #
    # Папка
    # ------------------------------------------------------------------ #

    async def ensure_folder(self, name: str, chat_ids: list[int]) -> int:
        """Создать или обновить папку с указанными чатами. Возвращает её id."""
        client = await self.connect()
        existing = await client(functions.messages.GetDialogFiltersRequest())
        filters = list(getattr(existing, "filters", existing) or [])

        target: types.DialogFilter | None = None
        used_ids: set[int] = set()
        for item in filters:
            filter_id = getattr(item, "id", None)
            if filter_id is not None:
                used_ids.add(int(filter_id))
            if isinstance(item, types.DialogFilter) and _filter_title(item) == name:
                target = item

        peers = []
        for chat_id in chat_ids:
            try:
                peers.append(await client.get_input_entity(chat_id))
            except (ValueError, TypeError):
                logger.warning("Чат %s не найден в сессии — пропускаем в папке", chat_id)

        folder_id = int(target.id) if target is not None else _free_folder_id(used_ids)
        updated = types.DialogFilter(
            id=folder_id,
            title=types.TextWithEntities(text=name, entities=[]),
            pinned_peers=list(getattr(target, "pinned_peers", []) or []) if target else [],
            include_peers=peers,
            exclude_peers=list(getattr(target, "exclude_peers", []) or []) if target else [],
        )
        await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=updated))
        logger.info("Папка «%s» (id=%s) содержит %d чатов", name, folder_id, len(peers))
        return folder_id


def _mask_phone(phone: str) -> str:
    """Показать номер в логах, не раскрывая его целиком."""
    digits = "".join(character for character in phone if character.isdigit())
    return f"+{digits[:2]}***{digits[-2:]}" if len(digits) > 5 else "номер"


def _filter_title(item: types.DialogFilter) -> str:
    """Достать текст названия папки (в новых слоях это TextWithEntities)."""
    title = getattr(item, "title", None)
    if title is None:
        return ""
    return str(getattr(title, "text", title))


def _free_folder_id(used: set[int]) -> int:
    """Подобрать свободный идентификатор папки."""
    for candidate in range(FOLDER_ID_MIN, FOLDER_ID_MAX + 1):
        if candidate not in used:
            return candidate
    raise RuntimeError("Закончились свободные идентификаторы папок Telegram")


def flood_wait_seconds(error: BaseException) -> int | None:
    """Сколько секунд просит подождать Telegram, если это flood wait."""
    if isinstance(error, FloodWaitError):
        return int(error.seconds)
    return None
