"""Матрица проверок моста: что именно гоняется командой ``/selftest full``.

Сценарии описаны данными, а не кодом, чтобы отчёт и набор проверок нельзя было
незаметно рассинхронизировать.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Scenario:
    """Один проверяемый случай пересылки Telegram → MAX."""

    key: str
    title: str
    #: Как отправить сообщение: возвращает идентификаторы созданных сообщений.
    kind: str
    text: str = ""
    markdown: bool = False
    files: list[tuple[bytes, str]] = field(default_factory=list)
    caption: str = ""
    force_document: bool = False
    #: Как именно отправлять файл: "voice" — голосовое, "round" — кружок.
    mode: str = ""
    #: Принимающая сторона склеивает отправленное в одно сообщение.
    merged: bool = False
    #: Отвечать на сообщение предыдущего сценария с этим ключом.
    reply_to_key: str | None = None
    #: После отправки заменить текст — проверка переноса правок.
    edit_to: str | None = None


def _png(color: tuple[int, int, int], size: int = 64) -> bytes:
    """Сгенерировать небольшую картинку для проверки медиа."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gif(size: int = 64) -> bytes:
    """Короткая анимация: проверка того, что GIF доезжает анимацией."""
    from PIL import Image

    frames = [Image.new("RGB", (size, size), color) for color in ((200, 40, 40), (40, 60, 200))]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0
    )
    return buffer.getvalue()


def _asset(name: str) -> bytes:
    """Прочитать эталонный файл, лежащий рядом с кодом.

    Голосовое и кружок нельзя собрать на лету без внешних кодеков, поэтому
    заранее подготовленные образцы едут вместе с пакетом.
    """
    return (Path(__file__).parent / "assets" / name).read_bytes()


def _jpeg(color: tuple[int, int, int], size: int = 64) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def build_scenarios(nonce: str) -> list[Scenario]:
    """Собрать список проверок с уникальной меткой прогона."""
    tag = f"selftest {nonce}"
    return [
        Scenario(
            key="text",
            title="Простой текст",
            kind="text",
            text=f"{tag}: простой текст",
        ),
        Scenario(
            key="formatting",
            title="Форматирование (жирный, курсив, код, ссылка)",
            kind="text",
            markdown=True,
            text=(
                f"{tag}: **жирный** __курсив__ `код` [ссылка](https://example.com) ~~зачёркнутый~~"
            ),
        ),
        Scenario(
            key="emoji",
            title="Эмодзи вне BMP + разметка после них",
            kind="text",
            markdown=True,
            text=f"{tag}: 🙂🚀👨‍👩‍👧‍👦 **после эмодзи жирный**",
        ),
        Scenario(
            key="reply",
            title="Ответ на сообщение",
            kind="text",
            text=f"{tag}: ответ на первое сообщение",
            reply_to_key="text",
        ),
        Scenario(
            key="edit",
            title="Правка сообщения",
            kind="text",
            text=f"{tag}: текст до правки",
            edit_to=f"{tag}: текст после правки",
        ),
        Scenario(
            key="long",
            title="Длинный текст (разбиение на части)",
            kind="text",
            text=f"{tag}: " + ("длинная строка проверки. " * 260),
        ),
        Scenario(
            key="photo",
            title="Фотография с подписью",
            kind="file",
            files=[(_jpeg((200, 40, 40)), "selftest_photo.jpg")],
            caption=f"{tag}: фото с подписью",
        ),
        Scenario(
            key="document",
            title="Документ",
            kind="file",
            files=[(f"{tag}\nсодержимое проверочного файла\n".encode(), "selftest.txt")],
            caption=f"{tag}: документ",
            force_document=True,
        ),
        Scenario(
            key="voice",
            title="Голосовое сообщение",
            kind="file",
            files=[(_asset("selftest_voice.ogg"), "selftest_voice.ogg")],
            mode="voice",
        ),
        Scenario(
            key="round",
            title="Видеосообщение (кружок)",
            kind="file",
            files=[(_asset("selftest_round.mp4"), "selftest_round.mp4")],
            mode="round",
        ),
        Scenario(
            key="animation",
            title="Анимация GIF",
            kind="file",
            files=[(_gif(), "selftest_anim.gif")],
            caption=f"{tag}: анимация",
        ),
        Scenario(
            key="album",
            title="Альбом из трёх картинок",
            kind="album",
            merged=True,
            files=[
                (_png((40, 160, 60)), "album_1.png"),
                (_png((60, 60, 200)), "album_2.png"),
                (_jpeg((220, 180, 40)), "album_3.jpg"),
            ],
            caption=f"{tag}: альбом",
        ),
    ]
