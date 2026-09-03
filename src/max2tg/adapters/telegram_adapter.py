"""Адаптер Telegram: приём сообщений из групп и доставка сообщений из MAX."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any, TypeVar

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
)
from aiogram.types import (
    Message as TgMessage,
)

from ..config import Settings
from ..db import MaxAccount
from ..migration import adopt_file_session
from ..models import (
    AttachmentKind,
    NormalizedAttachment,
    NormalizedMessage,
    Platform,
    RemoteChat,
)
from ..ratelimit import RateLimiter
from ..selftest import Scenario, build_scenarios
from ..storage import Storage
from ..util.media import (
    guess_mime,
    human_size,
    render_qr_png,
    safe_filename,
    to_telegram_sticker,
)
from ..util.net import resolve_proxy_url
from ..util.text import entities_to_spans, escape_html, spans_to_html
from .base import RemoteDirectory
from .max_manager import MaxAccountManager
from .telegram_userbot import (
    FOLDER_TITLE_LIMIT,
    TelegramUserbot,
    UserbotNotConfigured,
    flood_wait_seconds,
)
from .userbot_pool import UserbotPool

logger = logging.getLogger("telegram")

T = TypeVar("T")

#: Типы, которые Bot API принимает в media group.
AlbumItem = (
    InputMediaAudio | InputMediaDocument | InputMediaLivePhoto | InputMediaPhoto | InputMediaVideo
)

#: Лимиты Bot API.
TEXT_LIMIT = 4096
#: Виды вложений, к которым Telegram не разрешает подпись.
CAPTIONLESS_KINDS = frozenset({AttachmentKind.VIDEO_NOTE, AttachmentKind.STICKER})

CAPTION_LIMIT = 1024
ALBUM_LIMIT = 10

#: Сколько ждать доставки в самопроверке, прежде чем подводить итог.
#: Сколько ждать, пока мост доставит проверочные сообщения.
SELFTEST_WAIT_LIMIT = 180.0

#: Как часто перепроверять доставку.
SELFTEST_POLL_DELAY = 2.0

#: Насколько раньше запуска ищутся входящие из MAX, в секундах.
SELFTEST_INCOMING_WINDOW = 300
#: Пауза между проверочными сообщениями, чтобы не ловить flood wait.
SELFTEST_STEP_DELAY = 1.5

#: Насколько долгий flood wait ещё имеет смысл переждать на месте.
FLOOD_WAIT_TOLERANCE = 30

#: Пауза перед перезапуском поллинга после сетевого сбоя, в секундах.
RETRY_MIN_DELAY = 5.0
RETRY_MAX_DELAY = 300.0

#: Виды вложений, которые Telegram умеет объединять в альбом.
ALBUM_KINDS = frozenset({AttachmentKind.PHOTO, AttachmentKind.VIDEO})

#: Меню команд в интерфейсе Telegram: личка и группы видят разное.
PRIVATE_COMMANDS = [
    ("start", "с чего начать"),
    ("max_add", "подключить аккаунт MAX"),
    ("max_list", "мои аккаунты MAX"),
    ("login", "вход в свою сессию Telegram"),
    ("sync", "разложить чаты MAX по группам"),
    ("help", "справка"),
]

GROUP_COMMANDS = [
    ("status", "что происходит в этой группе"),
    ("chats", "чаты MAX"),
    ("bind", "привязать группу к чату MAX"),
    ("pause", "приостановить пересылку"),
    ("resume", "возобновить пересылку"),
    ("sync", "сверить группы с чатами MAX"),
    ("selftest", "проверить мост"),
    ("unbind", "снять привязку"),
    ("help", "справка"),
]

START_TEXT = (
    "<b>Мост MAX ⇄ Telegram</b>\n\n"
    "Я переношу переписку из MAX в Telegram и обратно: каждому чату MAX "
    "соответствует своя группа здесь.\n\n"
    "<b>Как начать</b>\n"
    "1. /max_add — подключить аккаунт MAX (пришлю QR-код)\n"
    "2. /login — вход в вашу сессию Telegram, чтобы я мог создавать группы\n"
    "3. /sync — я создам группы под все чаты MAX и сложу их в папку\n\n"
    "Не хотите входить сессией — создайте группу сами, добавьте меня "
    "администратором и выполните /bind.\n\n"
    "Подробности: /help"
)

#: Отказ, когда группой распоряжается другой человек.
NOT_YOURS = (
    "Этой группой распоряжается кто-то другой: менять привязку может "
    "администратор группы, а если она уже привязана — владелец её аккаунта MAX."
)

#: Сколько чатов показывать кнопками — дальше список превращается в простыню.
BIND_BUTTONS_LIMIT = 12

HELP_TEXT = (
    "<b>Мост MAX ⇄ Telegram</b>\n\n"
    "Каждая группа Telegram привязывается к одному чату MAX. "
    "Всё, что приходит в чат MAX, попадает сюда; всё, что вы пишете здесь, "
    "уходит в MAX.\n\n"
    "<b>Аккаунты MAX</b> (в личке с ботом)\n"
    "/max_add — подключить аккаунт MAX по QR-коду\n"
    "/max_list — мои аккаунты и их состояние\n"
    "/max_remove &lt;номер&gt; — отключить аккаунт\n\n"
    "<b>Группы</b>\n"
    "/chats [фильтр] — список чатов MAX с их идентификаторами\n"
    "/bind &lt;max_chat_id&gt; — привязать эту группу к чату MAX\n"
    "/sync [название папки] — создать недостающие группы и сверить названия с аватарами\n"
    "/login [номер] — вход в пользовательскую сессию Telegram (только в личке)\n"
    "/pause и /resume — приостановить и возобновить пересылку в этой группе\n"
    "/unbind — снять привязку\n"
    "/status — что происходит в этой группе\n"
    "/selftest [full] — прогнать проверочные сообщения в обе стороны\n"
    "/id — идентификатор этой группы\n"
    "/help — эта справка"
)


class TelegramAdapter:
    """Адаптер платформы Telegram."""

    platform = Platform.TELEGRAM

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        accounts: MaxAccountManager,
        on_message: Callable[[NormalizedMessage], Awaitable[None]],
        userbots: UserbotPool | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._accounts = accounts
        self._directory: RemoteDirectory = accounts
        self._on_message = on_message
        self._userbots = userbots
        #: Наследство одиночной установки ищем один раз за запуск.
        self._orphans_checked = False
        self._limiter = RateLimiter(settings.tg_global_rate, settings.tg_chat_rate)
        proxy = resolve_proxy_url(settings.tg_proxy)
        if proxy:
            logger.info("Bot API через прокси %s", proxy)
        self.bot = Bot(
            token=settings.tg_bot_token,
            session=AiohttpSession(proxy=proxy) if proxy else AiohttpSession(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self._build_router())
        #: Буфер альбомов: media_group_id -> собранные сообщения.
        self._albums: dict[str, list[TgMessage]] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}
        #: Включённый privacy mode прячет от бота сообщения группы.
        self._privacy_mode_on = False
        #: Имя бота нужно, чтобы приглашать его в создаваемые группы.
        self._username: str = ""
        self._bot_id: int = 0
        #: Синхронизации и самопроверки идут параллельно у разных людей.
        self._sync_tasks: dict[int, asyncio.Task[None]] = {}
        self._selftest_tasks: dict[int, asyncio.Task[None]] = {}
        self._history_task: asyncio.Task[None] | None = None
        #: Незавершённые шаги входа: id пользователя -> ожидаемый ответ.
        self._login_flows: dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # Запуск
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Запустить long polling, переживая обрывы связи.

        Мост живёт долго, а сеть — нет: падение поллинга не должно ронять
        вторую половину моста, поэтому перезапускаемся с нарастающей паузой.
        """
        delay = RETRY_MIN_DELAY
        while True:
            try:
                me = await self.bot.get_me()
                logger.info("Бот Telegram запущен: @%s (id=%s)", me.username, me.id)
                self._username = me.username or ""
                self._bot_id = me.id
                self._privacy_mode_on = me.can_read_all_group_messages is False
                await self._publish_commands()
                if self._privacy_mode_on:
                    logger.warning(
                        "У бота включён privacy mode: в группах, где он обычный "
                        "участник, сообщения ему не видны и пересылка Telegram -> MAX "
                        "не работает. Достаточно назначить %s администратором группы — "
                        "админ получает все сообщения. Либо выключите режим глобально: "
                        "@BotFather -> /mybots -> Bot Settings -> Group Privacy -> Turn off.",
                        f"@{me.username}",
                    )
                delay = RETRY_MIN_DELAY
                await self.dispatcher.start_polling(self.bot, handle_signals=False)
                return
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                logger.error("Telegram отверг токен бота — проверьте TG_BOT_TOKEN")
                raise
            except Exception as error:
                logger.error(
                    "Сбой Telegram-поллинга (%s: %s). Повтор через %.0f с",
                    type(error).__name__,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_DELAY)

    async def stop(self) -> None:
        """Остановить polling и закрыть сессию."""
        await self.dispatcher.stop_polling()
        await self.bot.session.close()

    # ------------------------------------------------------------------ #
    # Приём сообщений из Telegram
    # ------------------------------------------------------------------ #

    def _build_router(self) -> Router:
        router = Router(name="max2tg")

        router.message.register(self._cmd_start, CommandStart())
        router.message.register(self._cmd_help, Command("help"))
        router.message.register(self._cmd_pause, Command("pause"))
        router.message.register(self._cmd_resume, Command("resume"))
        router.message.register(self._cmd_id, Command("id"))
        router.message.register(self._cmd_max_add, Command("max_add"))
        router.message.register(self._cmd_max_list, Command("max_list"))
        router.message.register(self._cmd_max_remove, Command("max_remove"))
        router.message.register(self._cmd_chats, Command("chats"))
        router.message.register(self._cmd_bind, Command("bind"))
        router.message.register(self._cmd_unbind, Command("unbind"))
        router.message.register(self._cmd_status, Command("status"))
        router.message.register(self._cmd_login, Command("login"))
        router.message.register(self._cmd_sync, Command("sync"))
        router.message.register(self._cmd_selftest, Command("selftest"))
        router.my_chat_member.register(self._on_added_to_group)
        router.callback_query.register(self._on_callback)
        router.message.register(
            self._handle_group_message,
            F.chat.type.in_(
                {
                    ChatType.GROUP,
                    ChatType.SUPERGROUP,
                }
            ),
        )
        router.message.register(
            self._handle_private_message,
            F.chat.type == ChatType.PRIVATE,
        )
        router.edited_message.register(
            self._handle_edited_message,
            F.chat.type.in_(
                {
                    ChatType.GROUP,
                    ChatType.SUPERGROUP,
                }
            ),
        )
        return router

    def _is_admin(self, user_id: int | None) -> bool:
        if not self._settings.tg_admin_ids:
            return True
        return user_id in self._settings.tg_admin_ids

    async def _owner_accounts(self, user_id: int | None) -> list[MaxAccount]:
        """Аккаунты MAX, принадлежащие пользователю.

        Администраторы моста видят все аккаунты: иначе не починить чужую
        привязку и не разобрать инцидент.
        """
        if user_id is None:
            return []
        await self._adopt_orphans(user_id)
        if user_id in self._settings.tg_admin_ids:
            return await self._storage.list_accounts()
        return await self._storage.list_accounts(user_id)

    async def _adopt_orphans(self, user_id: int) -> None:
        """Передать наследство одиночной установки первому администратору.

        До многопользовательского режима владельца не существовало, поэтому у
        перенесённых аккаунтов его нет. Отдаём их тому, кто вправе ими
        распоряжаться, вместе со старой файловой сессией Telegram — иначе
        человек потеряет и привязки, и уже выполненный вход.
        """
        if self._orphans_checked or not self._is_admin(user_id):
            return
        self._orphans_checked = True
        claimed = await self._storage.claim_orphan_accounts(user_id)
        if not claimed:
            return
        logger.warning(
            "Аккаунты MAX %s закреплены за пользователем %s. "
            "Пропишите TG_ADMIN_IDS в .env, чтобы это не решалось на лету.",
            ", ".join(str(item) for item in claimed),
            user_id,
        )
        await adopt_file_session(self._storage, self._settings, user_id)

    async def _resolve_account(
        self, message: TgMessage, argument: str | None = None
    ) -> tuple[MaxAccount | None, str, str]:
        """Определить аккаунт для команды.

        Возвращает аккаунт, остаток аргументов и текст ошибки. Если аккаунт
        один — он подставляется молча; если несколько, номер обязателен.
        """
        user_id = message.from_user.id if message.from_user else None
        rest = (argument or "").strip()

        binding = await self._storage.get_by_tg(message.chat.id)
        parts = rest.split(maxsplit=1)
        if parts and parts[0].isdigit():
            wanted = int(parts[0])
            account = await self._storage.get_account(wanted)
            if account is not None:
                if not self._may_use(user_id, account):
                    return None, rest, "Этот аккаунт MAX принадлежит другому пользователю."
                return account, parts[1] if len(parts) > 1 else "", ""

        accounts = await self._owner_accounts(user_id)
        if binding is not None:
            for account in accounts:
                if account.id == binding.account_id:
                    return account, rest, ""

        if not accounts:
            return None, rest, "У вас нет подключённых аккаунтов MAX. Добавьте: /max_add"
        if len(accounts) == 1:
            return accounts[0], rest, ""

        listing = ", ".join(f"{item.id} — {item.nickname}" for item in accounts)
        return None, rest, f"У вас несколько аккаунтов MAX, укажите номер: {listing}"

    def _may_use(self, user_id: int | None, account: MaxAccount) -> bool:
        """Может ли пользователь распоряжаться этим аккаунтом."""
        if user_id is None:
            return False
        return account.owner_id == user_id or user_id in self._settings.tg_admin_ids

    async def _is_group_admin(self, chat_id: int, user_id: int | None) -> bool:
        """Распоряжается ли человек этой группой Telegram.

        Иначе любой участник мог бы перевесить общую группу на свой аккаунт MAX
        и тихо читать её переписку у себя.
        """
        if user_id is None:
            return False
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except Exception:
            logger.debug("Не удалось проверить права в чате %s", chat_id, exc_info=True)
            return False
        return member.status in {"administrator", "creator"}

    async def _may_manage_chat(self, chat_id: int, chat_type: str, user_id: int | None) -> bool:
        """Может ли человек менять привязку этой группы.

        Право даёт либо администратор моста, либо администратор самой группы:
        мост обслуживает многих, и хозяин одной группы не должен решать за
        соседнюю.
        """
        if self._is_admin(user_id):
            return True
        if chat_type == ChatType.PRIVATE:
            return False
        if not await self._is_group_admin(chat_id, user_id):
            return False

        binding = await self._storage.get_by_tg(chat_id)
        if binding is None:
            return True
        account = await self._storage.get_account(binding.account_id)
        # Уже привязанную группу трогает только владелец её аккаунта MAX.
        return account is None or self._may_use(user_id, account)

    async def _manage_allowed(self, message: TgMessage) -> bool:
        """Та же проверка прав, но для обычной команды в чате."""
        return await self._may_manage_chat(
            message.chat.id,
            str(message.chat.type),
            message.from_user.id if message.from_user else None,
        )

    def _may_signup(self, user_id: int | None) -> bool:
        """Разрешено ли этому человеку подключать свои аккаунты."""
        if user_id is None:
            return False
        if self._settings.allow_public_signup:
            return True
        return user_id in self._settings.tg_admin_ids

    # ------------------------------------------------------------------ #
    # Аккаунты MAX
    # ------------------------------------------------------------------ #

    async def _cmd_max_add(self, message: TgMessage, command: CommandObject) -> None:
        """Подключить новый аккаунт MAX: ссылка и QR-код приходят в личку."""
        user_id = message.from_user.id if message.from_user else None
        if not self._may_signup(user_id):
            await message.answer("Подключение аккаунтов доступно только администраторам моста.")
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer(
                "Подключать аккаунт MAX нужно в личке с ботом: там будет QR-код для входа."
            )
            return
        if user_id is None:
            return

        mine = await self._storage.list_accounts(user_id)
        if len(mine) >= self._settings.max_accounts_per_user:
            await message.answer(
                f"Достигнут предел: {self._settings.max_accounts_per_user} аккаунтов MAX. "
                "Освободите место через /max_remove."
            )
            return

        nickname = (command.args or "").strip()[:64] or f"MAX {len(mine) + 1}"
        account = await self._storage.add_account(user_id, nickname)
        await message.answer(
            f"Аккаунт <b>{escape_html(nickname)}</b> заведён (номер <code>{account.id}</code>).\n"
            "Сейчас пришлю ссылку для входа — откройте её в приложении MAX "
            "или отсканируйте QR-код."
        )

        async def send_login_link(url: str) -> None:
            await self._send_login_qr(message.chat.id, url)

        try:
            session = await self._accounts.start_account(
                account.id, token=None, nickname=nickname, qr_callback=send_login_link
            )
        except Exception as error:
            logger.exception("Не удалось подключить аккаунт MAX %s", account.id)
            await self._storage.remove_account(account.id)
            await message.answer(escape_html(f"Не удалось подключить аккаунт: {error}"))
            return

        await message.answer(
            f"✅ Аккаунт <b>{escape_html(session.nickname)}</b> подключён.\n"
            "Дальше: /sync — разложить чаты по группам, или /chats — посмотреть их список."
        )

    async def _send_login_qr(self, chat_id: int, url: str) -> None:
        """Отправить пользователю ссылку и QR-код для входа в MAX."""
        await self._send_chunks(
            chat_id,
            "Откройте ссылку в приложении MAX (Настройки → Устройства) "
            f"или отсканируйте QR-код:\n<code>{escape_html(url)}</code>",
        )
        image = await asyncio.to_thread(render_qr_png, url)
        if image is None:
            return
        with suppress(Exception):
            await self._call(
                chat_id,
                partial(
                    self.bot.send_photo,
                    chat_id=chat_id,
                    photo=BufferedInputFile(image, filename="max_login.png"),
                    caption="Код действует несколько минут.",
                ),
            )

    async def _cmd_max_list(self, message: TgMessage) -> None:
        """Показать подключённые аккаунты MAX."""
        user_id = message.from_user.id if message.from_user else None
        await self._send_account_list(message.chat.id, user_id)

    async def _send_account_list(self, chat_id: int, user_id: int | None) -> None:
        """Список аккаунтов MAX с их состоянием — общий для команды и кнопки."""
        accounts = await self._owner_accounts(user_id)
        if not accounts:
            await self._send_chunks(chat_id, "Аккаунтов MAX пока нет. Добавить: /max_add")
            return
        lines = ["<b>Ваши аккаунты MAX</b>", ""]
        for account in accounts:
            state = "подключён" if self._accounts.session(account.id) else "не подключён"
            bindings = await self._storage.list_bindings(account.id)
            lines.append(
                f"<code>{account.id}</code> — {escape_html(account.nickname)} "
                f"({state}, групп: {len(bindings)})"
            )
        await self._send_chunks(chat_id, "\n".join(lines))

    async def _cmd_max_remove(self, message: TgMessage, command: CommandObject) -> None:
        """Отключить аккаунт MAX и убрать его привязки."""
        user_id = message.from_user.id if message.from_user else None
        argument = (command.args or "").strip()
        if not argument.isdigit():
            await message.answer(
                "Укажите номер аккаунта: <code>/max_remove 2</code> (см. /max_list)"
            )
            return
        account = await self._storage.get_account(int(argument))
        if account is None or not self._may_use(user_id, account):
            await message.answer("Такого аккаунта нет или он принадлежит другому пользователю.")
            return
        await self._accounts.stop_account(account.id)
        await self._storage.remove_account(account.id)
        await message.answer(
            f"Аккаунт <b>{escape_html(account.nickname)}</b> отключён, его привязки удалены. "
            "Сами группы Telegram остались на месте."
        )

    async def _publish_commands(self) -> None:
        """Показать команды в меню Telegram — их не нужно будет вспоминать."""
        try:
            await self.bot.set_my_commands(
                [BotCommand(command=name, description=text) for name, text in PRIVATE_COMMANDS],
                scope=BotCommandScopeAllPrivateChats(),
            )
            await self.bot.set_my_commands(
                [BotCommand(command=name, description=text) for name, text in GROUP_COMMANDS],
                scope=BotCommandScopeAllGroupChats(),
            )
        except Exception:
            logger.warning("Не удалось обновить меню команд", exc_info=True)

    async def _cmd_start(self, message: TgMessage) -> None:
        """Первое знакомство: что это и что делать дальше."""
        if message.chat.type != ChatType.PRIVATE:
            await self._cmd_status(message)
            return
        await message.answer(START_TEXT, reply_markup=self._start_keyboard())

    def _start_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подключить MAX", callback_data="start:max_add")],
                [InlineKeyboardButton(text="Мои аккаунты", callback_data="start:max_list")],
                [InlineKeyboardButton(text="Справка", callback_data="start:help")],
            ]
        )

    async def _on_added_to_group(self, event: ChatMemberUpdated) -> None:
        """Поздороваться, когда бота добавили в группу, и сразу объяснить шаги."""
        new = event.new_chat_member
        if new.user.id != self._bot_id:
            return
        if new.status not in {"member", "administrator"}:
            return
        if await self._storage.get_by_tg(event.chat.id) is not None:
            # Группу создал /sync — она уже привязана, лишний шум не нужен.
            return

        lines = [
            "Я мост между MAX и Telegram.",
            "",
            "Чтобы эта группа заработала:",
            "1. /chats — посмотреть чаты MAX",
            "2. /bind — выбрать чат (можно просто нажать кнопку в списке)",
        ]
        if self._privacy_mode_on and new.status != "administrator":
            lines.append("")
            lines.append(
                "⚠️ Назначьте меня администратором группы — иначе я не вижу "
                "сообщения и пересылка отсюда в MAX не работает."
            )
        with suppress(Exception):
            await self.bot.send_message(event.chat.id, "\n".join(lines))

    async def _cmd_pause(self, message: TgMessage) -> None:
        """Временно остановить пересылку, не теряя привязку."""
        await self._switch_forwarding(message, enabled=False)

    async def _cmd_resume(self, message: TgMessage) -> None:
        """Вернуть пересылку после /pause."""
        await self._switch_forwarding(message, enabled=True)

    async def _switch_forwarding(self, message: TgMessage, *, enabled: bool) -> None:
        if not await self._manage_allowed(message):
            await message.answer(NOT_YOURS)
            return
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None:
            await message.answer("Группа не привязана — приостанавливать нечего.")
            return
        if binding.enabled == enabled:
            await message.answer(
                "Пересылка и так включена." if enabled else "Пересылка уже приостановлена."
            )
            return
        await self._storage.set_enabled(message.chat.id, enabled)
        await message.answer(
            "▶️ Пересылка возобновлена."
            if enabled
            else "⏸ Пересылка приостановлена. Вернуть: /resume"
        )

    async def _cmd_help(self, message: TgMessage) -> None:
        await message.answer(HELP_TEXT)

    async def _cmd_id(self, message: TgMessage) -> None:
        await message.answer(
            f"Чат: <code>{message.chat.id}</code>\n"
            f"Вы: <code>{message.from_user.id if message.from_user else '?'}</code>"
        )

    async def _cmd_chats(self, message: TgMessage, command: CommandObject) -> None:
        account, rest, problem = await self._resolve_account(message, command.args)
        if account is None:
            await message.answer(problem)
            return
        query = rest or None
        try:
            chats = await self._directory.list_chats(account.id, query)
        except Exception:
            logger.exception("Не удалось получить список чатов MAX")
            await message.answer("Не удалось получить список чатов MAX — смотрите логи.")
            return
        if not chats:
            await message.answer("Чаты MAX не найдены.")
            return

        lines = [f"<b>Чаты MAX — {escape_html(account.nickname)}</b>", ""]
        for chat in chats[:60]:
            title = escape_html(chat.title)
            lines.append(f"<code>{chat.id}</code> — {title} <i>({chat.type.lower()})</i>")
        lines.append("")
        lines.append("Привязать: <code>/bind &lt;id&gt;</code>")
        await self._send_chunks(message.chat.id, "\n".join(lines))

    async def _cmd_bind(self, message: TgMessage, command: CommandObject) -> None:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer("Привязывать можно только группу: добавьте бота в группу.")
            return
        if not await self._manage_allowed(message):
            await message.answer(NOT_YOURS)
            return
        account, raw, problem = await self._resolve_account(message, command.args)
        if account is None:
            await message.answer(problem)
            return
        if not raw:
            await self._offer_bind_buttons(message, account)
            return
        try:
            max_chat_id = int(raw.split()[0])
        except ValueError:
            await message.answer("Идентификатор чата MAX должен быть числом.")
            return

        chat = await self._directory.resolve_chat(account.id, max_chat_id)
        if chat is None:
            await message.answer(
                "Чат MAX с таким идентификатором не найден среди доступных. "
                "Проверьте список: /chats"
            )
            return

        await self._storage.bind(message.chat.id, max_chat_id, chat.title, account_id=account.id)
        await message.answer(
            f"✅ Группа привязана к чату MAX <b>{escape_html(chat.title)}</b> "
            f"(<code>{max_chat_id}</code>, аккаунт {escape_html(account.nickname)}).\n"
            "Сообщения из MAX будут приходить сюда, а ваши сообщения здесь — уходить в MAX."
        )
        limit = self._settings.history_import_limit
        if limit > 0:
            await message.answer(f"Переношу последние сообщения чата (до {limit})…")
            self._history_task = asyncio.create_task(
                self._import_history(message.chat.id, max_chat_id, limit, account.id),
                name="max2tg-history",
            )

    async def _offer_bind_buttons(self, message: TgMessage, account: MaxAccount) -> None:
        """Показать чаты MAX кнопками: привязка в одно нажатие, без копирования id."""
        try:
            chats = await self._directory.list_chats(account.id)
        except Exception:
            logger.exception("Не удалось получить список чатов MAX")
            await message.answer(
                "Не удалось получить список чатов MAX. "
                "Можно указать идентификатор вручную: <code>/bind 123456789</code>"
            )
            return
        if not chats:
            await message.answer("В этом аккаунте MAX нет доступных чатов.")
            return

        bound = {item.max_chat_id for item in await self._storage.list_bindings(account.id)}
        free = [chat for chat in chats if chat.id not in bound]
        if not free:
            await message.answer(
                "Все чаты этого аккаунта MAX уже привязаны к другим группам. Список: /chats"
            )
            return

        rows = [
            [
                InlineKeyboardButton(
                    text=_button_title(chat.title),
                    callback_data=f"bind:{account.id}:{chat.id}",
                )
            ]
            for chat in free[:BIND_BUTTONS_LIMIT]
        ]
        tail = ""
        if len(free) > BIND_BUTTONS_LIMIT:
            tail = (
                f"\n\nПоказаны первые {BIND_BUTTONS_LIMIT} из {len(free)}. "
                "Остальные — /chats, затем <code>/bind &lt;id&gt;</code>."
            )
        await message.answer(
            f"К какому чату MAX привязать эту группу? "
            f"Аккаунт: <b>{escape_html(account.nickname)}</b>.{tail}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _cmd_unbind(self, message: TgMessage) -> None:
        if not await self._manage_allowed(message):
            await message.answer(NOT_YOURS)
            return
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None:
            await message.answer("Эта группа ни к чему не привязана.")
            return
        # Снятая по ошибке привязка стоит переноса истории заново, поэтому спрашиваем.
        await message.answer(
            f"Снять привязку к чату MAX <b>{escape_html(binding.title or 'без названия')}</b>?\n"
            "Сообщения перестанут ходить в обе стороны. Группа и переписка останутся.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Снять", callback_data="unbind:yes"),
                        InlineKeyboardButton(text="Отмена", callback_data="unbind:no"),
                    ]
                ]
            ),
        )

    async def _on_callback(self, callback: CallbackQuery) -> None:
        """Разобрать нажатие на кнопку. Права проверяются здесь же."""
        data = callback.data or ""
        user_id = callback.from_user.id if callback.from_user else None
        message = callback.message
        if message is None:
            await callback.answer()
            return

        may_manage = await self._may_manage_chat(message.chat.id, str(message.chat.type), user_id)
        if data.startswith(("unbind:", "bind:")) and not may_manage:
            await callback.answer("Этой группой распоряжается кто-то другой.", show_alert=True)
            return
        if data.startswith("start:") and not self._may_signup(user_id):
            await callback.answer("Подключение к мосту закрыто.", show_alert=True)
            return

        if data.startswith("start:"):
            await self._callback_start(callback, data.removeprefix("start:"))
            return
        if data.startswith("bind:"):
            await self._callback_bind(callback, data.removeprefix("bind:"))
            return
        if data == "unbind:no":
            await callback.answer("Отменено.")
            with suppress(Exception):
                await self.bot.edit_message_text(
                    "Отменено — привязка на месте.",
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            return
        if data == "unbind:yes":
            removed = await self._storage.unbind(message.chat.id)
            await callback.answer("Готово." if removed else "Привязки уже нет.")
            with suppress(Exception):
                await self.bot.edit_message_text(
                    "Привязка снята. Вернуть: /bind" if removed else "Привязки уже нет.",
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            return
        await callback.answer()

    async def _callback_start(self, callback: CallbackQuery, action: str) -> None:
        """Кнопки приветствия: те же команды, но без набора руками."""
        message = callback.message
        if message is None:
            return
        await callback.answer()
        if action == "help":
            await self.bot.send_message(message.chat.id, HELP_TEXT)
        elif action == "max_list":
            await self._send_account_list(message.chat.id, callback.from_user.id)
        elif action == "max_add":
            await self.bot.send_message(
                message.chat.id,
                "Отправьте /max_add — я пришлю ссылку и QR-код для входа в MAX.",
            )

    async def _callback_bind(self, callback: CallbackQuery, payload: str) -> None:
        """Привязка выбранного кнопкой чата MAX."""
        message = callback.message
        if message is None:
            return
        try:
            account_id_raw, chat_id_raw = payload.split(":", 1)
            account_id, max_chat_id = int(account_id_raw), int(chat_id_raw)
        except ValueError:
            await callback.answer("Не разобрал выбор.", show_alert=True)
            return

        account = await self._storage.get_account(account_id)
        if account is None or not self._may_use(callback.from_user.id, account):
            await callback.answer("Аккаунт недоступен.", show_alert=True)
            return

        chat = await self._directory.resolve_chat(account_id, max_chat_id)
        if chat is None:
            await callback.answer("Чат MAX больше не доступен.", show_alert=True)
            return

        await self._storage.bind(message.chat.id, max_chat_id, chat.title, account_id=account_id)
        await callback.answer("Привязано.")
        with suppress(Exception):
            await self.bot.edit_message_text(
                f"✅ Группа привязана к чату MAX <b>{escape_html(chat.title)}</b> "
                f"(<code>{max_chat_id}</code>, аккаунт {escape_html(account.nickname)}).",
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        limit = self._settings.history_import_limit
        if limit > 0:
            await self.bot.send_message(
                message.chat.id, f"Переношу последние сообщения чата (до {limit})…"
            )
            self._history_task = asyncio.create_task(
                self._import_history(message.chat.id, max_chat_id, limit, account_id),
                name="max2tg-history",
            )

    async def _cmd_login(self, message: TgMessage, command: CommandObject) -> None:
        """Начать вход в пользовательскую сессию Telegram прямо из чата с ботом."""
        user_id = message.from_user.id if message.from_user else None
        if not self._may_signup(user_id):
            await message.answer(
                "Подключение к мосту сейчас закрыто — обратитесь к его администратору."
            )
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer(
                "Вход выполняется только в личке с ботом: код подтверждения нельзя "
                "показывать в группе."
            )
            return
        if user_id is None:
            return
        if self._userbots is None or not self._userbots.configured:
            await message.answer(
                "Сначала пропишите в <code>.env</code> значения <code>TG_API_ID</code> и "
                "<code>TG_API_HASH</code> с my.telegram.org и перезапустите мост."
            )
            return

        argument = (command.args or "").strip()
        if argument.lower() in {"cancel", "отмена"}:
            self._login_flows.pop(user_id, None)
            await message.answer("Вход отменён.")
            return
        if argument.lower() in {"logout", "выход"}:
            await self._userbots.forget(user_id)
            await message.answer("Сессия Telegram забыта. Автосоздание групп отключено.")
            return

        userbot = await self._userbots.get(user_id)
        if await userbot.is_authorized():
            who = await userbot.whoami()
            await message.answer(
                f"Сессия уже авторизована: <b>{escape_html(who)}</b>. Можно вызывать /sync."
            )
            return

        phone = argument or self._settings.tg_phone
        if not phone:
            await message.answer(
                "Укажите номер: <code>/login +79991234567</code> (или заполните TG_PHONE в .env)."
            )
            return

        try:
            await userbot.request_code(phone)
        except Exception as error:
            logger.exception("Не удалось запросить код входа")
            await message.answer(escape_html(f"Не удалось запросить код: {error}"))
            return

        self._login_flows[user_id] = "code"
        await message.answer(
            "Код отправлен в Telegram.\n\n"
            "⚠️ Пришлите его <b>через дефисы</b>, например <code>1-2-3-4-5</code>. "
            "Telegram аннулирует код, который увидит в переписке обычным числом.\n\n"
            "Сообщение с кодом я удалю сразу после проверки. Отменить: "
            "<code>/login cancel</code>"
        )

    async def _handle_private_message(self, message: TgMessage) -> None:
        """Ответы на шаги входа: код и пароль двухфакторной защиты."""
        user_id = message.from_user.id if message.from_user else None
        step = self._login_flows.get(user_id) if user_id is not None else None
        if step is None or user_id is None or self._userbots is None:
            return

        payload = (message.text or "").strip()
        # Секреты не задерживаются в переписке дольше необходимого.
        with suppress(Exception):
            await self.bot.delete_message(message.chat.id, message.message_id)

        if self._userbots is None:
            return
        userbot = await self._userbots.get(user_id)
        try:
            if step == "code":
                digits = re.sub(r"\D", "", payload)
                if not digits:
                    await message.answer("Не вижу цифр кода. Пришлите его как 1-2-3-4-5.")
                    return
                result = await userbot.submit_code(digits)
                if result == "password":
                    self._login_flows[user_id] = "password"
                    await message.answer(
                        "Нужен пароль двухфакторной защиты. Пришлите его сообщением — "
                        "я удалю его сразу после проверки."
                    )
                    return
            else:
                if not payload:
                    await message.answer("Пустой пароль. Пришлите пароль двухфакторной защиты.")
                    return
                await userbot.submit_password(payload)
        except Exception as error:
            logger.exception("Ошибка на шаге входа %s", step)
            self._login_flows.pop(user_id, None)
            await message.answer(escape_html(f"Вход не удался: {error}\n\nНачните заново: /login"))
            return

        self._login_flows.pop(user_id, None)
        who = await userbot.whoami()
        await message.answer(
            f"✅ Сессия авторизована: <b>{escape_html(who)}</b>.\n"
            "Теперь /sync создаст группы под чаты MAX и сложит их в папку."
        )

    async def _cmd_selftest(self, message: TgMessage, command: CommandObject) -> None:
        """Прогнать проверочные сообщения в обе стороны и отчитаться."""
        if not await self._manage_allowed(message):
            await message.answer(NOT_YOURS)
            return
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None:
            await message.answer("Группа не привязана — сначала /bind или /sync.")
            return
        running = self._selftest_tasks.get(message.chat.id)
        if running is not None and not running.done():
            await message.answer("Самопроверка в этой группе уже идёт — дождитесь отчёта.")
            return

        full = (command.args or "").strip().lower() in {"full", "полный", "все", "всё"}
        nonce = uuid.uuid4().hex[:6]
        await message.answer(
            f"Самопроверка <code>{nonce}</code> запущена"
            f"{' (полная матрица)' if full else ''}. Сообщения ниже — проверочные."
        )
        self._selftest_tasks[message.chat.id] = asyncio.create_task(
            self._run_selftest(
                message.chat.id,
                binding.max_chat_id,
                binding.account_id,
                nonce,
                full,
                message.from_user.id if message.from_user else None,
            ),
            name="max2tg-selftest",
        )

    async def _run_selftest(
        self,
        tg_chat_id: int,
        max_chat_id: int,
        account_id: int,
        nonce: str,
        full: bool,
        owner_id: int | None,
    ) -> None:
        """Прогнать проверки и вернуть в группу таблицу результатов."""
        results: list[tuple[str, str]] = []
        # Естественный порядок такой: человек пишет в MAX, а потом запускает
        # проверку. Поэтому засчитываем и то, что пришло незадолго до старта.
        started_at = int(time.time()) - SELFTEST_INCOMING_WINDOW

        # --- Telegram -> MAX: пишем от имени владельца, путь обычный ---
        sent_ids: dict[str, tuple[str, list[int], bool]] = {}
        selftest_bot: TelegramUserbot | None = None
        if self._userbots is not None and owner_id is not None:
            # Проверочные сообщения шлёт сам владелец: путь должен быть тот же,
            # что и у обычной переписки, иначе проверка ничего не доказывает.
            selftest_bot = await self._userbots.authorized(owner_id)
        if selftest_bot is not None:
            scenarios = build_scenarios(nonce)
            if not full:
                scenarios = [item for item in scenarios if item.key in {"text", "photo"}]
            for scenario in scenarios:
                try:
                    ids = await self._send_scenario(tg_chat_id, scenario, sent_ids, selftest_bot)
                except Exception as error:
                    logger.exception("Самопроверка: сценарий %s не отправился", scenario.key)
                    results.append((scenario.title, f"не отправилось: {error}"))
                    continue
                sent_ids[scenario.key] = (scenario.title, ids, scenario.merged)
                await asyncio.sleep(SELFTEST_STEP_DELAY)
        else:
            results.append(("Telegram - MAX", "пропущено: нет сессии, см. /login"))

        await self._await_delivery(tg_chat_id, sent_ids)

        checked: list[tuple[str, bool, str]] = []
        incoming = await self._storage.count_incoming_since(max_chat_id, started_at, account_id)
        if incoming:
            checked.append(("MAX - Telegram (входящее сообщение)", True, f" ({incoming})"))
        else:
            results.append(
                (
                    "MAX - Telegram",
                    "не проверено: напишите в этот чат MAX с телефона (можно перед "
                    "запуском) и повторите /selftest — сервер не присылает мосту "
                    "его собственные сообщения",
                )
            )
        logger.info("Самопроверка %s: входящих из MAX за прогон: %s", nonce, incoming)

        for key, (title, ids, merged) in sent_ids.items():
            mapped = [
                await self._storage.find_max_message(tg_chat_id, message_id) for message_id in ids
            ]
            arrived = [item for item in mapped if item]
            # Альбом принимающая сторона склеивает в одно сообщение, поэтому
            # дробь «1/3» означала бы потерю там, где её нет.
            suffix = f" ({len(arrived)}/{len(ids)})" if len(ids) > 1 and not merged else ""
            checked.append((title, bool(arrived), suffix))
            logger.info("Самопроверка %s: %s -> %s", nonce, key, bool(arrived))

        lines = [f"<b>Итог самопроверки {nonce}</b>", ""]
        failed = 0
        for title, ok, suffix in checked:
            if not ok:
                failed += 1
            mark = "\u2705" if ok else "\u274c"
            lines.append(f"{mark} {escape_html(title)}{suffix}")
        for title, status in results:
            warning = status.startswith("не проверено")
            if not warning:
                failed += 1
            mark = "\u26a0\ufe0f" if warning else "\u274c"
            lines.append(f"{mark} {escape_html(title)} — {escape_html(status)}")
        lines.insert(1, f"Проверок: {len(checked) + len(results)}, неудачных: {failed}")
        if failed:
            lines.append("")
            lines.append("Подробности сбоев — в логе моста.")
        await self._send_chunks(tg_chat_id, "\n".join(lines))

    async def _await_delivery(
        self, tg_chat_id: int, sent_ids: dict[str, tuple[str, list[int], bool]]
    ) -> None:
        """Дождаться, пока очередь моста разберёт отправленное.

        Фиксированная пауза врала: одно тяжёлое вложение задерживало очередь,
        и следующие за ним сценарии попадали в отчёт как несработавшие.
        """
        if not sent_ids:
            return
        deadline = time.monotonic() + SELFTEST_WAIT_LIMIT
        while True:
            if await self._all_arrived(tg_chat_id, sent_ids) or time.monotonic() >= deadline:
                return
            await asyncio.sleep(SELFTEST_POLL_DELAY)

    async def _all_arrived(
        self, tg_chat_id: int, sent_ids: dict[str, tuple[str, list[int], bool]]
    ) -> bool:
        """Всё ли отправленное уже нашло пару на той стороне."""
        for _title, ids, merged in sent_ids.values():
            found = 0
            for message_id in ids:
                if await self._storage.find_max_message(tg_chat_id, message_id):
                    found += 1
            # Склеенному альбому хватает одной пары: он ушёл одним сообщением.
            enough = 1 if merged else len(ids)
            if found < enough:
                return False
        return True

    async def _send_scenario(
        self,
        tg_chat_id: int,
        scenario: Scenario,
        already_sent: dict[str, tuple[str, list[int], bool]],
        userbot: TelegramUserbot | None,
    ) -> list[int]:
        """Отправить один проверочный случай от имени владельца сессии."""
        if userbot is None:
            return []
        reply_to: int | None = None
        if scenario.reply_to_key:
            previous = already_sent.get(scenario.reply_to_key)
            reply_to = previous[1][0] if previous and previous[1] else None

        if scenario.kind == "album":
            return await userbot.send_album(tg_chat_id, scenario.files, scenario.caption)
        if scenario.kind == "file":
            data, file_name = scenario.files[0]
            return [
                await userbot.send_file(
                    tg_chat_id,
                    data,
                    file_name,
                    caption=scenario.caption,
                    force_document=scenario.force_document,
                    voice_note=scenario.mode == "voice",
                    video_note=scenario.mode == "round",
                )
            ]

        message_id = await userbot.send_text(
            tg_chat_id, scenario.text, reply_to=reply_to, markdown=scenario.markdown
        )
        if scenario.edit_to:
            await asyncio.sleep(SELFTEST_STEP_DELAY)
            await userbot.edit_text(tg_chat_id, message_id, scenario.edit_to)
        return [message_id]

    async def _import_history(
        self, tg_chat_id: int, max_chat_id: int, limit: int, account_id: int
    ) -> None:
        """Перенести историю чата и отчитаться в группе."""
        try:
            count = await self._directory.import_history(account_id, max_chat_id, limit)
        except Exception as error:
            logger.exception("Не удалось перенести историю чата %s", max_chat_id)
            await self._send_chunks(tg_chat_id, escape_html(f"История не перенесена: {error}"))
            return
        await self._send_chunks(
            tg_chat_id,
            f"История перенесена: {count} сообщ." if count else "В чате MAX пока нет сообщений.",
        )

    async def _apply_avatar(
        self, userbot: TelegramUserbot, tg_chat_id: int, chat: RemoteChat, account_id: int
    ) -> str:
        """Перенести картинку чата MAX в аватар группы Telegram.

        Возвращает "ok", "skip" (картинки нет или она не годится) либо "flood",
        когда Telegram просит притормозить со сменой фото.
        """
        try:
            data = await self._directory.fetch_avatar(account_id, chat)
        except Exception:
            logger.warning("Не удалось получить аватар чата MAX %s", chat.id, exc_info=True)
            return "skip"
        if not data:
            return "skip"

        # Смена фото — дорогая операция, Telegram ограничивает её частоту жёстче,
        # чем отправку сообщений.
        await asyncio.sleep(self._settings.sync_avatar_delay)
        try:
            await userbot.set_group_photo(tg_chat_id, data)
        except Exception as error:
            wait = flood_wait_seconds(error)
            if wait is not None and wait <= FLOOD_WAIT_TOLERANCE:
                logger.info("Telegram просит подождать %s с перед сменой фото", wait)
                await asyncio.sleep(wait + 1)
                try:
                    await userbot.set_group_photo(tg_chat_id, data)
                except Exception:
                    logger.warning("Аватар группы %s не поставлен", tg_chat_id, exc_info=True)
                    return "flood" if flood_wait_seconds(error) else "skip"
            elif wait is not None:
                logger.warning("Telegram просит подождать %s с — откладываем аватары", wait)
                return "flood"
            else:
                logger.warning("Не удалось поставить аватар группе %s", tg_chat_id, exc_info=True)
                return "skip"
        await self._storage.remember_icon(tg_chat_id, chat.icon_url)
        return "ok"

    async def _userbot_for(self, account: MaxAccount) -> TelegramUserbot | None:
        """Сессия Telegram, от имени которой создаются группы аккаунта."""
        if self._userbots is None:
            return None
        return await self._userbots.authorized(account.owner_id)

    async def _cmd_sync(self, message: TgMessage, command: CommandObject) -> None:
        """Создать недостающие группы под чаты MAX и обновить папку."""
        user_id = message.from_user.id if message.from_user else None
        if not self._may_signup(user_id):
            await message.answer(
                "Подключение к мосту сейчас закрыто — обратитесь к его администратору."
            )
            return
        # Своя очередь на каждого: чужая синхронизация не должна мешать.
        running = self._sync_tasks.get(user_id or 0)
        if running is not None and not running.done():
            await message.answer("Ваша синхронизация уже идёт — дождитесь отчёта.")
            return

        folder = (command.args or "").strip() or self._settings.tg_folder_name
        await message.answer(
            f"Синхронизирую чаты MAX с папкой <b>{escape_html(folder)}</b>. "
            "Это займёт время: Telegram ограничивает частоту создания групп."
        )
        self._sync_tasks[user_id or 0] = asyncio.create_task(
            self._sync_accounts(message, folder), name="max2tg-sync"
        )

    async def _sync_accounts(self, message: TgMessage, folder: str) -> None:
        """Прогнать синхронизацию по всем аккаунтам MAX пользователя."""
        user_id = message.from_user.id if message.from_user else None
        accounts = await self._owner_accounts(user_id)
        if not accounts:
            await self._send_chunks(
                message.chat.id, "У вас нет подключённых аккаунтов MAX. Добавьте: /max_add"
            )
            return
        multi_account = len(accounts) > 1
        for account in accounts:
            if self._accounts.session(account.id) is None:
                await self._send_chunks(
                    message.chat.id,
                    f"Аккаунт {escape_html(account.nickname)} не подключён — пропускаю.",
                )
                continue
            await self._run_sync(message.chat.id, folder, account, multi_account)

    async def _run_sync(
        self,
        report_chat_id: int,
        folder: str,
        account: MaxAccount,
        multi_account: bool = False,
    ) -> None:
        """Синхронизация одного аккаунта: группы, названия, аватары, история."""
        userbot = await self._userbot_for(account)
        if userbot is None:
            await self._send_chunks(
                report_chat_id,
                f"Для аккаунта <b>{escape_html(account.nickname)}</b> нет пользовательской "
                "сессии Telegram, поэтому группы автоматически не создаются.\n"
                "Либо войдите: /login в личке с ботом, либо создайте группу сами, "
                "добавьте меня администратором и выполните /bind.",
            )
            return
        created = 0
        existing = 0
        deferred = 0
        avatars = 0
        renamed = 0
        imported = 0
        history_limit = self._settings.history_import_limit
        avatars_allowed = True
        avatars_deferred = False
        renames_allowed = True
        renames_deferred = 0
        failures: list[str] = []

        try:
            chats = await self._directory.list_chats(account.id)
        except Exception as error:
            await self._send_chunks(
                report_chat_id, escape_html(f"Не удалось получить список чатов MAX: {error}")
            )
            return

        for chat in chats:
            binding = await self._storage.get_by_max(chat.id, account.id)
            if binding is not None:
                existing += 1
                # База помнит лишь то, что мост когда-то сделал: название могли
                # переименовать, аватар удалить. Сверяемся с самой группой.
                state = await userbot.group_state(binding.tg_chat_id)
                if state is None:
                    failures.append(f"{chat.title}: группа недоступна, привязка осталась")
                    continue

                wanted_title = _group_title(chat.title)
                if chat.title and state.title != wanted_title:
                    if not renames_allowed:
                        # Дальше по списку ответ будет тот же — не тратим попытки.
                        renames_deferred += 1
                    else:
                        try:
                            await userbot.rename_group(binding.tg_chat_id, wanted_title)
                            await self._storage.update_binding(binding.tg_chat_id, title=chat.title)
                            renamed += 1
                        except Exception as error:
                            wait = flood_wait_seconds(error)
                            if wait is not None:
                                renames_allowed = False
                                renames_deferred += 1
                                logger.info(
                                    "Переименования отложены: Telegram просит подождать %s с", wait
                                )
                            else:
                                logger.warning(
                                    "Не удалось переименовать группу %s",
                                    binding.tg_chat_id,
                                    exc_info=True,
                                )
                                failures.append(
                                    f"{chat.title}: переименование не удалось ({error})"
                                )

                # Историю подтягиваем и для давних привязок: в группе может
                # не хватать переписки, а повторы отсекает таблица соответствий.
                if history_limit > 0:
                    imported += await self._directory.import_history(
                        account.id, chat.id, history_limit
                    )
                    await asyncio.sleep(self._settings.sync_create_delay)

                needs_avatar = bool(chat.icon_url) and (
                    not state.has_photo or chat.icon_url != binding.icon_url
                )
                if avatars_allowed and needs_avatar:
                    result = await self._apply_avatar(userbot, binding.tg_chat_id, chat, account.id)
                    if result == "ok":
                        avatars += 1
                    elif result == "flood":
                        avatars_allowed = False
                        avatars_deferred = True
                continue
            if created >= self._settings.sync_group_limit:
                deferred += 1
                continue
            try:
                group = await userbot.create_group(
                    title=_group_title(chat.title),
                    bot_username=f"@{self._username}" if self._username else "",
                    about=f"Мост с чатом MAX {chat.id}",
                )
            except UserbotNotConfigured as error:
                failures.append(str(error))
                break
            except Exception as error:
                wait = flood_wait_seconds(error)
                if wait is not None:
                    failures.append(
                        f"Telegram просит подождать {wait} с — остальные группы "
                        "создам при следующем /sync"
                    )
                    break
                logger.exception("Не удалось создать группу для чата MAX %s", chat.id)
                failures.append(f"{chat.title}: {error}")
                continue

            await self._storage.bind(group.chat_id, chat.id, chat.title, account_id=account.id)
            created += 1
            if history_limit > 0:
                imported += await self._directory.import_history(account.id, chat.id, history_limit)
            if avatars_allowed:
                result = await self._apply_avatar(userbot, group.chat_id, chat, account.id)
                if result == "ok":
                    avatars += 1
                elif result == "flood":
                    avatars_allowed = False
                    avatars_deferred = True
            await asyncio.sleep(self._settings.sync_create_delay)

        # Папка своя на каждый аккаунт, поэтому в неё идут только его чаты.
        bindings = await self._storage.list_bindings(account.id)
        folder_name = _folder_title(folder, account.nickname, multi_account)
        folder_note = ""
        try:
            await userbot.ensure_folder(folder_name, [item.tg_chat_id for item in bindings])
            folder_note = f"Папка <b>{escape_html(folder_name)}</b>: {len(bindings)} чатов."
        except Exception as error:
            logger.exception("Не удалось обновить папку %s", folder_name)
            folder_note = escape_html(f"Папку обновить не удалось: {error}")

        lines = [
            f"<b>Синхронизация завершена — {escape_html(account.nickname)}</b>",
            f"Создано групп: {created}",
            f"Уже были привязаны: {existing}",
        ]
        if renamed:
            lines.append(f"Переименовано групп: {renamed}")
        if imported:
            lines.append(f"Перенесено сообщений из истории: {imported}")
        if avatars:
            lines.append(f"Обновлено аватаров: {avatars}")
        if renames_deferred:
            lines.append(
                f"Переименование отложено для {renames_deferred} групп "
                "(Telegram ограничивает частоту) — повторите /sync позже"
            )
        if avatars_deferred:
            lines.append(
                "Остальные аватары Telegram пока не принимает (ограничение частоты) — "
                "повторите /sync через несколько минут"
            )
        if deferred:
            lines.append(
                f"Отложено до следующего /sync: {deferred} "
                f"(лимит {self._settings.sync_group_limit} за раз)"
            )
        lines.append(folder_note)
        if failures:
            lines.append("")
            lines.append("<b>Проблемы</b>")
            lines.extend(escape_html(item) for item in failures[:10])
        await self._send_chunks(report_chat_id, "\n".join(line for line in lines if line))

    async def _cmd_status(self, message: TgMessage) -> None:
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None:
            await message.answer("Группа не привязана. Используйте /chats и /bind.")
            return
        title = escape_html(binding.title or "без названия")
        account = await self._storage.get_account(binding.account_id)
        live = self._accounts.session(binding.account_id) is not None
        lines = [
            f"Группа привязана к чату MAX <b>{title}</b> (<code>{binding.max_chat_id}</code>).",
            f"Аккаунт MAX: <b>{escape_html(account.nickname if account else '?')}</b> "
            f"({'на связи' if live else 'не подключён'}).",
            "Пересылка: включена."
            if binding.enabled
            else "Пересылка приостановлена — возобновить: /resume",
        ]
        moved = await self._storage.count_messages(message.chat.id)
        if moved:
            lines.append(f"Перенесено сообщений: {moved}.")
        if not live:
            lines.append(
                "\n⚠️ Аккаунт MAX сейчас не подключён — сообщения не ходят. "
                "Проверьте /max_list, при необходимости подключите заново: /max_add"
            )
        if self._privacy_mode_on and not await self._is_bot_admin(message.chat.id):
            lines.append(
                "\n⚠️ У бота включён privacy mode, и в этой группе он не администратор — "
                "сообщения отсюда в MAX не уходят. Назначьте его администратором группы "
                "(мгновенно) либо выключите режим в @BotFather → Bot Settings → "
                "Group Privacy → Turn off и перезапустите мост."
            )
        await message.answer("\n".join(lines))

    async def _is_bot_admin(self, chat_id: int) -> bool:
        """Проверить, администратор ли бот в этой группе.

        Администратор получает все сообщения независимо от privacy mode, поэтому
        предупреждать о режиме имеет смысл только там, где прав у бота нет.
        """
        if not self._bot_id:
            return False
        try:
            member = await self.bot.get_chat_member(chat_id, self._bot_id)
        except Exception:
            logger.debug("Не удалось проверить права бота в чате %s", chat_id, exc_info=True)
            return False
        return member.status in {"administrator", "creator"}

    async def _handle_group_message(self, message: TgMessage) -> None:
        """Обычное сообщение в группе — кандидат на пересылку в MAX."""
        if message.from_user and message.from_user.is_bot:
            return
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None or not binding.enabled:
            return

        if message.media_group_id:
            self._buffer_album(message)
            return

        normalized = await self._normalize(message)
        if normalized is not None:
            await self._on_message(normalized)

    async def _handle_edited_message(self, message: TgMessage) -> None:
        """Правка сообщения в группе."""
        if message.from_user and message.from_user.is_bot:
            return
        binding = await self._storage.get_by_tg(message.chat.id)
        if binding is None or not binding.enabled:
            return
        normalized = await self._normalize(message)
        if normalized is None:
            return
        normalized.is_edit = True
        # Вложения при правке не пересобираем: MAX не умеет менять их у отправленного
        # сообщения, а повторная заливка создала бы дубликат.
        normalized.attachments.clear()
        if not normalized.text:
            return
        await self._on_message(normalized)

    # -- альбомы --------------------------------------------------------- #

    def _buffer_album(self, message: TgMessage) -> None:
        """Скопить сообщения одного альбома и отправить их вместе."""
        group_id = str(message.media_group_id)
        self._albums.setdefault(group_id, []).append(message)
        task = self._album_tasks.get(group_id)
        if task is not None:
            task.cancel()
        self._album_tasks[group_id] = asyncio.create_task(self._flush_album(group_id))

    async def _flush_album(self, group_id: str) -> None:
        try:
            await asyncio.sleep(self._settings.media_group_delay)
        except asyncio.CancelledError:
            return
        messages = self._albums.pop(group_id, [])
        self._album_tasks.pop(group_id, None)
        if not messages:
            return
        messages.sort(key=lambda item: item.message_id)
        base = await self._normalize(messages[0])
        if base is None:
            return
        for extra in messages[1:]:
            part = await self._normalize(extra)
            if part is None:
                continue
            base.attachments.extend(part.attachments)
            if part.text and not base.text:
                base.text = part.text
                base.spans = part.spans
            base.notes.extend(part.notes)
        await self._on_message(base)

    # -- нормализация ---------------------------------------------------- #

    async def _normalize(self, message: TgMessage) -> NormalizedMessage | None:
        """Превратить сообщение Telegram в нормализованное представление."""
        text = message.text or message.caption or ""
        spans = entities_to_spans(message.entities or message.caption_entities)
        normalized = NormalizedMessage(
            source=Platform.TELEGRAM,
            source_chat_id=message.chat.id,
            source_message_id=str(message.message_id),
            author=self._display_name(message),
            text=text,
            spans=spans,
            reply_to_source_id=(
                str(message.reply_to_message.message_id) if message.reply_to_message else None
            ),
        )

        attachment = self._extract_attachment(message)
        if attachment is not None:
            normalized.attachments.append(attachment)

        note = self._describe_special(message)
        if note:
            normalized.notes.append(note)

        return normalized if not normalized.is_empty else None

    @staticmethod
    def _display_name(message: TgMessage) -> str:
        user = message.from_user
        if user is None:
            return message.chat.title or "Telegram"
        name = " ".join(part for part in (user.first_name, user.last_name) if part)
        return name or (user.username or str(user.id))

    def _extract_attachment(self, message: TgMessage) -> NormalizedAttachment | None:
        """Достать вложение сообщения (Telegram кладёт в сообщение не больше одного)."""
        if message.photo:
            photo = message.photo[-1]
            return self._build(
                photo.file_id,
                AttachmentKind.PHOTO,
                f"photo_{message.message_id}.jpg",
                "image/jpeg",
                photo.file_size,
                width=photo.width,
                height=photo.height,
            )
        if message.video:
            video = message.video
            return self._build(
                video.file_id,
                AttachmentKind.VIDEO,
                video.file_name or f"video_{message.message_id}.mp4",
                video.mime_type or "video/mp4",
                video.file_size,
                duration=video.duration,
                width=video.width,
                height=video.height,
            )
        if message.video_note:
            note = message.video_note
            return self._build(
                note.file_id,
                AttachmentKind.VIDEO_NOTE,
                f"video_note_{message.message_id}.mp4",
                "video/mp4",
                note.file_size,
                duration=note.duration,
            )
        if message.voice:
            voice = message.voice
            return self._build(
                voice.file_id,
                AttachmentKind.VOICE,
                f"voice_{message.message_id}.ogg",
                voice.mime_type or "audio/ogg",
                voice.file_size,
                duration=voice.duration,
            )
        if message.audio:
            audio = message.audio
            name = audio.file_name or f"{audio.performer or 'audio'} - {audio.title or ''}".strip()
            return self._build(
                audio.file_id,
                AttachmentKind.AUDIO,
                safe_filename(name, f"audio_{message.message_id}.mp3"),
                audio.mime_type or "audio/mpeg",
                audio.file_size,
                duration=audio.duration,
            )
        if message.animation:
            animation = message.animation
            return self._build(
                animation.file_id,
                AttachmentKind.ANIMATION,
                animation.file_name or f"animation_{message.message_id}.mp4",
                animation.mime_type or "video/mp4",
                animation.file_size,
                duration=animation.duration,
                width=animation.width,
                height=animation.height,
            )
        if message.sticker:
            sticker = message.sticker
            suffix = ".webm" if sticker.is_video else (".tgs" if sticker.is_animated else ".webp")
            return self._build(
                sticker.file_id,
                AttachmentKind.STICKER,
                f"sticker_{sticker.file_unique_id}{suffix}",
                guess_mime(f"x{suffix}", "image/webp"),
                sticker.file_size,
                note=sticker.emoji,
            )
        if message.document:
            document = message.document
            return self._build(
                document.file_id,
                AttachmentKind.DOCUMENT,
                document.file_name or f"file_{message.message_id}.bin",
                document.mime_type,
                document.file_size,
            )
        return None

    def _build(
        self,
        file_id: str,
        kind: AttachmentKind,
        filename: str,
        mime_type: str | None,
        size: int | None,
        **extra: Any,
    ) -> NormalizedAttachment:
        return NormalizedAttachment(
            kind=kind,
            filename=safe_filename(filename),
            mime_type=mime_type,
            size=size,
            loader=self._make_loader(file_id, size),
            **extra,
        )

    def _make_loader(self, file_id: str, size: int | None) -> Callable[[], Awaitable[bytes]]:
        limit = self._settings.tg_download_limit

        async def loader() -> bytes:
            if size is not None and size > limit:
                raise ValueError(
                    f"файл больше лимита Bot API ({human_size(size)} > {human_size(limit)})"
                )
            buffer = await self.bot.download(file_id)
            if buffer is None:
                raise ValueError("Telegram не отдал содержимое файла")
            return buffer.read()

        return loader

    @staticmethod
    def _describe_special(message: TgMessage) -> str | None:
        """Описать то, что нельзя перенести файлом (гео, контакты, опросы)."""
        if message.location:
            location = message.location
            return (
                f"📍 Геопозиция: {location.latitude:.6f}, {location.longitude:.6f}\n"
                f"https://maps.google.com/?q={location.latitude},{location.longitude}"
            )
        if message.venue:
            venue = message.venue
            return f"📍 {venue.title}\n{venue.address}"
        if message.contact:
            contact = message.contact
            name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
            return f"👤 Контакт: {name} — {contact.phone_number}"
        if message.poll:
            options = "\n".join(f"• {option.text}" for option in message.poll.options)
            return f"📊 Опрос: {message.poll.question}\n{options}"
        if message.dice:
            return f"🎲 {message.dice.emoji} — {message.dice.value}"
        return None

    # ------------------------------------------------------------------ #
    # Доставка сообщений из MAX
    # ------------------------------------------------------------------ #

    async def deliver(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        reply_to_target_id: str | None,
    ) -> list[str]:
        """Отправить нормализованное сообщение в группу Telegram."""
        reply_to = int(reply_to_target_id) if reply_to_target_id else None
        header = _author_header(message.author)
        body = spans_to_html(message.text, message.spans) if message.text else ""
        notes = "\n".join(escape_html(note) for note in message.notes)
        quote = ""
        if reply_to is None and message.reply_preview:
            # Оригинала в этой группе нет (например, он был до запуска моста) —
            # тогда цитата показывается текстом, иначе ответ выглядит оторванным.
            quote = f"<blockquote>{escape_html(message.reply_preview)}</blockquote>"
        caption = "\n".join(part for part in (header, quote, body, notes) if part)

        sent: list[str] = []
        albums, singles = self._split_attachments(message.attachments)

        if not message.attachments:
            sent.extend(await self._send_chunks(target_chat_id, caption, reply_to))
            return sent

        caption_used = False
        for batch in albums:
            media = await self._build_album(batch, caption if not caption_used else "")
            if not media:
                continue
            caption_used = True
            messages = await self._call(
                target_chat_id,
                partial(
                    self.bot.send_media_group,
                    chat_id=target_chat_id,
                    media=media,
                    reply_to_message_id=reply_to,
                ),
            )
            sent.extend(str(item.message_id) for item in messages)

        for attachment in singles:
            text = caption if not caption_used else ""
            message_id = await self._send_single(target_chat_id, attachment, text, reply_to)
            if message_id is not None:
                caption_used = True
                sent.append(message_id)

        if not caption_used and caption:
            sent.extend(await self._send_chunks(target_chat_id, caption, reply_to))
        return sent

    async def edit(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        target_message_id: str,
    ) -> bool:
        """Отредактировать ранее пересланное сообщение."""
        header = _author_header(message.author)
        body = spans_to_html(message.text, message.spans) if message.text else ""
        text = "\n".join(part for part in (header, body) if part)
        if not text or len(text) > TEXT_LIMIT:
            return False
        message_id = int(target_message_id)
        try:
            await self._call(
                target_chat_id,
                lambda: self.bot.edit_message_text(
                    chat_id=target_chat_id, message_id=message_id, text=text
                ),
            )
            return True
        except TelegramBadRequest:
            pass
        try:
            await self._call(
                target_chat_id,
                lambda: self.bot.edit_message_caption(
                    chat_id=target_chat_id, message_id=message_id, caption=text[:CAPTION_LIMIT]
                ),
            )
            return True
        except TelegramBadRequest:
            return False

    async def send_service(self, chat_id: int, text: str) -> None:
        """Служебное сообщение моста."""
        await self._send_chunks(chat_id, text)

    # -- внутреннее ------------------------------------------------------ #

    @staticmethod
    def _split_attachments(
        attachments: list[NormalizedAttachment],
    ) -> tuple[list[list[NormalizedAttachment]], list[NormalizedAttachment]]:
        """Разложить вложения на альбомы (фото/видео) и одиночные отправки."""
        album_items = [item for item in attachments if item.kind in ALBUM_KINDS]
        singles = [item for item in attachments if item.kind not in ALBUM_KINDS]
        if len(album_items) < 2:
            return [], attachments
        albums = [
            album_items[index : index + ALBUM_LIMIT]
            for index in range(0, len(album_items), ALBUM_LIMIT)
        ]
        return albums, singles

    async def _build_album(
        self, batch: list[NormalizedAttachment], caption: str
    ) -> list[AlbumItem]:
        media: list[AlbumItem] = []
        for index, attachment in enumerate(batch):
            payload = await self._load(attachment)
            if payload is None:
                continue
            file = BufferedInputFile(payload, filename=attachment.filename)
            item_caption = caption[:CAPTION_LIMIT] if index == 0 and caption else None
            if attachment.kind is AttachmentKind.PHOTO:
                media.append(InputMediaPhoto(media=file, caption=item_caption))
            else:
                media.append(InputMediaVideo(media=file, caption=item_caption))
        return media

    async def _send_single(
        self,
        chat_id: int,
        attachment: NormalizedAttachment,
        caption: str,
        reply_to: int | None,
    ) -> str | None:
        """Отправить одно вложение подходящим методом Bot API."""
        payload = await self._load(attachment)
        if payload is None:
            # Часть вложений MAX принципиально не отдаёт файлом — тогда вместо
            # технической ошибки уходит понятная человеку строка.
            note = attachment.note or f"⚠️ Вложение «{attachment.filename}» не перенесено"
            await self._send_chunks(chat_id, escape_html(note), reply_to)
            return None

        kind = attachment.kind
        if kind is AttachmentKind.STICKER:
            # Стикеры MAX — обычные PNG, Bot API принимает стикером только WEBP.
            converted = to_telegram_sticker(payload)
            if converted is None:
                kind = AttachmentKind.PHOTO
            else:
                payload = converted
                attachment.filename = attachment.filename.rsplit(".", 1)[0] + ".webp"

        file = BufferedInputFile(payload, filename=attachment.filename)
        short_caption = caption[:CAPTION_LIMIT] if caption else None
        common: dict[str, Any] = {"chat_id": chat_id, "reply_to_message_id": reply_to}

        # Стикер и кружок подписи не принимают, поэтому автор уходит отдельной
        # строкой — и именно перед вложением: после него это выглядит как
        # пустая реплика неизвестно к чему.
        if kind in CAPTIONLESS_KINDS and caption:
            await self._send_chunks(chat_id, caption, reply_to)
            reply_to = None
            common = {"chat_id": chat_id, "reply_to_message_id": None}

        senders: dict[AttachmentKind, tuple[Any, str, bool]] = {
            AttachmentKind.PHOTO: (self.bot.send_photo, "photo", True),
            AttachmentKind.VIDEO: (self.bot.send_video, "video", True),
            AttachmentKind.VIDEO_NOTE: (self.bot.send_video_note, "video_note", False),
            AttachmentKind.VOICE: (self.bot.send_voice, "voice", True),
            AttachmentKind.AUDIO: (self.bot.send_audio, "audio", True),
            AttachmentKind.ANIMATION: (self.bot.send_animation, "animation", True),
            AttachmentKind.STICKER: (self.bot.send_sticker, "sticker", False),
        }
        method, argument, supports_caption = senders.get(
            kind, (self.bot.send_document, "document", True)
        )
        kwargs: dict[str, Any] = {argument: file, **common}
        if supports_caption:
            kwargs["caption"] = short_caption

        try:
            result = await self._call(chat_id, partial(method, **kwargs))
        except TelegramBadRequest as error:
            logger.warning("Telegram отклонил вложение %s: %s", attachment.filename, error)
            fallback = await self._load_alternative(attachment)
            if fallback is not None:
                # Анимацию стикера не приняли — уходит статичное превью.
                alt_file = BufferedInputFile(
                    fallback, filename=attachment.filename.rsplit(".", 1)[0] + ".png"
                )
                result = await self._call(
                    chat_id, partial(self.bot.send_photo, photo=alt_file, **common)
                )
            else:
                result = await self._call(
                    chat_id,
                    partial(
                        self.bot.send_document,
                        document=file,
                        caption=short_caption,
                        **common,
                    ),
                )

        return str(result.message_id)

    async def _load_alternative(self, attachment: NormalizedAttachment) -> bytes | None:
        """Запасное содержимое вложения, если основное Telegram не принял."""
        if attachment.alt_loader is None:
            return None
        try:
            return await attachment.alt_loader()
        except Exception:
            logger.debug("Запасной источник тоже недоступен", exc_info=True)
            return None

    async def _load(self, attachment: NormalizedAttachment) -> bytes | None:
        """Скачать содержимое вложения, соблюдая лимит выгрузки в Telegram."""
        if attachment.size is not None and attachment.size > self._settings.tg_upload_limit:
            logger.warning(
                "Вложение %s (%s) больше лимита Telegram — пропускаем",
                attachment.filename,
                human_size(attachment.size),
            )
            attachment.note = f"файл {human_size(attachment.size)} превышает лимит Telegram"
            return None
        try:
            return await attachment.loader()
        except Exception as error:
            logger.warning("Не удалось получить вложение %s: %s", attachment.filename, error)
            if not attachment.note:
                attachment.note = f"⚠️ «{attachment.filename}» не перенесено: {error}"
            return None

    async def _send_chunks(self, chat_id: int, text: str, reply_to: int | None = None) -> list[str]:
        """Отправить текст, разбив его на части по лимиту Bot API."""
        if not text:
            return []
        sent: list[str] = []
        for chunk in _split_text(text, TEXT_LIMIT):
            answer_to = reply_to if not sent else None
            try:
                result = await self._call(
                    chat_id,
                    partial(
                        self.bot.send_message,
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=answer_to,
                        disable_web_page_preview=True,
                    ),
                )
            except TelegramBadRequest as error:
                # Разбиение длинного текста могло разорвать тег разметки —
                # повторяем без разметки, чтобы сообщение всё-таки дошло.
                logger.warning("Telegram отверг разметку (%s), шлём без неё", error)
                result = await self._call(
                    chat_id,
                    partial(
                        self.bot.send_message,
                        chat_id=chat_id,
                        text=_strip_html(chunk),
                        reply_to_message_id=answer_to,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    ),
                )
            sent.append(str(result.message_id))
        return sent

    async def _call(self, chat_id: int, factory: Callable[[], Awaitable[T]]) -> T:
        """Выполнить вызов Bot API с троттлингом и обработкой flood wait."""
        attempts = 4
        for attempt in range(1, attempts + 1):
            await self._limiter.acquire(chat_id)
            try:
                return await factory()
            except TelegramRetryAfter as error:
                delay = float(error.retry_after) + 1.0
                logger.warning("Telegram flood wait: ждём %.1f с", delay)
                await asyncio.sleep(delay)
            except TelegramBadRequest:
                raise
            except Exception:
                if attempt == attempts:
                    raise
                await asyncio.sleep(2.0 * attempt)
        raise RuntimeError("не удалось выполнить запрос к Telegram")


def _group_title(chat_title: str) -> str:
    """Название группы повторяет название чата MAX."""
    return chat_title.strip() or "Чат MAX"


def _folder_title(base: str, nickname: str, multi_account: bool) -> str:
    """Название папки: своя папка на каждый аккаунт MAX.

    Пока аккаунт один, папка называется так, как попросили в /sync. Как только
    аккаунтов становится несколько, их чаты нужно различать — и делать это
    удобнее папками, а не префиксами в названии каждой группы.
    """
    if not multi_account:
        return base.strip()[:FOLDER_TITLE_LIMIT]
    name = nickname.strip() or base.strip()
    return name[:FOLDER_TITLE_LIMIT]


def _author_header(author: str) -> str:
    """Подпись отправителя первой строкой сообщения."""
    return f"👤 <b>{escape_html(author)}</b>:"


def _strip_html(text: str) -> str:
    """Убрать HTML-разметку, оставив читаемый текст."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _split_text(text: str, limit: int) -> list[str]:
    """Разбить текст на части, стараясь резать по переводам строк."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def _button_title(title: str) -> str:
    """Подпись кнопки: Telegram обрезает длинные, лучше сделать это осмысленно."""
    clean = (title or "Чат MAX").strip()
    return clean if len(clean) <= 40 else clean[:39] + "…"
