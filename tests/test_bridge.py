"""Проверка маршрутизации моста и слоя хранения."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from max2tg.bridge import Bridge
from max2tg.db import create_engine, create_session_factory, init_models
from max2tg.models import NormalizedMessage, Platform
from max2tg.storage import Storage

TG_CHAT = -1001234567890
MAX_CHAT = 555001


@dataclass
class RecordingAdapter:
    """Адаптер-заглушка, запоминающий доставленные сообщения."""

    platform: Platform
    delivered: list[tuple[NormalizedMessage, int, str | None]] = field(default_factory=list)
    edited: list[tuple[int, str]] = field(default_factory=list)
    services: list[tuple[int, str]] = field(default_factory=list)
    next_id: int = 1000
    edit_ok: bool = True

    async def deliver(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        reply_to_target_id: str | None,
    ) -> list[str]:
        self.delivered.append((message, target_chat_id, reply_to_target_id))
        self.next_id += 1
        return [str(self.next_id)]

    async def edit(
        self, message: NormalizedMessage, target_chat_id: int, target_message_id: str
    ) -> bool:
        self.edited.append((target_chat_id, target_message_id))
        return self.edit_ok

    async def send_service(self, chat_id: int, text: str) -> None:
        self.services.append((chat_id, text))


@pytest.fixture
async def storage() -> Storage:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    return Storage(create_session_factory(engine))


async def _drain(bridge: Bridge) -> None:
    """Дождаться разбора очередей."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        if all(queue.empty() for queue in bridge._queues.values()):
            break


@pytest.mark.asyncio
async def test_max_message_goes_to_bound_group(storage: Storage) -> None:
    await storage.bind(TG_CHAT, MAX_CHAT, "Тестовый чат")
    telegram = RecordingAdapter(Platform.TELEGRAM)
    bridge = Bridge(storage)
    bridge.register(telegram)

    message = NormalizedMessage(
        source=Platform.MAX,
        source_chat_id=MAX_CHAT,
        source_message_id="42",
        author="Вася",
        text="привет",
    )
    await bridge.submit(message)
    await _drain(bridge)
    await bridge.close()

    assert len(telegram.delivered) == 1
    _, target_chat, reply_to = telegram.delivered[0]
    assert target_chat == TG_CHAT
    assert reply_to is None
    assert await storage.find_tg_message(MAX_CHAT, "42") == 1001


@pytest.mark.asyncio
async def test_unbound_chat_is_ignored(storage: Storage) -> None:
    telegram = RecordingAdapter(Platform.TELEGRAM)
    bridge = Bridge(storage)
    bridge.register(telegram)

    await bridge.submit(
        NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=999,
            source_message_id="1",
            author="Кто-то",
            text="привет",
        )
    )
    await _drain(bridge)
    await bridge.close()
    assert telegram.delivered == []


@pytest.mark.asyncio
async def test_reply_is_mapped_between_platforms(storage: Storage) -> None:
    await storage.bind(TG_CHAT, MAX_CHAT, None)
    await storage.remember(
        tg_chat_id=TG_CHAT,
        tg_message_id=500,
        max_chat_id=MAX_CHAT,
        max_message_id="900",
        direction="max2tg",
    )
    max_adapter = RecordingAdapter(Platform.MAX)
    bridge = Bridge(storage)
    bridge.register(max_adapter)

    await bridge.submit(
        NormalizedMessage(
            source=Platform.TELEGRAM,
            source_chat_id=TG_CHAT,
            source_message_id="501",
            author="Я",
            text="ответ",
            reply_to_source_id="500",
        )
    )
    await _drain(bridge)
    await bridge.close()

    assert max_adapter.delivered[0][1] == MAX_CHAT
    assert max_adapter.delivered[0][2] == "900"
    # Пересылка из Telegram должна помечаться как зеркальная.
    assert await storage.is_mirrored_from_tg(MAX_CHAT, "1001")


@pytest.mark.asyncio
async def test_edit_uses_existing_counterpart(storage: Storage) -> None:
    await storage.bind(TG_CHAT, MAX_CHAT, None)
    await storage.remember(
        tg_chat_id=TG_CHAT,
        tg_message_id=700,
        max_chat_id=MAX_CHAT,
        max_message_id="800",
        direction="max2tg",
    )
    telegram = RecordingAdapter(Platform.TELEGRAM)
    bridge = Bridge(storage)
    bridge.register(telegram)

    await bridge.submit(
        NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=MAX_CHAT,
            source_message_id="800",
            author="Вася",
            text="исправленный текст",
            is_edit=True,
        )
    )
    await _drain(bridge)
    await bridge.close()

    assert telegram.edited == [(TG_CHAT, "700")]
    assert telegram.delivered == []


@pytest.mark.asyncio
async def test_failed_edit_falls_back_to_new_message(storage: Storage) -> None:
    await storage.bind(TG_CHAT, MAX_CHAT, None)
    telegram = RecordingAdapter(Platform.TELEGRAM, edit_ok=False)
    bridge = Bridge(storage)
    bridge.register(telegram)

    await bridge.submit(
        NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=MAX_CHAT,
            source_message_id="801",
            author="Вася",
            text="правка",
            is_edit=True,
        )
    )
    await _drain(bridge)
    await bridge.close()

    assert len(telegram.delivered) == 1
    assert "изменено" in telegram.delivered[0][0].notes[0]


@pytest.mark.asyncio
async def test_binding_is_exclusive(storage: Storage) -> None:
    await storage.bind(TG_CHAT, MAX_CHAT, "первый")
    await storage.bind(-100999, MAX_CHAT, "второй")
    bindings = await storage.list_bindings()
    assert len(bindings) == 1
    assert bindings[0].tg_chat_id == -100999
    assert await storage.unbind(-100999) is True
    assert await storage.unbind(-100999) is False


@pytest.mark.asyncio
async def test_repeated_push_is_not_delivered_twice(storage: Storage) -> None:
    """Повторный push того же сообщения не должен дублировать его в чате."""
    await storage.bind(TG_CHAT, MAX_CHAT, None)
    telegram = RecordingAdapter(Platform.TELEGRAM)
    bridge = Bridge(storage)
    bridge.register(telegram)

    def make() -> NormalizedMessage:
        return NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=MAX_CHAT,
            source_message_id="4242",
            author="Александр",
            text="https://example.com/doc.html",
        )

    await bridge.submit(make())
    await _drain(bridge)
    await bridge.submit(make())
    await _drain(bridge)
    await bridge.close()

    assert len(telegram.delivered) == 1
