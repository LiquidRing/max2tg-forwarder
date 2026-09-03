"""Проверка переноса форматирования между платформами."""

from __future__ import annotations

from dataclasses import dataclass

from max2tg.models import TextSpan
from max2tg.util.text import (
    entities_to_spans,
    max_elements_to_spans,
    spans_to_html,
    spans_to_max_elements,
    spans_to_max_tags,
    utf16_length,
)


@dataclass
class FakeEntity:
    """Заглушка aiogram.types.MessageEntity."""

    type: str
    offset: int
    length: int
    url: str | None = None


def test_entities_to_max_elements() -> None:
    spans = entities_to_spans(
        [
            FakeEntity(type="bold", offset=0, length=6),
            FakeEntity(type="text_link", offset=7, length=5, url="https://example.com"),
            FakeEntity(type="spoiler", offset=0, length=3),
        ]
    )
    elements = spans_to_max_elements(spans)
    assert {"type": "STRONG", "from": 0, "length": 6, "attributes": {}} in elements
    assert {
        "type": "LINK",
        "from": 7,
        "length": 5,
        "attributes": {"url": "https://example.com"},
    } in elements
    # Спойлер не поддерживается MAX — пропускается.
    assert len(elements) == 2


def test_spans_to_max_tags() -> None:
    text = "Привет мир"
    spans = [TextSpan(type="STRONG", offset=0, length=6)]
    assert spans_to_max_tags(text, spans) == "<STRONG>Привет</STRONG> мир"


def test_max_elements_to_html() -> None:
    text = "жирный курсив"
    spans = max_elements_to_spans(
        [
            {"type": "STRONG", "from": 0, "length": 6, "attributes": {}},
            {"type": "EMPHASIZED", "from": 7, "length": 6},
            {"type": "UNKNOWN", "from": 0, "length": 2},
        ]
    )
    assert spans_to_html(text, spans) == "<b>жирный</b> <i>курсив</i>"


def test_html_escaping() -> None:
    assert spans_to_html("a < b & c", []) == "a &lt; b &amp; c"


def test_overlapping_spans_are_nested_correctly() -> None:
    text = "abcdef"
    spans = [
        TextSpan(type="STRONG", offset=0, length=4),
        TextSpan(type="EMPHASIZED", offset=2, length=4),
    ]
    rendered = spans_to_html(text, spans)
    # Разметка должна остаться валидной (без пересечения тегов).
    assert rendered == "<b>ab</b><b><i>cd</i></b><i>ef</i>"


def test_utf16_offsets_with_emoji() -> None:
    text = "🙂жирный"
    assert utf16_length("🙂") == 2
    spans = [TextSpan(type="STRONG", offset=2, length=6)]
    assert spans_to_html(text, spans) == "🙂<b>жирный</b>"
