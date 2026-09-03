"""Нормализованное представление сообщения, общее для обеих платформ."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Platform(StrEnum):
    """Платформа-источник или получатель."""

    TELEGRAM = "telegram"
    MAX = "max"

    @property
    def other(self) -> Platform:
        """Противоположная платформа."""
        return Platform.MAX if self is Platform.TELEGRAM else Platform.TELEGRAM


class AttachmentKind(StrEnum):
    """Вид вложения в нормализованном виде."""

    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"
    AUDIO = "audio"
    ANIMATION = "animation"
    STICKER = "sticker"
    DOCUMENT = "document"


#: Загрузчик содержимого вложения. Вызывается адаптером-получателем — то есть
#: скачивание происходит лениво и только если вложение реально будет отправлено.
Loader = Callable[[], Awaitable[bytes]]


@dataclass(slots=True)
class NormalizedAttachment:
    """Вложение, независимое от платформы."""

    kind: AttachmentKind
    filename: str
    loader: Loader
    mime_type: str | None = None
    size: int | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    #: Запасной источник: например, статичное превью анимированного стикера.
    alt_loader: Loader | None = None
    #: Текстовое описание, если содержимое не удалось перенести как файл.
    note: str | None = None


@dataclass(slots=True)
class TextSpan:
    """Диапазон форматирования в тексте (смещения — в кодовых единицах UTF-16)."""

    type: str
    offset: int
    length: int
    url: str | None = None


@dataclass(slots=True)
class NormalizedMessage:
    """Сообщение, приведённое к общему виду."""

    source: Platform
    source_chat_id: int
    source_message_id: str
    author: str
    #: Аккаунт MAX, которому принадлежит переписка. Идентификаторы чатов
    #: уникальны только внутри аккаунта, поэтому маршрут без него неоднозначен.
    account_id: int = 0
    text: str = ""
    spans: list[TextSpan] = field(default_factory=list)
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    #: id сообщения в чате-источнике, на которое отвечает это сообщение.
    reply_to_source_id: str | None = None
    #: Краткая цитата оригинала: показывается, если сам оригинал
    #: в чате-получателе отсутствует и сослаться не на что.
    reply_preview: str | None = None
    #: Правка ранее отправленного сообщения.
    is_edit: bool = False
    #: Служебные пометки (что не удалось перенести и почему).
    notes: list[str] = field(default_factory=list)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def is_empty(self) -> bool:
        """Нечего пересылать."""
        return not self.text and not self.attachments and not self.notes


@dataclass(slots=True)
class RemoteChat:
    """Краткие сведения о чате на удалённой платформе (для команд управления)."""

    id: int
    title: str
    type: str
    participants_count: int | None = None
    last_message: str | None = None
    #: Адрес картинки чата: переносится в аватар группы Telegram.
    icon_url: str | None = None
