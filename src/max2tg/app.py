"""Сборка и запуск моста."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from .adapters.max_manager import MaxAccountManager
from .adapters.telegram_adapter import TelegramAdapter
from .adapters.userbot_pool import UserbotPool
from .bridge import Bridge
from .config import Settings, load_settings
from .crypto import SecretBox
from .db import create_engine, create_session_factory, init_models
from .logging_setup import setup_logging
from .migration import (
    ensure_account_owner,
    migrate_file_session,
    migrate_single_account,
    reencrypt_secrets,
)
from .storage import Storage

logger = logging.getLogger("app")

#: Предел ожидания на каждый шаг остановки, в секундах.
SHUTDOWN_TIMEOUT = 10.0


async def run_bridge(settings: Settings) -> None:
    """Поднять оба адаптера и работать до остановки."""
    engine = create_engine(settings.database_url)
    await init_models(engine)
    secrets = SecretBox(settings.master_key)
    sessions = create_session_factory(engine)
    storage = Storage(sessions, secrets)
    bridge = Bridge(storage)

    if not secrets.enabled:
        logger.warning(
            "MASTER_KEY не задан: токены MAX и сессии Telegram хранятся открытым текстом. "
            "Для общего сервиса это недопустимо."
        )

    await migrate_single_account(storage, sessions, settings)
    await ensure_account_owner(storage, settings)
    await migrate_file_session(storage, settings)
    await reencrypt_secrets(storage, secrets)

    accounts = MaxAccountManager(settings, storage, bridge.submit)
    userbots = UserbotPool(settings, storage)
    telegram_adapter = TelegramAdapter(settings, storage, accounts, bridge.submit, userbots)

    bridge.register(accounts)
    bridge.register(telegram_adapter)

    if not settings.tg_admin_ids:
        logger.warning(
            "TG_ADMIN_IDS не задан: командами управления сможет пользоваться любой "
            "участник группы. Узнать свой идентификатор — команда /id."
        )

    # Сессии MAX поднимаются по сохранённым токенам: новые аккаунты
    # подключаются уже через бота, командой /max_add.
    started = await accounts.start_all()
    bindings = await storage.list_bindings()
    logger.info("Подключено аккаунтов MAX: %d, активных привязок: %d", started, len(bindings))

    tasks = [
        asyncio.create_task(telegram_adapter.run(), name="telegram-polling"),
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if exception is not None:
                logger.error(
                    "Задача %s завершилась ошибкой: %s",
                    task.get_name(),
                    exception,
                    exc_info=exception,
                )
                raise exception
            logger.warning("Задача %s завершилась — останавливаем мост", task.get_name())
    finally:
        await _shutdown(tasks, bridge, accounts, telegram_adapter)
        with contextlib.suppress(Exception):
            await userbots.stop()
        await engine.dispose()


async def _shutdown(
    tasks: list[asyncio.Task[None]],
    bridge: Bridge,
    accounts: MaxAccountManager,
    telegram_adapter: TelegramAdapter,
) -> None:
    """Погасить мост, не давая ему зависнуть на этапе остановки.

    pyromax сам восстанавливает соединение и может проглотить отмену, поэтому
    каждый шаг завершения ограничен по времени.
    """
    for task in tasks:
        task.cancel()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=SHUTDOWN_TIMEOUT
        )
    for step, coroutine in (
        ("очередь моста", bridge.close()),
        ("сессии MAX", accounts.stop()),
        ("адаптер Telegram", telegram_adapter.stop()),
    ):
        try:
            await asyncio.wait_for(coroutine, timeout=SHUTDOWN_TIMEOUT)
        except TimeoutError:
            logger.warning("Остановка (%s) не уложилась в %s с", step, SHUTDOWN_TIMEOUT)
        except Exception:
            logger.debug("Ошибка при остановке (%s)", step, exc_info=True)


async def main() -> None:
    """Точка входа приложения."""
    settings = load_settings()
    setup_logging(settings.log_level)
    # pyromax хранит токен MAX в tokens.json рядом с рабочим каталогом.
    if str(settings.state_dir) not in ("", "."):
        os.makedirs(settings.state_dir, exist_ok=True)
        os.chdir(settings.state_dir)
    try:
        await run_bridge(settings)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Остановка по запросу пользователя")


async def login_telegram() -> None:
    """Интерактивный вход в пользовательскую сессию Telegram (для /sync).

    Сессия сохраняется в базе за первым администратором из ``TG_ADMIN_IDS``:
    у каждого пользователя моста своя, привязанная к его Telegram-аккаунту.
    """
    settings = load_settings()
    setup_logging(settings.log_level)
    if not settings.tg_admin_ids:
        logger.error("Не задан TG_ADMIN_IDS — некому записать сессию. Узнать свой id: /id")
        return

    engine = create_engine(settings.database_url)
    await init_models(engine)
    storage = Storage(create_session_factory(engine), SecretBox(settings.master_key))
    pool = UserbotPool(settings, storage)
    userbot = await pool.get(settings.tg_admin_ids[0])
    if not userbot.configured:
        logger.error(
            "Не заданы TG_API_ID и TG_API_HASH. Возьмите их на my.telegram.org "
            "(API development tools) и добавьте в .env"
        )
        return
    try:
        await userbot.login_interactive()
    finally:
        await engine.dispose()
