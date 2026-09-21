"""Кто чем вправе распоряжаться в общем мосте.

Мост обслуживает много людей сразу, поэтому проверки прав живут отдельным
файлом: одна ошибка здесь означает, что посторонний перевесит чужую группу на
свой аккаунт MAX и начнёт читать её переписку.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, User
from aiogram.types import Message as TgMessage

from max2tg.adapters.telegram_adapter import TelegramAdapter
from max2tg.config import Settings
from max2tg.db import create_engine, create_session_factory, init_models
from max2tg.models import NormalizedMessage
from max2tg.storage import Storage

FAKE_TOKEN = "123456789:AAHfakeTokenForTestsOnly_00000000000"
BRIDGE_ADMIN = 1
OWNER = 7
STRANGER = 99
GROUP = -1001234567890
OTHER_GROUP = -1009876543210
MAX_CHAT = 555001


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "TG_BOT_TOKEN": FAKE_TOKEN,
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "TG_ADMIN_IDS": [BRIDGE_ADMIN],
        "TG_API_ID": None,
        "TG_API_HASH": None,
        "MASTER_KEY": None,
        "TG_PROXY": None,
        "TG_USERBOT_PROXY": None,
    }
    values.update({key.upper(): value for key, value in overrides.items()})
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
async def storage() -> Storage:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    return Storage(create_session_factory(engine))


class _Directory:
    """Справочник чатов MAX: у каждого аккаунта свои."""

    def __init__(self) -> None:
        self.synced: list[int] = []

    async def list_chats(self, account_id: int, query: str | None = None) -> list:
        from max2tg.models import RemoteChat

        return [RemoteChat(id=account_id * 100, title=f"Чат аккаунта {account_id}", type="DIALOG")]

    async def resolve_chat(self, account_id: int, chat_id: int):
        from max2tg.models import RemoteChat

        return RemoteChat(id=chat_id, title="Чат", type="DIALOG")

    async def import_history(self, account_id: int, chat_id: int, limit: int) -> int:
        return 0

    async def fetch_avatar(self, account_id: int, chat) -> bytes | None:
        return None

    def session(self, account_id: int) -> object:
        return object()


async def _sink(message: NormalizedMessage) -> None:
    return None


def _message(user_id: int, chat_id: int = GROUP, chat_type: str = "supergroup") -> TgMessage:
    return TgMessage(  # type: ignore[arg-type]
        message_id=10,
        date=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        chat=Chat(id=chat_id, type=chat_type, title="Группа"),
        from_user=User(id=user_id, is_bot=False, first_name="Кто-то"),
    )


class _Adapter:
    """Адаптер с перехваченными ответами и правами в группе."""

    def __init__(self, adapter: TelegramAdapter) -> None:
        self.adapter = adapter
        self.replies: list[str] = []
        self.markups: list[Any] = []
        self.group_admins: set[int] = set()

        async def answer(text: str, **kwargs: Any) -> None:
            self.replies.append(text)

        async def send_chunks(
            chat_id: int, text: str, reply_to: int | None = None, reply_markup: Any = None
        ) -> None:
            self.replies.append(text)
            self.markups.append(reply_markup)

        async def group_admin(chat_id: int, user_id: int | None) -> bool:
            return user_id in self.group_admins

        self._answer = answer
        adapter._send_chunks = send_chunks  # type: ignore[method-assign]
        adapter._is_group_admin = group_admin  # type: ignore[method-assign]

    def message(self, user_id: int, chat_id: int = GROUP, chat_type: str = "supergroup"):
        message = _message(user_id, chat_id, chat_type)
        object.__setattr__(message, "_answer", self._answer)
        return message

    @property
    def last(self) -> str:
        return self.replies[-1] if self.replies else ""


@pytest.fixture
async def bridge(storage: Storage):
    adapter = TelegramAdapter(make_settings(), storage, _Directory(), _sink)
    wrapper = _Adapter(adapter)
    try:
        yield wrapper, storage
    finally:
        await adapter.bot.session.close()


def _patch_answer(message: TgMessage, sink: list[str]) -> TgMessage:
    """aiogram-сообщение не умеет отвечать без сети — подменяем ответ."""

    async def answer(text: str, **kwargs: Any) -> None:
        sink.append(text)

    object.__setattr__(message, "answer", answer)
    return message


@pytest.mark.asyncio
async def test_stranger_may_connect_own_account_when_signup_is_open(bridge) -> None:
    """Открытая регистрация: посторонний заводит свой аккаунт и входит сессией."""
    wrapper, _ = bridge
    adapter = wrapper.adapter

    assert adapter._may_signup(STRANGER) is True

    replies: list[str] = []
    message = _patch_answer(
        wrapper.message(STRANGER, chat_id=STRANGER, chat_type="private"), replies
    )
    await adapter._cmd_login(message, CommandObject(command="login", args=None))

    # Отказа быть не должно: вход в свою сессию — не привилегия администратора.
    assert not any("администратор" in text for text in replies), replies


@pytest.mark.asyncio
async def test_closed_signup_refuses_stranger_with_explanation(storage: Storage) -> None:
    """Закрытая регистрация объясняет отказ, а не молчит."""
    adapter = TelegramAdapter(
        make_settings(ALLOW_PUBLIC_SIGNUP=False), storage, _Directory(), _sink
    )
    try:
        assert adapter._may_signup(STRANGER) is False

        replies: list[str] = []
        message = _patch_answer(_message(STRANGER, STRANGER, "private"), replies)
        await adapter._cmd_login(message, CommandObject(command="login", args=None))
        assert replies and "закрыт" in replies[-1].lower()

        replies.clear()
        await adapter._cmd_sync(
            _patch_answer(_message(STRANGER, STRANGER, "private"), replies),
            CommandObject(command="sync", args=None),
        )
        assert replies and "закрыт" in replies[-1].lower()
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_only_group_admin_binds_a_free_group(bridge) -> None:
    """Обычный участник не привяжет группу к своему аккаунту MAX."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    await storage.add_account(owner_id=STRANGER, nickname="Чужой")

    replies: list[str] = []
    await adapter._cmd_bind(
        _patch_answer(_message(STRANGER), replies), CommandObject(command="bind", args=None)
    )
    assert replies and "распоряжается кто-то другой" in replies[-1]

    # Тот же человек, но администратор группы — привязка доступна.
    wrapper.group_admins.add(STRANGER)
    replies.clear()
    await adapter._cmd_bind(
        _patch_answer(_message(STRANGER), replies), CommandObject(command="bind", args="100")
    )
    assert not any("распоряжается кто-то другой" in text for text in replies), replies


@pytest.mark.asyncio
async def test_bound_group_belongs_to_its_max_account_owner(bridge) -> None:
    """Привязанную группу перевешивает только владелец её аккаунта MAX."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    await storage.bind(GROUP, MAX_CHAT, "Чат", account_id=account.id)

    # Посторонний — даже администратор группы — не трогает чужую привязку.
    wrapper.group_admins.update({OWNER, STRANGER})
    replies: list[str] = []
    await adapter._cmd_unbind(_patch_answer(_message(STRANGER), replies))
    assert replies and "распоряжается кто-то другой" in replies[-1]
    assert await storage.get_by_tg(GROUP) is not None

    # Владелец аккаунта — распоряжается.
    replies.clear()
    await adapter._cmd_unbind(_patch_answer(_message(OWNER), replies))
    assert replies and "распоряжается кто-то другой" not in replies[-1]


@pytest.mark.asyncio
async def test_pause_is_not_a_bridge_admin_privilege(bridge) -> None:
    """Паузу ставит хозяин группы, а не только администратор моста."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    await storage.bind(GROUP, MAX_CHAT, "Чат", account_id=account.id)
    wrapper.group_admins.add(OWNER)

    replies: list[str] = []
    await adapter._cmd_pause(_patch_answer(_message(OWNER), replies))
    binding = await storage.get_by_tg(GROUP)
    assert binding is not None and binding.enabled is False
    assert replies and "приостановлена" in replies[-1]

    replies.clear()
    await adapter._cmd_resume(_patch_answer(_message(OWNER), replies))
    binding = await storage.get_by_tg(GROUP)
    assert binding is not None and binding.enabled is True


@pytest.mark.asyncio
async def test_bridge_admin_keeps_access_everywhere(bridge) -> None:
    """Администратор моста разбирает чужие привязки — иначе некому чинить."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    await storage.bind(GROUP, MAX_CHAT, "Чат", account_id=account.id)

    assert await adapter._may_manage_chat(GROUP, "supergroup", BRIDGE_ADMIN) is True
    # И в личке администратор не ограничен группой.
    assert await adapter._may_manage_chat(BRIDGE_ADMIN, "private", BRIDGE_ADMIN) is True


@pytest.mark.asyncio
async def test_accounts_and_chats_are_not_shared_between_people(bridge) -> None:
    """Каждый видит только свои аккаунты MAX и их чаты."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    mine = await storage.add_account(owner_id=OWNER, nickname="Мой")
    await storage.add_account(owner_id=STRANGER, nickname="Чужой")

    assert [item.id for item in await adapter._owner_accounts(OWNER)] == [mine.id]

    wrapper.replies.clear()
    await adapter._send_account_list(GROUP, OWNER)
    assert "Мой" in wrapper.last and "Чужой" not in wrapper.last

    # Администратор моста видит все — это его работа.
    assert len(await adapter._owner_accounts(BRIDGE_ADMIN)) == 2


@pytest.mark.asyncio
async def test_sync_runs_per_person_without_blocking_others(bridge) -> None:
    """Синхронизация одного человека не мешает синхронизации другого."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    await storage.add_account(owner_id=OWNER, nickname="Мой")
    await storage.add_account(owner_id=STRANGER, nickname="Чужой")

    # Очередь у каждого своя: словарь задач, а не один общий слот.
    assert adapter._sync_tasks == {}
    assert adapter._selftest_tasks == {}

    replies: list[str] = []
    await adapter._cmd_sync(
        _patch_answer(_message(OWNER, OWNER, "private"), replies),
        CommandObject(command="sync", args=None),
    )
    assert OWNER in adapter._sync_tasks
    task = adapter._sync_tasks[OWNER]
    task.cancel()

    # Второй человек не упирается в чужую очередь.
    replies.clear()
    await adapter._cmd_sync(
        _patch_answer(_message(STRANGER, STRANGER, "private"), replies),
        CommandObject(command="sync", args=None),
    )
    assert not any("уже идёт" in text for text in replies), replies
    for pending in adapter._sync_tasks.values():
        pending.cancel()


@pytest.mark.asyncio
async def test_same_chat_id_in_two_accounts_never_crosses(storage: Storage) -> None:
    """Чат «Избранное» имеет номер 0 у каждого аккаунта MAX.

    Если маршрут искать только по номеру чата, переписка одного человека
    попадёт в группу другого — поэтому маршрут всегда включает аккаунт.
    """
    from max2tg.bridge import Bridge
    from max2tg.models import Platform

    mine = await storage.add_account(owner_id=OWNER, nickname="Мой")
    theirs = await storage.add_account(owner_id=STRANGER, nickname="Чужой")
    await storage.bind(GROUP, 0, "Избранное", account_id=mine.id)
    await storage.bind(OTHER_GROUP, 0, "Избранное", account_id=theirs.id)

    bridge = Bridge(storage)

    mine_message = NormalizedMessage(
        source=Platform.MAX,
        source_chat_id=0,
        source_message_id="m1",
        account_id=mine.id,
        author="Я",
        text="моё",
    )
    theirs_message = NormalizedMessage(
        source=Platform.MAX,
        source_chat_id=0,
        source_message_id="m2",
        account_id=theirs.id,
        author="Он",
        text="чужое",
    )

    assert await bridge._resolve_route(mine_message) == (Platform.TELEGRAM, GROUP)
    assert await bridge._resolve_route(theirs_message) == (Platform.TELEGRAM, OTHER_GROUP)

    # Обратный путь тоже разводит людей: группа знает свой аккаунт.
    from_group = NormalizedMessage(
        source=Platform.TELEGRAM,
        source_chat_id=OTHER_GROUP,
        source_message_id="42",
        author="Я",
        text="ответ",
    )
    assert await bridge._resolve_route(from_group) == (Platform.MAX, 0)
    assert from_group.account_id == theirs.id


@pytest.mark.asyncio
async def test_account_limit_and_private_only_signup(bridge) -> None:
    """Аккаунтов на человека — не больше настроенного, и только из лички."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    limit = adapter._settings.max_accounts_per_user

    for index in range(limit):
        await storage.add_account(owner_id=STRANGER, nickname=f"Аккаунт {index}")

    replies: list[str] = []
    await adapter._cmd_max_add(
        _patch_answer(_message(STRANGER, STRANGER, "private"), replies),
        CommandObject(command="max_add", args=None),
    )
    assert replies and "предел" in replies[-1].lower()
    assert len(await storage.list_accounts(STRANGER)) == limit

    # В группе подключать аккаунт нельзя: QR-код увидели бы все участники.
    replies.clear()
    await adapter._cmd_max_add(
        _patch_answer(_message(OWNER), replies), CommandObject(command="max_add", args=None)
    )
    assert replies and "в личке" in replies[-1].lower()


class _LoginManager:
    """Менеджер аккаунтов, вход которого можно завершить, сломать или не завершать."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.aborted: list[int] = []
        self.qr_sender: Any = None

    async def start_account(self, account_id: int, token, nickname, qr_callback) -> object:
        self.qr_sender = qr_callback
        if self.behaviour == "ok":
            return type("Session", (), {"nickname": "Егор"})()
        if self.behaviour == "error":
            raise RuntimeError("password is required to login in account with 2FA.")
        await asyncio.Event().wait()  # вход, который так и не заканчивается
        raise AssertionError("недостижимо")

    async def abort_login(self, account_id: int) -> None:
        self.aborted.append(account_id)


def _adapter_with_manager(storage: Storage, manager: _LoginManager, timeout: float):
    adapter = TelegramAdapter(
        make_settings(MAX_LOGIN_TIMEOUT=timeout), storage, _Directory(), _sink
    )
    adapter._accounts = manager  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_login_that_never_finishes_is_aborted(storage: Storage) -> None:
    """QR не отсканировали — вход прерывается, а не повторяется до перезапуска."""
    manager = _LoginManager("hang")
    adapter = _adapter_with_manager(storage, manager, timeout=0.2)
    try:
        outcome = await adapter._connect_with_deadline(5, "Егор", _noop, asyncio.Event())
        assert isinstance(outcome, str) and "не отсканирован" in outcome
        assert manager.aborted == [5]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_login_stops_at_once_when_the_bot_is_blocked(storage: Storage) -> None:
    """Бот заблокирован — QR не доставить, ждать таймаут незачем и писать некому."""
    manager = _LoginManager("hang")
    adapter = _adapter_with_manager(storage, manager, timeout=30.0)
    try:
        blocked = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, blocked.set)
        started = asyncio.get_running_loop().time()
        outcome = await adapter._connect_with_deadline(6, "Егор", _noop, blocked)
        assert outcome == ""
        assert asyncio.get_running_loop().time() - started < 5
        assert manager.aborted == [6]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_login_result_and_error_pass_through(storage: Storage) -> None:
    """Успешный вход и ошибка входа доходят до вызывающего без потерь."""
    ok = _adapter_with_manager(storage, _LoginManager("ok"), timeout=5.0)
    broken = _adapter_with_manager(storage, _LoginManager("error"), timeout=5.0)
    try:
        session = await ok._connect_with_deadline(7, "Егор", _noop, asyncio.Event())
        assert session.nickname == "Егор"
        error = await broken._connect_with_deadline(8, "Егор", _noop, asyncio.Event())
        assert isinstance(error, RuntimeError) and "2FA" in str(error)
    finally:
        await ok.bot.session.close()
        await broken.bot.session.close()


async def _noop(url: str) -> None:
    return None


@pytest.mark.asyncio
async def test_max_password_is_only_for_bridge_admin_accounts(storage: Storage) -> None:
    """Пароль 2FA из .env принадлежит владельцу моста и чужим аккаунтам не даётся."""
    from max2tg.adapters.max_manager import MaxAccountManager

    manager = MaxAccountManager(make_settings(MAX_PASSWORD="пароль-владельца"), storage, _sink)
    mine = await storage.add_account(owner_id=BRIDGE_ADMIN, nickname="Мой")
    theirs = await storage.add_account(owner_id=STRANGER, nickname="Чужой")

    assert await manager._password_for(mine.id) == "пароль-владельца"
    assert await manager._password_for(theirs.id) is None
    assert await manager._password_for(9999) is None


@pytest.mark.asyncio
async def test_unfinished_logins_are_purged_on_start(storage: Storage) -> None:
    """Брошенный /max_add не занимает слот аккаунта после перезапуска."""
    from max2tg.adapters.max_manager import MaxAccountManager

    manager = MaxAccountManager(make_settings(), storage, _sink)
    abandoned = await storage.add_account(owner_id=STRANGER, nickname="Брошенный")
    legacy = await storage.add_account(owner_id=OWNER, nickname="Старый")
    await storage.bind(GROUP, MAX_CHAT, "Чат", account_id=legacy.id)

    await manager.start_all()

    left = {item.id for item in await storage.list_accounts()}
    assert abandoned.id not in left
    # Аккаунт с привязками без токена — не мусор: ему нужен повторный вход.
    assert legacy.id in left


def _button_texts(markup: Any) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callback(user_id: int, data: str, sink: list[str]) -> Any:
    """Нажатие кнопки: ответы и правки перехватываются, сети нет."""
    from aiogram.types import CallbackQuery

    async def answer(text: str | None = None, **kwargs: Any) -> None:
        sink.append(f"answer:{text}")

    callback = CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="Кто-то"),
        chat_instance="x",
        data=data,
        message=_message(user_id, user_id, "private"),
    )
    object.__setattr__(callback, "answer", answer)
    return callback


class _Accounts:
    """Менеджер аккаунтов: помнит, кого остановили и переподключили."""

    def __init__(self, live: set[int], fail_reconnect: bool = False) -> None:
        self.live = live
        self.stopped: list[int] = []
        self.reconnected: list[int] = []
        self.fail_reconnect = fail_reconnect

    def session(self, account_id: int) -> object | None:
        return object() if account_id in self.live else None

    async def stop_account(self, account_id: int) -> None:
        self.stopped.append(account_id)
        self.live.discard(account_id)

    async def reconnect_account(self, account_id: int) -> None:
        if self.fail_reconnect:
            raise RuntimeError("нет сохранённого токена")
        self.reconnected.append(account_id)
        self.live.add(account_id)


async def _swallow_edit(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_account_list_offers_reconnect_only_for_dropped_accounts(bridge) -> None:
    """Кнопка «Переподключить» есть у отвалившегося аккаунта, у живого — нет."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    live = await storage.add_account(owner_id=OWNER, nickname="Живой")
    await storage.add_account(owner_id=OWNER, nickname="Мёртвый")
    adapter._accounts = _Accounts({live.id})  # type: ignore[assignment]

    await adapter._send_account_list(GROUP, OWNER)

    texts = _button_texts(wrapper.markups[-1])
    assert "Переподключить «Мёртвый»" in texts
    assert "Переподключить «Живой»" not in texts
    assert "Удалить «Живой»" in texts and "Удалить «Мёртвый»" in texts
    assert "🟢" in wrapper.last and "🔴" in wrapper.last


@pytest.mark.asyncio
async def test_max_remove_asks_before_wiping_the_account(bridge) -> None:
    """Удаление аккаунта необратимо — сначала подтверждение, и только потом стирание."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    adapter.bot.edit_message_text = _swallow_edit  # type: ignore[method-assign]
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    manager = _Accounts({account.id})
    adapter._accounts = manager  # type: ignore[assignment]

    sent: list[str] = []
    captured: dict[str, Any] = {}

    async def answer(text: str, **kwargs: Any) -> None:
        sent.append(text)
        captured.update(kwargs)

    message = _message(OWNER, OWNER, "private")
    object.__setattr__(message, "answer", answer)
    await adapter._cmd_max_remove(
        message, CommandObject(command="max_remove", args=str(account.id))
    )

    # Одной командой ничего не стёрто: только вопрос с кнопками.
    assert await storage.get_account(account.id) is not None
    assert manager.stopped == []
    assert "Удалить аккаунт" in sent[-1]
    assert f"acc:remove_yes:{account.id}" in [
        button.callback_data for row in captured["reply_markup"].inline_keyboard for button in row
    ]

    # «Отмена» оставляет всё как было.
    sink: list[str] = []
    await adapter._callback_account(_callback(OWNER, "x", sink), "remove_no:0")
    assert await storage.get_account(account.id) is not None

    # «Да» — стирает и останавливает сессию.
    await adapter._callback_account(_callback(OWNER, "x", sink), f"remove_yes:{account.id}")
    assert await storage.get_account(account.id) is None
    assert manager.stopped == [account.id]


@pytest.mark.asyncio
async def test_stranger_cannot_press_other_peoples_account_buttons(bridge) -> None:
    """Кнопки аккаунта работают только у его владельца."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    manager = _Accounts({account.id})
    adapter._accounts = manager  # type: ignore[assignment]

    sink: list[str] = []
    await adapter._callback_account(_callback(STRANGER, "x", sink), f"remove_yes:{account.id}")
    await adapter._callback_account(_callback(STRANGER, "x", sink), f"reconnect:{account.id}")

    assert await storage.get_account(account.id) is not None
    assert manager.stopped == [] and manager.reconnected == []
    assert sink.count("answer:Аккаунт недоступен.") == 2


@pytest.mark.asyncio
async def test_reconnect_restores_a_dropped_account(bridge) -> None:
    """Команда поднимает отвалившийся аккаунт; ошибка объясняется словами."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    manager = _Accounts(set())
    adapter._accounts = manager  # type: ignore[assignment]

    replies: list[str] = []
    await adapter._cmd_max_reconnect(
        _patch_answer(_message(OWNER, OWNER, "private"), replies),
        CommandObject(command="max_reconnect", args=None),
    )
    assert manager.reconnected == [account.id]
    assert "снова на связи" in replies[-1]

    adapter._accounts = _Accounts(set(), fail_reconnect=True)  # type: ignore[assignment]
    replies.clear()
    await adapter._cmd_max_reconnect(
        _patch_answer(_message(OWNER, OWNER, "private"), replies),
        CommandObject(command="max_reconnect", args=str(account.id)),
    )
    assert "Не удалось переподключить" in replies[-1]
    assert "нет сохранённого токена" in replies[-1]


@pytest.mark.asyncio
async def test_owner_is_told_when_the_max_session_drops(bridge) -> None:
    """Потеря связи не остаётся тихой: владелец получает сообщение с кнопкой."""
    wrapper, storage = bridge
    adapter = wrapper.adapter
    account = await storage.add_account(owner_id=OWNER, nickname="Хозяин")
    orphan = await storage.add_account(owner_id=0, nickname="Ничей")

    await adapter.notify_account_lost(account.id)
    assert "отключился" in wrapper.last and "Хозяин" in wrapper.last
    assert _button_texts(wrapper.markups[-1]) == ["Переподключить"]

    # У аккаунта без владельца писать некому — и падать из-за этого нельзя.
    before = len(wrapper.replies)
    await adapter.notify_account_lost(orphan.id)
    await adapter.notify_account_lost(9999)
    assert len(wrapper.replies) == before


class _DyingSession:
    """Сессия MAX, слушать которую больше нечем."""

    def __init__(self, error: bool) -> None:
        self.error = error
        self.stopped = False

    async def run(self) -> None:
        if self.error:
            raise RuntimeError("обрыв")

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_manager_reports_a_session_that_ended_by_itself(storage: Storage) -> None:
    """Сам закончившийся цикл событий — потеря связи; остановка снаружи — нет."""
    from max2tg.adapters.max_manager import MaxAccountManager

    manager = MaxAccountManager(make_settings(), storage, _sink)
    lost: list[int] = []

    async def on_lost(account_id: int) -> None:
        lost.append(account_id)

    manager.set_lost_handler(on_lost)

    for account_id, error in ((1, True), (2, False)):
        session = _DyingSession(error)
        manager._sessions[account_id] = session  # type: ignore[assignment]
        await manager._run(account_id, session)  # type: ignore[arg-type]
        assert account_id in lost
        assert manager.session(account_id) is None
        assert session.stopped is True

    # Сессию убрали намеренно (/max_remove): сообщать не о чем.
    lost.clear()
    await manager._run(3, _DyingSession(False))  # type: ignore[arg-type]
    assert lost == []


@pytest.mark.asyncio
async def test_reconnect_needs_a_saved_token(storage: Storage) -> None:
    """Без сохранённого токена переподключение честно отсылает к /max_add."""
    from max2tg.adapters.max_manager import MaxAccountManager

    manager = MaxAccountManager(make_settings(), storage, _sink)
    account = await storage.add_account(owner_id=OWNER, nickname="Без токена")

    with pytest.raises(RuntimeError, match="/max_add"):
        await manager.reconnect_account(account.id)


class _FakeMapper:
    """Соединение MAX, у которого можно проверить, что его действительно закрыли."""

    def __init__(self) -> None:
        self.closed = False
        self._lifecycle_manager = type("Manager", (), {})()
        self._lifecycle_manager._manage_lifecycle_task = asyncio.ensure_future(asyncio.sleep(60))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_adapter_stop_ends_the_pyromax_reconnect_loop(storage: Storage) -> None:
    """Остановка снимает задачу переподключения pyromax и закрывает соединение.

    Иначе «удалённый» аккаунт продолжал пересылать сообщения: библиотека сама
    поднимала сессию заново после отмены слушателя.
    """
    from max2tg.adapters.max_adapter import MaxAdapter

    adapter = MaxAdapter(make_settings(), storage, _sink, account_id=1)
    mapper = _FakeMapper()
    adapter._api = type("Api", (), {"mapper": mapper})()  # type: ignore[assignment]
    reconnect_loop = mapper._lifecycle_manager._manage_lifecycle_task

    await adapter.stop()
    await asyncio.sleep(0)

    assert mapper.closed is True
    assert reconnect_loop.cancelled() or reconnect_loop.cancelling() > 0


@pytest.mark.asyncio
async def test_adapter_stop_survives_a_broken_connection(storage: Storage) -> None:
    """Сбой при закрытии соединения не должен ронять остановку."""
    from max2tg.adapters.max_adapter import MaxAdapter

    adapter = MaxAdapter(make_settings(), storage, _sink, account_id=1)

    class _BrokenMapper:
        async def close(self) -> None:
            raise RuntimeError("сокет уже закрыт")

    adapter._api = type("Api", (), {"mapper": _BrokenMapper()})()  # type: ignore[assignment]
    await adapter.stop()  # не должно бросить


class _StubbornSession:
    """Сессия, слушатель которой проглатывает отмену — как это делает pyromax."""

    def __init__(self) -> None:
        self.stopped = False
        #: Тест сам отпускает слушатель в конце — иначе он не даст закрыться циклу событий.
        self.release = False

    async def run(self) -> None:
        while not self.release:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_stop_account_does_not_hang_on_a_stubborn_listener(storage: Storage) -> None:
    """Команда /max_remove не зависает, даже если слушатель не реагирует на отмену."""
    from max2tg.adapters.max_manager import MaxAccountManager

    manager = MaxAccountManager(make_settings(), storage, _sink)
    session = _StubbornSession()
    manager._sessions[5] = session  # type: ignore[assignment]
    manager._tasks[5] = asyncio.create_task(manager._run(5, session))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(manager.stop_account(5), timeout=30.0)

    session.release = True
    assert session.stopped is True
    assert manager.session(5) is None
    # Ждали разумно долго, но не вечно.
    assert loop.time() - started < 20
