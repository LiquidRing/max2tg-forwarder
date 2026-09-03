"""Перенос установки, созданной до многопользовательского режима.

Раньше мост работал с единственным аккаунтом MAX: токен лежал в ``tokens.json``,
а привязки не знали ни про владельца, ни про аккаунт. Здесь эта установка
переносится на новую схему, чтобы обновление не потребовало переподключать
аккаунт и заново привязывать чаты.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .crypto import SecretBox
from .db import Binding, MaxAccount, MessageMap, TelegramSession
from .storage import Storage

logger = logging.getLogger("migration")

#: Имя файла, в котором pyromax хранил токен единственного аккаунта.
LEGACY_TOKEN_FILE = "tokens.json"

#: Ключ токена веб-сессии в этом файле.
LEGACY_TOKEN_KEY = "ENVELOPE_MAX_TOKEN_V11WebSocketTransportWEB"


def read_legacy_token(state_dir: Path | str = ".") -> str | None:
    """Прочитать токен MAX из файла старой установки."""
    path = Path(state_dir) / LEGACY_TOKEN_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Не удалось прочитать %s", path, exc_info=True)
        return None
    for key, value in data.items():
        if key.startswith(LEGACY_TOKEN_KEY) and isinstance(value, str) and value:
            return value
    return None


async def migrate_single_account(
    storage: Storage,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int | None:
    """Завести аккаунт для старой установки и привязать к нему прежние чаты.

    Возвращает идентификатор созданного аккаунта или ``None``, если переносить
    нечего (новая установка либо миграция уже выполнена).
    """
    if await storage.list_accounts():
        return None

    bindings = await storage.list_bindings()
    token = read_legacy_token(settings.state_dir)
    if not bindings and not token:
        return None

    owner_id = settings.tg_admin_ids[0] if settings.tg_admin_ids else 0
    account = await storage.add_account(owner_id, nickname="MAX")
    if token:
        await storage.save_account_token(account.id, token=token)

    async with sessions() as session, session.begin():
        await session.execute(
            update(Binding).where(Binding.account_id == 0).values(account_id=account.id)
        )
        await session.execute(
            update(MessageMap).where(MessageMap.account_id == 0).values(account_id=account.id)
        )

    logger.warning(
        "Перенос старой установки: создан аккаунт MAX %s (владелец %s), "
        "привязок перенесено %d, токен %s",
        account.id,
        owner_id or "не задан — укажите TG_ADMIN_IDS",
        len(bindings),
        "найден" if token else "не найден, потребуется /max_add",
    )
    return int(account.id)


async def ensure_account_owner(storage: Storage, settings: Settings) -> None:
    """Проставить владельца аккаунтам, оставшимся без него после переноса."""
    if not settings.tg_admin_ids:
        return
    owner_id = settings.tg_admin_ids[0]
    for account in await storage.list_accounts():
        if account.owner_id:
            continue
        async with storage.sessions() as session, session.begin():
            stored = await session.get(MaxAccount, account.id)
            if stored is not None:
                stored.owner_id = owner_id
                logger.info("Аккаунту MAX %s назначен владелец %s", account.id, owner_id)


async def migrate_file_session(storage: Storage, settings: Settings) -> None:
    """Разобрать наследство одиночной установки.

    Раньше и сессия Telegram, и аккаунт MAX были одни на весь мост, поэтому
    владельца у них нет. Спрашивать его неоткуда, но старая сессия сама знает,
    чья она: подключаемся ею и берём идентификатор у Telegram.
    """
    if settings.tg_admin_ids:
        await adopt_file_session(storage, settings, settings.tg_admin_ids[0])

    orphans = [item for item in await storage.list_accounts() if not item.owner_id]
    if not orphans:
        return

    owner_id = settings.tg_admin_ids[0] if settings.tg_admin_ids else None
    if owner_id is None:
        owner_id = await _owner_of_file_session(storage, settings)
    if owner_id is None:
        logger.warning(
            "Аккаунты MAX %s остались без владельца: задайте TG_ADMIN_IDS "
            "или напишите боту в личку — он закрепит их за первым обратившимся.",
            ", ".join(str(item.id) for item in orphans),
        )
        return

    claimed = await storage.claim_orphan_accounts(owner_id)
    logger.warning(
        "Перенос старой установки: аккаунты MAX %s закреплены за пользователем %s",
        ", ".join(str(item) for item in claimed),
        owner_id,
    )


async def _owner_of_file_session(storage: Storage, settings: Settings) -> int | None:
    """Чей это Telegram — спрашиваем у самой старой сессии."""
    exported = await asyncio.to_thread(_export_file_session, Path(f"{settings.tg_session}.session"))
    if exported is None:
        return None

    from .adapters.telegram_userbot import TelegramUserbot

    userbot = TelegramUserbot(settings, exported)
    if not userbot.configured:
        return None
    try:
        owner_id = await userbot.owner_id()
    except Exception:
        logger.warning("Не удалось опознать владельца старой сессии Telegram", exc_info=True)
        return None
    finally:
        with suppress(Exception):
            await userbot.disconnect()

    if owner_id is not None and not await storage.get_tg_session(owner_id):
        await storage.save_tg_session(owner_id, exported)
        logger.warning(
            "Перенос старой установки: сессия Telegram закреплена за пользователем %s",
            owner_id,
        )
    return owner_id


async def adopt_file_session(storage: Storage, settings: Settings, owner_id: int) -> None:
    """Закрепить старую файловую сессию Telethon за конкретным пользователем.

    Раньше сессия была одна на весь мост и лежала файлом рядом с процессом.
    В общем сервисе у каждого пользователя своя, поэтому единственную старую
    отдаём владельцу наследства — иначе он потерял бы уже выполненный вход.
    """
    if await storage.get_tg_session(owner_id):
        return

    path = Path(f"{settings.tg_session}.session")
    try:
        exported = await asyncio.to_thread(_export_file_session, path)
    except Exception:
        logger.warning("Не удалось перенести файловую сессию Telegram", exc_info=True)
        return
    if exported is None:
        return
    await storage.save_tg_session(owner_id, exported)

    logger.warning(
        "Перенос старой установки: сессия Telegram из %s закреплена за пользователем %s",
        path.name,
        owner_id,
    )


def _export_file_session(path: Path) -> str | None:
    """Прочитать файловую сессию Telethon и отдать её строкой (блокирующе)."""
    if not path.exists():
        return None
    from telethon.sessions import SQLiteSession, StringSession

    legacy = SQLiteSession(str(path.with_suffix("")))
    try:
        if legacy.auth_key is None:
            return None
        string = StringSession()
        string.set_dc(legacy.dc_id, legacy.server_address, legacy.port)
        string.auth_key = legacy.auth_key
        return str(string.save())
    finally:
        legacy.close()


async def reencrypt_secrets(storage: Storage, secrets: SecretBox) -> None:
    """Перешифровать секреты, сохранённые до появления мастер-ключа.

    Иначе токен MAX и сессии Telegram так и лежали бы открытым текстом:
    ``SecretBox`` их читает, но сам по себе ничего не переписывает.
    """
    if not secrets.enabled:
        return

    updated = 0
    for account in await storage.list_accounts():
        if account.token and not secrets.is_encrypted(account.token):
            await storage.save_account_token(account.id, account.token)
            updated += 1

    async with storage.sessions() as session:
        rows = list(await session.scalars(select(TelegramSession)))
    for row in rows:
        if row.session and not secrets.is_encrypted(row.session):
            await storage.save_tg_session(row.owner_id, row.session)
            updated += 1

    if updated:
        logger.warning("Секретов перешифровано под MASTER_KEY: %d", updated)
