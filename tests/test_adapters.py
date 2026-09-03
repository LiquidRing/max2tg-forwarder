"""Проверка нормализации сообщений в адаптерах (без обращения к сети)."""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

import pytest
from aiogram.types import Chat, Document, PhotoSize, User
from aiogram.types import Message as TgMessage
from pyromax.mapping.envelope.v11.payloads.models import (
    ControlMappingModel,
    FileMappingModel,
    PhotoMappingModel,
    ShareMappingModel,
    VideoNoteMappingModel,
    VoiceMappingModel,
)

from max2tg.adapters.max_adapter import MaxAdapter
from max2tg.adapters.telegram_adapter import TelegramAdapter, _split_text
from max2tg.config import Settings
from max2tg.db import create_engine, create_session_factory, init_models
from max2tg.models import AttachmentKind, NormalizedMessage, Platform
from max2tg.storage import Storage

TG_CHAT = -1001234567890
MAX_CHAT = 555001
FAKE_TOKEN = "123456789:AAHfakeTokenForTestsOnly_00000000000"


def make_settings(**overrides: object) -> Settings:
    """Настройки для теста — без оглядки на .env разработчика."""
    values: dict[str, object] = {
        "TG_BOT_TOKEN": FAKE_TOKEN,
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "TG_ADMIN_IDS": [],
        "TG_API_ID": None,
        "TG_API_HASH": None,
        "TG_PHONE": None,
        "MASTER_KEY": None,
        "TG_PROXY": None,
        "TG_USERBOT_PROXY": None,
    }
    values.update({key.upper(): value for key, value in overrides.items()})
    # _env_file=None: иначе настоящий .env подмешался бы в проверки прав.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
async def storage() -> Storage:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine)
    return Storage(create_session_factory(engine))


class DummyDirectory:
    """Заглушка менеджера аккаунтов MAX."""

    async def list_chats(self, account_id: int, query: str | None = None) -> list:
        return []

    async def resolve_chat(self, account_id: int, chat_id: int) -> None:
        return None

    async def import_history(self, account_id: int, chat_id: int, limit: int) -> int:
        return 0

    async def fetch_avatar(self, account_id: int, chat) -> bytes | None:
        return None

    def session(self, account_id: int):
        return object()


async def _account(storage: Storage, nickname: str = "MAX"):
    """Завести аккаунт MAX — привязки и синхронизация теперь идут через него."""
    return await storage.add_account(owner_id=7, nickname=nickname)


async def _collect(bucket: list[NormalizedMessage]):
    async def handler(message: NormalizedMessage) -> None:
        bucket.append(message)

    return handler


def _tg_message(**fields: object) -> TgMessage:
    base: dict[str, object] = {
        "message_id": 10,
        "date": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "chat": Chat(id=TG_CHAT, type="supergroup", title="Группа"),
        "from_user": User(id=7, is_bot=False, first_name="Иван", last_name="Петров"),
    }
    base.update(fields)
    return TgMessage(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_telegram_normalizes_text_and_document(storage: Storage) -> None:
    received: list[NormalizedMessage] = []
    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect(received))
    try:
        message = _tg_message(
            caption="подпись",
            document=Document(
                file_id="AgAD",
                file_unique_id="u1",
                file_name="отчёт.pdf",
                mime_type="application/pdf",
                file_size=2048,
            ),
        )
        normalized = await adapter._normalize(message)
        assert normalized is not None
        assert normalized.source is Platform.TELEGRAM
        assert normalized.author == "Иван Петров"
        assert normalized.text == "подпись"
        assert len(normalized.attachments) == 1
        attachment = normalized.attachments[0]
        assert attachment.kind is AttachmentKind.DOCUMENT
        assert attachment.filename == "отчёт.pdf"
        assert attachment.size == 2048
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_telegram_photo_and_reply(storage: Storage) -> None:
    received: list[NormalizedMessage] = []
    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect(received))
    try:
        message = _tg_message(
            photo=[
                PhotoSize(file_id="small", file_unique_id="s", width=90, height=90, file_size=10),
                PhotoSize(file_id="big", file_unique_id="b", width=900, height=900, file_size=1000),
            ],
            reply_to_message=_tg_message(message_id=5, text="исходное"),
        )
        normalized = await adapter._normalize(message)
        assert normalized is not None
        assert normalized.reply_to_source_id == "5"
        assert normalized.attachments[0].kind is AttachmentKind.PHOTO
        assert normalized.attachments[0].width == 900
    finally:
        await adapter.bot.session.close()


def test_split_text_keeps_all_content() -> None:
    text = "\n".join(f"строка {index}" for index in range(1, 2001))
    chunks = _split_text(text, 4096)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


@pytest.mark.asyncio
async def test_max_converts_attachments(storage: Storage) -> None:
    received: list[NormalizedMessage] = []
    adapter = MaxAdapter(make_settings(), storage, await _collect(received))
    normalized = NormalizedMessage(
        source=Platform.MAX,
        source_chat_id=MAX_CHAT,
        source_message_id="1",
        author="Вася",
    )

    photo = PhotoMappingModel(type="PHOTO", photoToken="tok", photoId=1, baseUrl="https://x/y")
    voice = VoiceMappingModel(type="AUDIO", token="t", audioId=2)
    note = VideoNoteMappingModel(type="VIDEO", token="t", videoId=3, videoType=1)
    document = FileMappingModel(type="FILE", fileId=4, name="файл.zip", size=99)
    share = ShareMappingModel(type="SHARE", shareId=5, url="https://example.com", title="Ссылка")
    control = ControlMappingModel(type="CONTROL", event="USER_ADD")

    for attach in (photo, voice, note, document, share, control, {"_type": "STICKER"}):
        await adapter._convert_attachment(_FakeMaxMessage(), attach, normalized)

    kinds = [item.kind for item in normalized.attachments]
    assert kinds == [
        AttachmentKind.PHOTO,
        AttachmentKind.VOICE,
        AttachmentKind.VIDEO_NOTE,
        AttachmentKind.DOCUMENT,
    ]
    assert normalized.attachments[3].filename == "файл.zip"
    assert any("Ссылка" in note_text for note_text in normalized.notes)
    assert any("USER_ADD" in note_text for note_text in normalized.notes)
    assert any("Стикер" in note_text for note_text in normalized.notes)


class _FakeMaxMessage:
    """Минимальная замена pyromax.models.Message для конвертации вложений."""

    message_id = 1
    chat_id = MAX_CHAT


@pytest.mark.asyncio
async def test_max_handlers_receive_message_argument(storage: Storage) -> None:
    """pyromax подставляет аргументы по аннотациям — проверяем совместимость."""
    from pyromax.models import Message as MaxMessage
    from pyromax.utils.inspect_func_and_form_args import inspect_and_form

    received: list[NormalizedMessage] = []
    adapter = MaxAdapter(make_settings(), storage, await _collect(received))
    sample = MaxMessage(
        message_id=1,
        chat_id=MAX_CHAT,
        time=0,
        type="USER",
        text="привет",
        cid=None,
        attaches=[],
    )
    args = inspect_and_form(adapter._handle_message, {MaxMessage: sample})
    assert args == {"message": sample}


@pytest.mark.asyncio
async def test_status_warns_about_privacy_mode(storage: Storage) -> None:
    """Пока privacy mode включён, /status обязан об этом предупреждать."""
    received: list[NormalizedMessage] = []
    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect(received))
    try:
        assert adapter._privacy_mode_on is False
        await storage.bind(TG_CHAT, MAX_CHAT, "Чат")
        adapter._privacy_mode_on = True

        answers: list[str] = []

        class _Answering:
            chat = Chat(id=TG_CHAT, type="supergroup", title="Группа")

            async def answer(self, text: str) -> None:
                answers.append(text)

        await adapter._cmd_status(_Answering())  # type: ignore[arg-type]
        assert "privacy mode" in answers[0]
    finally:
        await adapter.bot.session.close()


class _FakePool:
    """Пул из одной сессии: тестам достаточно одного владельца."""

    configured = True

    def __init__(self, userbot: object) -> None:
        self._userbot = userbot
        self.forgotten: list[int] = []

    async def get(self, owner_id: int) -> object:
        return self._userbot

    async def authorized(self, owner_id: int) -> object | None:
        return self._userbot

    async def forget(self, owner_id: int) -> None:
        self.forgotten.append(owner_id)


class _FakeUserbot:
    """Заглушка пользовательской сессии Telegram."""

    def __init__(self) -> None:
        self.configured = True
        self.created: list[str] = []
        self.folder: tuple[str, list[int]] | None = None
        self.photos: list[tuple[int, bytes]] = []
        self.photo_error: Exception | None = None
        self.rename_error: Exception | None = None
        self.states: dict[int, object] = {}
        self.renamed: list[tuple[int, str]] = []
        self.next_id = -1001000000000

    async def create_group(self, title: str, bot_username: str, about: str = ""):
        from max2tg.adapters.telegram_userbot import CreatedGroup

        self.created.append(title)
        self.next_id -= 1
        return CreatedGroup(chat_id=self.next_id, title=title)

    async def ensure_folder(self, name: str, chat_ids: list[int]) -> int:
        self.folder = (name, list(chat_ids))
        return 2

    async def set_group_photo(self, chat_id: int, data: bytes) -> None:
        if self.photo_error is not None:
            raise self.photo_error
        self.photos.append((chat_id, data))

    async def group_state(self, chat_id: int):
        from max2tg.adapters.telegram_userbot import GroupState

        return self.states.get(chat_id, GroupState(title="", has_photo=True))

    async def rename_group(self, chat_id: int, title: str) -> None:
        if self.rename_error is not None:
            raise self.rename_error
        self.renamed.append((chat_id, title))


class _ChatsDirectory:
    """Справочник с заданным списком чатов MAX."""

    def __init__(self, chats, avatar: bytes | None = None, history_size: int = 0):
        self._chats = chats
        self._avatar = avatar
        self.history_size = history_size
        self.history_calls: list[tuple[int, int]] = []

    async def fetch_avatar(self, account_id: int, chat):
        return self._avatar if chat.icon_url else None

    async def import_history(self, account_id: int, chat_id: int, limit: int) -> int:
        self.history_calls.append((chat_id, limit))
        return self.history_size

    async def list_chats(self, account_id: int, query: str | None = None):
        return self._chats

    async def resolve_chat(self, account_id: int, chat_id: int):
        return next((chat for chat in self._chats if chat.id == chat_id), None)

    def session(self, account_id: int):
        return object()


def _remote(chat_id: int, title: str, icon_url: str | None = None):
    from max2tg.models import RemoteChat

    return RemoteChat(id=chat_id, title=title, type="CHAT", icon_url=icon_url)


@pytest.mark.asyncio
async def test_sync_creates_missing_groups_and_folder(storage: Storage) -> None:
    """Первый /sync создаёт группы под все чаты MAX и собирает папку."""
    chats = [_remote(1, "Чат один"), _remote(2, "Чат два")]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "MAX (СКАМ)", await _account(storage))

        assert userbot.created == ["Чат один", "Чат два"]
        bindings = {item.max_chat_id: item.tg_chat_id for item in await storage.list_bindings()}
        assert set(bindings) == {1, 2}
        assert userbot.folder is not None
        assert userbot.folder[0] == "MAX (СКАМ)"
        assert sorted(userbot.folder[1]) == sorted(bindings.values())
        assert "Создано групп: 2" in sent[0]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_is_incremental(storage: Storage) -> None:
    """Повторный /sync создаёт только недостающие группы."""
    chats = [_remote(1, "Чат один"), _remote(2, "Чат два"), _remote(3, "Чат три")]
    account = await _account(storage)
    await storage.bind(-1002222222222, 1, "Чат один", account_id=account.id)
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert userbot.created == ["Чат два", "Чат три"]
        assert "Уже были привязаны: 1" in sent[0]
        assert len(await storage.list_bindings()) == 3
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_respects_group_limit(storage: Storage) -> None:
    """Лимит за вызов не превышается, остаток откладывается до следующего /sync."""
    chats = [_remote(index, f"Чат {index}") for index in range(1, 6)]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_group_limit=2),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert len(userbot.created) == 2
        assert "Отложено до следующего /sync: 3" in sent[0]
    finally:
        await adapter.bot.session.close()


def _capture(bucket: list[str]):
    async def send(chat_id: int, text: str, reply_to: int | None = None) -> list[str]:
        bucket.append(text)
        return ["1"]

    return send


@pytest.mark.asyncio
async def test_max_delivery_has_no_author_prefix(storage: Storage) -> None:
    """В MAX уходит чистый текст: аккаунт там один, подпись избыточна."""
    adapter = MaxAdapter(make_settings(), storage, await _collect([]))
    calls: list[dict] = []

    class _Sent:
        message_id = 777

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return _Sent()

    async def fake_upload(message, chat_id):
        return []

    adapter._send = fake_send
    adapter._upload = fake_upload

    message = NormalizedMessage(
        source=Platform.TELEGRAM,
        source_chat_id=TG_CHAT,
        source_message_id="9",
        author="Иван Петров",
        text="привет",
    )
    sent = await adapter.deliver(message, MAX_CHAT, None)

    assert sent == ["777"]
    assert calls[0]["text"] == "привет"
    assert calls[0]["offset"] == 0
    assert "Иван" not in calls[0]["text"]


class _LoginUserbot:
    """Заглушка сессии для проверки шагов входа."""

    def __init__(self, needs_password: bool = False) -> None:
        self.configured = True
        self.authorized = False
        self.codes: list[str] = []
        self.passwords: list[str] = []
        self.phones: list[str] = []
        self._needs_password = needs_password

    async def is_authorized(self) -> bool:
        return self.authorized

    async def request_code(self, phone: str) -> None:
        self.phones.append(phone)

    async def submit_code(self, code: str) -> str:
        self.codes.append(code)
        if self._needs_password:
            return "password"
        self.authorized = True
        return "done"

    async def submit_password(self, password: str) -> None:
        self.passwords.append(password)
        self.authorized = True

    async def whoami(self) -> str:
        return "Владелец моста"


class _PrivateMessage:
    """Минимальное приватное сообщение с записью ответов."""

    def __init__(self, text: str, answers: list[str]) -> None:
        self.text = text
        self.message_id = 55
        self.chat = Chat(id=7, type="private")
        self.from_user = User(id=7, is_bot=False, first_name="Владелец")
        self._answers = answers

    async def answer(self, text: str) -> None:
        self._answers.append(text)


@pytest.mark.asyncio
async def test_login_flow_strips_separators_from_code(storage: Storage) -> None:
    """Код, присланный через дефисы, доходит до Telethon чистыми цифрами."""
    userbot = _LoginUserbot()
    adapter = TelegramAdapter(
        make_settings(tg_phone="+79990000000"),
        storage,
        DummyDirectory(),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        answers: list[str] = []
        await adapter._cmd_login(_PrivateMessage("/login", answers), _FakeCommand(""))
        assert userbot.phones == ["+79990000000"]
        assert "1-2-3-4-5" in answers[0]

        adapter.bot.delete_message = _fail_delete
        await adapter._handle_private_message(_PrivateMessage("1-2-3-4-5", answers))
        assert userbot.codes == ["12345"]
        assert "авторизована" in answers[-1]
        assert adapter._login_flows == {}
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_login_flow_asks_for_two_factor_password(storage: Storage) -> None:
    """При включённой двухфакторной защите бот просит пароль вторым шагом."""
    userbot = _LoginUserbot(needs_password=True)
    adapter = TelegramAdapter(
        make_settings(tg_phone="+79990000000"),
        storage,
        DummyDirectory(),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        answers: list[str] = []
        adapter.bot.delete_message = _fail_delete
        await adapter._cmd_login(_PrivateMessage("/login", answers), _FakeCommand(""))
        await adapter._handle_private_message(_PrivateMessage("11111", answers))
        assert "пароль" in answers[-1].lower()
        assert adapter._login_flows == {7: "password"}

        await adapter._handle_private_message(_PrivateMessage("секрет", answers))
        assert userbot.passwords == ["секрет"]
        assert "авторизована" in answers[-1]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_login_is_refused_in_group(storage: Storage) -> None:
    """В группе вход недоступен: код нельзя показывать участникам."""
    userbot = _LoginUserbot()
    adapter = TelegramAdapter(
        make_settings(),
        storage,
        DummyDirectory(),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        answers: list[str] = []
        message = _PrivateMessage("/login", answers)
        message.chat = Chat(id=TG_CHAT, type="supergroup", title="Группа")
        await adapter._cmd_login(message, _FakeCommand(""))
        assert "личке" in answers[0]
        assert userbot.phones == []
    finally:
        await adapter.bot.session.close()


class _FakeCommand:
    def __init__(self, args: str) -> None:
        self.args = args


async def _fail_delete(*args: object, **kwargs: object) -> bool:
    raise RuntimeError("удаление недоступно в тесте")


@pytest.mark.asyncio
async def test_sync_transfers_chat_avatars(storage: Storage) -> None:
    """Картинка чата MAX становится аватаром созданной группы."""
    chats = [_remote(1, "С картинкой", "https://max/icon.jpg"), _remote(2, "Без картинки")]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats, avatar=b"picture-bytes"),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert len(userbot.photos) == 1
        assert userbot.photos[0][1] == b"picture-bytes"
        assert "Обновлено аватаров: 1" in sent[0]
        bindings = {item.max_chat_id: item.icon_url for item in await storage.list_bindings()}
        assert bindings[1] == "https://max/icon.jpg"
        assert bindings[2] is None
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_updates_avatar_when_it_changed(storage: Storage) -> None:
    """У существующей привязки аватар обновляется только при смене картинки."""
    account = await _account(storage)
    await storage.bind(
        -1002222222222, 1, "Чат", icon_url="https://max/old.jpg", account_id=account.id
    )
    await storage.bind(
        -1003333333333, 2, "Другой", icon_url="https://max/same.jpg", account_id=account.id
    )
    chats = [_remote(1, "Чат", "https://max/new.jpg"), _remote(2, "Другой", "https://max/same.jpg")]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats, avatar=b"new-picture"),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert [chat_id for chat_id, _ in userbot.photos] == [-1002222222222]
        assert userbot.created == []
        bindings = {item.max_chat_id: item.icon_url for item in await storage.list_bindings()}
        assert bindings[1] == "https://max/new.jpg"
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_covers_favorites_chat(storage: Storage) -> None:
    """Чат с идентификатором 0 — это «Избранное», его тоже нужно связать."""
    chats = [_remote(0, "Избранное"), _remote(5, "Нормальный чат")]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        adapter._send_chunks = _capture([])
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert userbot.created == ["Избранное", "Нормальный чат"]
        assert sorted(item.max_chat_id for item in await storage.list_bindings()) == [0, 5]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_defers_avatars_on_flood_wait(storage: Storage) -> None:
    """Долгий flood wait останавливает перенос аватаров, но не синхронизацию."""
    from telethon.errors import FloodWaitError

    chats = [_remote(index, f"Чат {index}", f"https://max/{index}.jpg") for index in (1, 2, 3)]
    userbot = _FakeUserbot()
    userbot.photo_error = FloodWaitError(request=None, capture=600)
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats, avatar=b"picture"),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        # Группы созданы все, аватары отложены после первой же отбивки.
        assert len(userbot.created) == 3
        assert userbot.photos == []
        assert "ограничение частоты" in sent[0]
        assert all(item.icon_url is None for item in await storage.list_bindings())
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_unavailable_voice_becomes_readable_note(storage: Storage) -> None:
    """Голосовое, которое MAX не отдаёт, превращается в понятную строку."""
    from max2tg.models import AttachmentKind, NormalizedAttachment

    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect([]))
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)

        async def failing_loader() -> bytes:
            raise ValueError("MAX не отдал содержимое вложения")

        attachment = NormalizedAttachment(
            kind=AttachmentKind.VOICE,
            filename="voice_1.ogg",
            loader=failing_loader,
            note="🎤 Голосовое сообщение — MAX не отдаёт его файлом, послушайте в приложении",
        )
        result = await adapter._send_single(TG_CHAT, attachment, "", None)

        assert result is None
        assert "Голосовое сообщение" in sent[0]
        assert "не отдал содержимое" not in sent[0]
    finally:
        await adapter.bot.session.close()


def test_find_attach_url_picks_matching_attachment() -> None:
    """Ссылка берётся у того вложения, чей идентификатор совпал."""
    from max2tg.adapters.max_adapter import _find_attach_url

    payload = {
        "messages": [
            {
                "id": "111",
                "attaches": [
                    {"_type": "AUDIO", "audioId": 999, "url": "https://a.oneme.ru/audio?cid=other"}
                ],
            },
            {
                "id": "222",
                "attaches": [
                    {"_type": "AUDIO", "audioId": 42, "url": "https://a.oneme.ru/audio?cid=target"}
                ],
            },
        ]
    }
    assert _find_attach_url(payload, 42, "222").endswith("cid=target")


def test_find_attach_url_returns_none_without_links() -> None:
    from max2tg.adapters.max_adapter import _find_attach_url

    assert _find_attach_url({"messages": [{"attaches": [{"audioId": 1}]}]}, 1, "5") is None


@pytest.mark.asyncio
async def test_media_history_used_only_for_voice(storage: Storage) -> None:
    """Кружки качаются прежним путём: медиа-история отдаёт для них не тот файл."""
    from pyromax.mapping.envelope.v11.payloads.models import (
        VideoNoteMappingModel,
        VoiceMappingModel,
    )

    adapter = MaxAdapter(make_settings(), storage, await _collect([]))
    history_calls: list[object] = []

    async def fake_history(attach: object, attach_type: str) -> str | None:
        history_calls.append(attach)
        return None

    async def fake_url(opcode: int, payload: dict) -> str | None:
        return None

    adapter._media_history_url = fake_history
    adapter._request_file_url = fake_url

    note = VideoNoteMappingModel(
        type="VIDEO", token="t", videoId=1, videoType=1, chatId=5, messageId="9"
    )
    await adapter._download_by_link(note)
    assert history_calls == []

    voice = VoiceMappingModel(type="AUDIO", token="t", audioId=2, chatId=5, messageId="9")
    await adapter._download_by_link(voice)
    assert history_calls == [voice]


def test_share_preview_does_not_repeat_link_from_text() -> None:
    """Превью ссылки не дублирует адрес, который уже есть в тексте."""
    from max2tg.adapters.max_adapter import _describe_share

    text = "Смотри https://lk.sfr.gov.ru/mchd.html,"
    note = _describe_share("https://lk.sfr.gov.ru/mchd.html", "СФР. МЧД", "18.06.2026", text)
    assert note == "🔗 СФР. МЧД\n18.06.2026"

    assert _describe_share("https://other.example", None, None, text) == "🔗 https://other.example"
    assert _describe_share("https://lk.sfr.gov.ru/mchd.html", None, None, text) == ""


def test_unknown_share_attachment_is_rendered_as_preview() -> None:
    """SHARE, не разобранный библиотекой, всё равно показывается по-человечески."""
    from max2tg.adapters.max_adapter import _describe_unknown

    attach = {
        "_type": "SHARE",
        "url": "https://lk.sfr.gov.ru/mchd.html",
        "title": "СФР. МЧД",
        "description": "переход на новые требования",
    }
    note = _describe_unknown(attach, "текст со ссылкой https://lk.sfr.gov.ru/mchd.html")
    assert "не поддерживается" not in note
    assert "СФР. МЧД" in note


@pytest.mark.asyncio
async def test_reply_id_comes_from_nested_message(storage: Storage) -> None:
    """pyromax теряет message_id в ссылке ответа — берём его из вложенного сообщения."""
    from pyromax.models import Message as MaxMessage
    from pyromax.models.Message import MessageLink

    adapter = MaxAdapter(make_settings(), storage, await _collect([]))
    adapter._names[7] = "Автор"

    original = MaxMessage(
        message_id=1001,
        chat_id=MAX_CHAT,
        time=0,
        type="USER",
        text="исходное сообщение",
        cid=None,
        attaches=[],
        sender_id=7,
    )
    reply = MaxMessage(
        message_id=1002,
        chat_id=MAX_CHAT,
        time=0,
        type="USER",
        text="ответ",
        cid=None,
        attaches=[],
        sender_id=7,
        link=MessageLink(type="REPLY", message=original),
    )

    normalized = await adapter._normalize(reply, is_edit=False, forwarded=False)

    assert normalized is not None
    assert normalized.reply_to_source_id == "1001"
    assert normalized.reply_preview == "Автор: исходное сообщение"


@pytest.mark.asyncio
async def test_quote_shown_when_original_is_missing(storage: Storage) -> None:
    """Без известного оригинала ответ приходит с текстовой цитатой."""
    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect([]))
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        message = NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=MAX_CHAT,
            source_message_id="2",
            author="Вася",
            text="ответ",
            reply_preview="Автор: исходное сообщение",
        )
        await adapter.deliver(message, TG_CHAT, None)

        assert "<blockquote>Автор: исходное сообщение</blockquote>" in sent[0]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_author_header_format(storage: Storage) -> None:
    """Отправитель из MAX подписывается как «👤 Имя:»."""
    adapter = TelegramAdapter(make_settings(), storage, DummyDirectory(), await _collect([]))
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        message = NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=MAX_CHAT,
            source_message_id="1",
            author="Иван Петров",
            text="привет",
        )
        await adapter.deliver(message, TG_CHAT, None)

        assert sent[0].startswith("👤 <b>Иван Петров</b>:")
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_repairs_title_and_missing_avatar(storage: Storage) -> None:
    """Синхронизация чинит расхождения: устаревшее имя и удалённый аватар."""
    from max2tg.adapters.telegram_userbot import GroupState

    account = await _account(storage)
    await storage.bind(
        -1002222222222, 1, "Старое имя", icon_url="https://max/icon.jpg", account_id=account.id
    )
    chats = [_remote(1, "Новое имя", "https://max/icon.jpg")]
    userbot = _FakeUserbot()
    # В самой группе имя устарело, а картинку удалили руками.
    userbot.states[-1002222222222] = GroupState(title="Старое имя", has_photo=False)

    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats, avatar=b"picture"),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert userbot.renamed == [(-1002222222222, "Новое имя")]
        assert [chat_id for chat_id, _ in userbot.photos] == [-1002222222222]
        assert "Переименовано групп: 1" in sent[0]
        assert "Обновлено аватаров: 1" in sent[0]
        binding = await storage.get_by_max(1, account.id)
        assert binding is not None and binding.title == "Новое имя"
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_reports_unavailable_group(storage: Storage) -> None:
    """Пропавшая группа попадает в отчёт, а не молча считается исправной."""
    account = await _account(storage)
    await storage.bind(-1002222222222, 1, "Чат", account_id=account.id)
    chats = [_remote(1, "Чат", "https://max/icon.jpg")]
    userbot = _FakeUserbot()
    userbot.states[-1002222222222] = None  # type: ignore[assignment]

    async def missing(chat_id: int):
        return None

    userbot.group_state = missing  # type: ignore[method-assign]
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0),
        storage,
        _ChatsDirectory(chats, avatar=b"picture"),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert "недоступна" in sent[0]
        assert userbot.photos == []
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_favorites_chat_uses_own_avatar(storage: Storage) -> None:
    """У «Избранного» нет своей картинки — берём аватар владельца аккаунта."""

    class _FakeApi:
        id = 777

    class _FakeChat:
        id = 0
        type = "DIALOG"
        title = None
        participants: ClassVar[dict[int, int]] = {777: 1}
        participants_count = 1
        last_message = None
        base_icon_url = None
        base_raw_icon_url = None

    adapter = MaxAdapter(make_settings(), storage, await _collect([]))
    adapter._api = _FakeApi()  # type: ignore[assignment]
    asked: list[int] = []

    async def fake_avatar(user_id: int) -> str:
        asked.append(user_id)
        return "https://max/own-avatar.jpg"

    adapter._resolve_avatar = fake_avatar
    adapter._names[777] = "Владелец"

    remote = await adapter._to_remote(_FakeChat())

    assert asked == [777]
    assert remote.icon_url == "https://max/own-avatar.jpg"
    assert remote.title == "Избранное"


@pytest.mark.asyncio
async def test_sync_imports_history_for_every_bound_chat(storage: Storage) -> None:
    """История подтягивается и в новые группы, и в давно привязанные."""
    chats = [_remote(1, "Новый чат")]
    userbot = _FakeUserbot()
    directory = _ChatsDirectory(chats, history_size=42)
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0, history_import_limit=100),
        storage,
        directory,
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert directory.history_calls == [(1, 100)]
        assert "Перенесено сообщений из истории: 42" in sent[0]

        # Повторный вызов на уже существующей привязке тоже тянет историю:
        # лишнего в чат не попадёт, повторы отсекает таблица соответствий.
        directory.history_calls.clear()
        await adapter._run_sync(TG_CHAT, "Папка", account)
        assert directory.history_calls == [(1, 100)]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_sync_skips_history_when_disabled(storage: Storage) -> None:
    """Нулевой лимит полностью отключает перенос истории."""
    chats = [_remote(1, "Новый чат")]
    userbot = _FakeUserbot()
    directory = _ChatsDirectory(chats, history_size=10)
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0, history_import_limit=0),
        storage,
        directory,
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        adapter._send_chunks = _capture([])
        account = await _account(storage)
        await adapter._run_sync(TG_CHAT, "Папка", account)

        assert directory.history_calls == []
    finally:
        await adapter.bot.session.close()


async def test_userbot_pool_keeps_one_session_per_owner(storage: Storage) -> None:
    """У каждого пользователя своя сессия, и она переживает выгрузку из памяти."""
    from max2tg.adapters.userbot_pool import UserbotPool

    pool = UserbotPool(make_settings(TG_API_ID=1, TG_API_HASH="hash"), storage)

    first = await pool.get(111)
    second = await pool.get(222)
    assert first is not second
    assert await pool.get(111) is first

    # Сохранение идёт через хранилище — новый пул поднимет ту же строку.
    await storage.save_tg_session(111, "session-of-111")
    fresh = UserbotPool(make_settings(TG_API_ID=1, TG_API_HASH="hash"), storage)
    restored = await fresh.get(111)
    assert restored._session_string == "session-of-111"
    assert (await fresh.get(222))._session_string is None

    await pool.forget(111)
    assert await storage.get_tg_session(111) is None


async def test_userbot_pool_requires_api_credentials(storage: Storage) -> None:
    """Без api_id и api_hash вход невозможен — пул честно об этом сообщает."""
    from max2tg.adapters.userbot_pool import UserbotPool

    pool = UserbotPool(make_settings(TG_API_ID=None, TG_API_HASH=None), storage)
    assert pool.configured is False
    assert await pool.authorized(111) is None


@pytest.mark.asyncio
async def test_sync_defers_renames_on_flood_wait(storage: Storage) -> None:
    """Массовое переименование упирается в лимит — остальные ждут следующего /sync."""
    from telethon.errors import FloodWaitError

    from max2tg.adapters.telegram_userbot import GroupState

    chats = [_remote(index, f"Чат {index}") for index in (1, 2, 3)]
    userbot = _FakeUserbot()
    userbot.rename_error = FloodWaitError(request=None, capture=780)
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0, history_import_limit=0),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        account = await _account(storage)
        for index in (1, 2, 3):
            tg_chat_id = -100_000 - index
            await storage.bind(tg_chat_id, index, "Старое имя", account_id=account.id)
            userbot.states[tg_chat_id] = GroupState(title="Старое имя", has_photo=True)

        await adapter._run_sync(TG_CHAT, "Папка", account)

        # Первая попытка съедает отбивку, дальше мост не долбится в лимит.
        assert userbot.renamed == []
        assert "Переименование отложено для 3 групп" in sent[0]
        assert "переименование не удалось" not in sent[0]
    finally:
        await adapter.bot.session.close()


@pytest.mark.asyncio
async def test_folder_is_named_per_account(storage: Storage) -> None:
    """Второй аккаунт получает свою папку, а группы остаются без префиксов."""
    from max2tg.adapters.telegram_adapter import _folder_title

    assert _folder_title("MAX (СКАМ)", "Qq", multi_account=False) == "MAX (СКАМ)"
    assert _folder_title("MAX (СКАМ)", "Qq", multi_account=True) == "Qq"
    # Название папки Telegram обрезает — делаем это сами и предсказуемо.
    assert _folder_title("MAX (СКАМ)", "Очень длинный ник", multi_account=True) == "Очень длинны"

    chats = [_remote(1, "Чат один")]
    userbot = _FakeUserbot()
    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, sync_avatar_delay=0, history_import_limit=0),
        storage,
        _ChatsDirectory(chats),
        await _collect([]),
        _FakePool(userbot),  # type: ignore[arg-type]
    )
    try:
        adapter._send_chunks = _capture([])
        second = await storage.add_account(owner_id=7, nickname="Второй")
        await adapter._run_sync(TG_CHAT, "MAX (СКАМ)", second, multi_account=True)

        assert userbot.created == ["Чат один"]
        assert userbot.folder is not None
        assert userbot.folder[0] == "Второй"
    finally:
        await adapter.bot.session.close()


def test_permanent_max_errors_are_not_retried() -> None:
    """Отказ MAX по вложению не лечится повтором — незачем держать очередь."""
    from max2tg.adapters.max_adapter import _is_permanent

    assert _is_permanent(RuntimeError("errors.process.attachment.video.not.supported"))
    assert _is_permanent(RuntimeError("video.not.supported"))
    # Обрыв соединения, наоборот, лечится переподключением.
    assert not _is_permanent(ConnectionResetError("websocket closed"))
    assert not _is_permanent(TimeoutError())


def test_round_video_keeps_its_shape_in_max() -> None:
    """Кружок уходит своей ячейкой и с длительностью — иначе он не круглый."""
    from pyromax.models import FileAttachment, VideoNoteAttachment

    from max2tg.adapters.max_adapter import (
        DEFAULT_ROUND_DURATION_MS,
        DEGRADED_NOTES,
        UPLOAD_TYPES,
        _as_round,
    )

    assert UPLOAD_TYPES[AttachmentKind.VIDEO_NOTE] is VideoNoteAttachment
    # Голосовое MAX от сторонних клиентов не принимает — едет файлом с пометкой.
    assert UPLOAD_TYPES[AttachmentKind.VOICE] is FileAttachment
    assert AttachmentKind.VOICE in DEGRADED_NOTES
    assert AttachmentKind.VIDEO_NOTE not in DEGRADED_NOTES

    class _Uploaded:
        video_id = 42
        token = "t"

    payload = _as_round(_Uploaded(), 7).to_payload[0]
    assert payload == {"_type": "VIDEO", "videoId": 42, "token": "t", "duration": 7000}
    # Без длительности сервер отвергает вложение, поэтому она всегда есть.
    assert _as_round(_Uploaded(), None).to_payload[0]["duration"] == DEFAULT_ROUND_DURATION_MS


def test_link_request_needs_message_address() -> None:
    """Без chatId и messageId запрос не отправляется: MAX рвёт соединение."""
    from pyromax.mapping.envelope.v11.payloads.models import FileMappingModel

    from max2tg.adapters.max_adapter import MaxAdapter, _restore_context

    orphan = FileMappingModel(type="FILE", file_id=42, token="t", name="voice.m4a")
    assert MaxAdapter._link_requests(None, orphan) == []  # type: ignore[arg-type]

    # История чата отдаёт вложения без адреса — берём его у самого сообщения.
    class _Message:
        chat_id = 555
        message_id = "777"

    _restore_context(orphan, _Message())  # type: ignore[arg-type]
    requests = MaxAdapter._link_requests(None, orphan)  # type: ignore[arg-type]
    assert requests and requests[0][1]["chatId"] == 555
    assert requests[0][1]["messageId"] == "777"


@pytest.mark.asyncio
async def test_sync_without_user_session_explains_manual_path(storage: Storage) -> None:
    """Без сессии Telegram мост не молчит, а объясняет ручной путь."""

    class _EmptyPool:
        configured = True

        async def authorized(self, owner_id: int) -> None:
            return None

    adapter = TelegramAdapter(
        make_settings(sync_create_delay=0, history_import_limit=0),
        storage,
        _ChatsDirectory([_remote(1, "Чат")]),
        await _collect([]),
        _EmptyPool(),  # type: ignore[arg-type]
    )
    try:
        sent: list[str] = []
        adapter._send_chunks = _capture(sent)
        await adapter._run_sync(TG_CHAT, "Папка", await _account(storage))

        assert sent, "пользователь должен получить объяснение, а не тишину"
        assert "/login" in sent[0] and "/bind" in sent[0]
    finally:
        await adapter.bot.session.close()


async def test_incoming_counter_is_scoped_to_account(storage: Storage) -> None:
    """Счётчик входящих смотрит на нужный аккаунт, а не на нулевой."""
    import time as _time

    now = int(_time.time())
    await storage.remember(
        max_chat_id=0,
        max_message_id="m1",
        tg_chat_id=TG_CHAT,
        tg_message_id=1,
        direction="max2tg",
        account_id=1,
    )

    assert await storage.count_incoming_since(0, now - 60, 1) == 1
    # Тот же чат у другого аккаунта — чужая переписка, её считать нельзя.
    assert await storage.count_incoming_since(0, now - 60, 2) == 0


async def test_pause_stops_forwarding_but_keeps_binding(storage: Storage) -> None:
    """Пауза выключает пересылку, не теряя привязку и историю соответствий."""
    account = await _account(storage)
    await storage.bind(TG_CHAT, MAX_CHAT, "Чат", account_id=account.id)

    assert await storage.set_enabled(TG_CHAT, False) is True
    binding = await storage.get_by_tg(TG_CHAT)
    assert binding is not None and binding.enabled is False
    # Привязка на месте — возобновление ничего не восстанавливает заново.
    assert await storage.get_by_max(MAX_CHAT, account.id) is not None

    assert await storage.set_enabled(TG_CHAT, True) is True
    resumed = await storage.get_by_tg(TG_CHAT)
    assert resumed is not None and resumed.enabled is True

    # Несуществующая привязка не притворяется успехом.
    assert await storage.set_enabled(-999, False) is False


def test_command_menus_cover_documented_commands() -> None:
    """Меню Telegram не должно расходиться со справкой."""
    from max2tg.adapters.telegram_adapter import (
        GROUP_COMMANDS,
        HELP_TEXT,
        PRIVATE_COMMANDS,
    )

    for name, description in [*PRIVATE_COMMANDS, *GROUP_COMMANDS]:
        assert description and description[0].islower(), name
        assert len(description) <= 60, name
        assert f"/{name}" in HELP_TEXT or name == "start", name


def test_button_title_is_trimmed_predictably() -> None:
    """Подпись кнопки обрезается сами, а не молча Telegram'ом."""
    from max2tg.adapters.telegram_adapter import _button_title

    assert _button_title("Короткий") == "Короткий"
    assert _button_title("") == "Чат MAX"
    long = _button_title("я" * 80)
    assert len(long) == 40 and long.endswith("…")


async def test_long_message_keeps_every_part_mapped(storage: Storage) -> None:
    """Длинное сообщение Telegram уходит в MAX частями — помнить нужно все.

    Раньше запись затирала предыдущую, и «потерянные» части возвращались из
    MAX эхом при следующем переносе истории.
    """
    for part in ("max-1", "max-2", "max-3"):
        await storage.remember(
            tg_chat_id=TG_CHAT,
            tg_message_id=555,
            max_chat_id=MAX_CHAT,
            max_message_id=part,
            direction="tg2max",
            account_id=1,
        )

    for part in ("max-1", "max-2", "max-3"):
        assert await storage.find_tg_message(MAX_CHAT, part, 1) == 555

    # Повтор той же пары не плодит дубликаты.
    await storage.remember(
        tg_chat_id=TG_CHAT,
        tg_message_id=555,
        max_chat_id=MAX_CHAT,
        max_message_id="max-2",
        direction="tg2max",
        account_id=1,
    )
    assert await storage.count_messages(TG_CHAT) == 3


@pytest.mark.asyncio
async def test_stranger_manages_only_their_own_chats(storage: Storage) -> None:
    """Чужую группу посторонний не перевесит на свой аккаунт MAX."""
    from aiogram.enums import ChatType

    # Пустой TG_ADMIN_IDS означает «командуют все» — здесь нужен настоящий мост
    # с назначенным администратором.
    adapter = TelegramAdapter(
        make_settings(TG_ADMIN_IDS=[1]),
        storage,
        DummyDirectory(),
        await _collect([]),
    )
    try:
        owner, stranger = 7, 99
        account = await storage.add_account(owner_id=owner, nickname="Хозяин")
        await storage.bind(TG_CHAT, MAX_CHAT, "Чат", account_id=account.id)

        admins: set[int] = {owner, stranger}

        async def group_admin(chat_id: int, user_id: int | None) -> bool:
            return user_id in admins

        adapter._is_group_admin = group_admin  # type: ignore[method-assign]

        # Владелец аккаунта распоряжается своей группой.
        assert await adapter._may_manage_chat(TG_CHAT, ChatType.SUPERGROUP, owner) is True
        # Администратор группы, но чужой аккаунт MAX — доступа нет.
        assert await adapter._may_manage_chat(TG_CHAT, ChatType.SUPERGROUP, stranger) is False
        # Свободную группу может привязать её администратор.
        assert await adapter._may_manage_chat(-4242, ChatType.SUPERGROUP, stranger) is True
        # Не администратор группы — мимо.
        admins.discard(stranger)
        assert await adapter._may_manage_chat(-4242, ChatType.SUPERGROUP, stranger) is False
    finally:
        await adapter.bot.session.close()
