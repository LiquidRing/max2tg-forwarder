"""Кто чем вправе распоряжаться в общем мосте.

Мост обслуживает много людей сразу, поэтому проверки прав живут отдельным
файлом: одна ошибка здесь означает, что посторонний перевесит чужую группу на
свой аккаунт MAX и начнёт читать её переписку.
"""

from __future__ import annotations

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
        self.group_admins: set[int] = set()

        async def answer(text: str, **kwargs: Any) -> None:
            self.replies.append(text)

        async def send_chunks(chat_id: int, text: str, reply_to: int | None = None) -> None:
            self.replies.append(text)

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
