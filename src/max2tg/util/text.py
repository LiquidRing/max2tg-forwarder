"""Перенос форматирования текста между Telegram и MAX.

Telegram отдаёт разметку списком сущностей со смещениями в кодовых единицах
UTF-16; MAX — списком «элементов» такого же вида. Внутри моста и то и другое
приводится к :class:`~max2tg.models.TextSpan`.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Any

from ..models import TextSpan

#: Тип сущности Telegram -> тип элемента MAX.
TG_TO_MAX: dict[str, str] = {
    "bold": "STRONG",
    "italic": "EMPHASIZED",
    "underline": "UNDERLINE",
    "strikethrough": "STRIKETHROUGH",
    "blockquote": "QUOTE",
    "expandable_blockquote": "QUOTE",
    "text_link": "LINK",
    "code": "MONOSPACED",
    "pre": "MONOSPACED",
}

#: Тип элемента MAX -> HTML-тег Telegram.
MAX_TO_HTML: dict[str, str] = {
    "STRONG": "b",
    "EMPHASIZED": "i",
    "UNDERLINE": "u",
    "STRIKETHROUGH": "s",
    "QUOTE": "blockquote",
    "MONOSPACED": "code",
    "CODE": "code",
    "HEADING": "b",
    "LINK": "a",
}

MAX_TAGS: frozenset[str] = frozenset(
    {"STRONG", "EMPHASIZED", "UNDERLINE", "STRIKETHROUGH", "QUOTE", "LINK"}
)


def utf16_length(text: str) -> int:
    """Длина текста в кодовых единицах UTF-16 — единицах смещений разметки."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _utf16_positions(text: str) -> list[int]:
    """Отображение «смещение в UTF-16» -> «индекс символа Python»."""
    positions: list[int] = []
    for index, char in enumerate(text):
        positions.extend([index] * (2 if ord(char) > 0xFFFF else 1))
    positions.append(len(text))
    return positions


def _to_char_range(positions: Sequence[int], offset: int, length: int) -> tuple[int, int] | None:
    """Перевести диапазон UTF-16 в диапазон индексов символов."""
    if offset < 0 or length <= 0 or offset >= len(positions) - 1:
        return None
    end = min(offset + length, len(positions) - 1)
    start_char = positions[offset]
    end_char = positions[end]
    if end_char <= start_char:
        return None
    return start_char, end_char


def entities_to_spans(entities: Iterable[Any] | None) -> list[TextSpan]:
    """Сущности aiogram -> список диапазонов форматирования."""
    spans: list[TextSpan] = []
    for entity in entities or []:
        max_type = TG_TO_MAX.get(getattr(entity, "type", ""))
        if max_type is None:
            continue
        url = getattr(entity, "url", None)
        if max_type == "LINK" and not url:
            continue
        spans.append(
            TextSpan(
                type=max_type,
                offset=int(entity.offset),
                length=int(entity.length),
                url=url,
            )
        )
    return spans


def max_elements_to_spans(elements: Iterable[dict[str, Any]] | None) -> list[TextSpan]:
    """Элементы MAX -> список диапазонов форматирования."""
    spans: list[TextSpan] = []
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        element_type = str(element.get("type") or "").upper()
        if element_type not in MAX_TO_HTML:
            continue
        attributes = element.get("attributes") or {}
        url = attributes.get("url") if isinstance(attributes, dict) else None
        try:
            offset = int(element.get("from", 0))
            length = int(element.get("length", 0))
        except (TypeError, ValueError):
            continue
        if element_type == "LINK" and not url:
            continue
        spans.append(TextSpan(type=element_type, offset=offset, length=length, url=url))
    return spans


def spans_to_max_elements(spans: Iterable[TextSpan]) -> list[dict[str, Any]]:
    """Диапазоны -> элементы в формате MAX."""
    elements: list[dict[str, Any]] = []
    for span in spans:
        if span.type not in MAX_TAGS:
            continue
        element: dict[str, Any] = {
            "type": span.type,
            "from": span.offset,
            "length": span.length,
            "attributes": {"url": span.url} if span.type == "LINK" and span.url else {},
        }
        elements.append(element)
    return elements


def spans_to_max_tags(text: str, spans: Iterable[TextSpan]) -> str:
    """Диапазоны -> текст с XML-подобными тегами MAX.

    Запасной вариант для случаев, когда сервер игнорирует ``elements``.
    """
    return _render(text, spans, renderer=_max_tag_renderer)


def spans_to_html(text: str, spans: Iterable[TextSpan]) -> str:
    """Диапазоны -> HTML для Telegram (с экранированием текста)."""
    return _render(text, spans, renderer=_html_renderer)


def escape_html(text: str) -> str:
    """Экранировать текст для отправки в Telegram с parse_mode=HTML."""
    return html.escape(text, quote=False)


# --------------------------------------------------------------------------- #
# Рендеринг
# --------------------------------------------------------------------------- #

_Style = tuple[str, str | None]


def _max_tag_renderer(style: _Style) -> tuple[str, str]:
    tag, url = style
    if tag == "LINK" and url:
        return f'<LINK url="{html.escape(url, quote=True)}">', "</LINK>"
    return f"<{tag}>", f"</{tag}>"


def _html_renderer(style: _Style) -> tuple[str, str]:
    tag, url = style
    html_tag = MAX_TO_HTML.get(tag)
    if html_tag is None:
        return "", ""
    if html_tag == "a":
        if not url:
            return "", ""
        return f'<a href="{html.escape(url, quote=True)}">', "</a>"
    return f"<{html_tag}>", f"</{html_tag}>"


def _render(
    text: str,
    spans: Iterable[TextSpan],
    renderer: Any,
) -> str:
    """Собрать размеченный текст, гарантируя корректную вложенность тегов.

    Диапазоны Telegram и MAX могут пересекаться произвольно, поэтому разметка
    строится посимвольно: на каждой границе изменения набора стилей все открытые
    теги закрываются и открываются заново в стабильном порядке.
    """
    escape = renderer is _html_renderer
    if not text:
        return ""

    positions = _utf16_positions(text)
    per_char: list[list[_Style]] = [[] for _ in range(len(text))]
    for span in spans:
        char_range = _to_char_range(positions, span.offset, span.length)
        if char_range is None:
            continue
        style: _Style = (span.type, span.url)
        if not renderer(style)[0]:
            # Стиль не поддерживается получателем — оставляем текст как есть.
            continue
        start, end = char_range
        for index in range(start, end):
            if style not in per_char[index]:
                per_char[index].append(style)

    chunks: list[str] = []
    active: list[_Style] = []

    def close_all() -> None:
        for style in reversed(active):
            chunks.append(renderer(style)[1])
        active.clear()

    for index, char in enumerate(text):
        wanted = per_char[index]
        if wanted != active:
            close_all()
            for style in wanted:
                chunks.append(renderer(style)[0])
                active.append(style)
        chunks.append(html.escape(char, quote=False) if escape else char)
    close_all()
    return "".join(chunks)
