"""Проверка разбора адреса прокси."""

from __future__ import annotations

import pytest

from max2tg.util.net import parse_proxy, resolve_proxy_url


def test_parse_http_proxy() -> None:
    assert parse_proxy("http://127.0.0.1:3067") == {
        "proxy_type": "http",
        "addr": "127.0.0.1",
        "port": 3067,
        "rdns": True,
    }


def test_parse_socks_with_credentials() -> None:
    proxy = parse_proxy("socks5://user:secret@proxy.local:1080")
    assert proxy is not None
    assert proxy["proxy_type"] == "socks5"
    assert proxy["addr"] == "proxy.local"
    assert proxy["port"] == 1080
    assert proxy["username"] == "user"
    assert proxy["password"] == "secret"


def test_parse_bare_host_defaults_to_http() -> None:
    proxy = parse_proxy("127.0.0.1:8080")
    assert proxy is not None
    assert proxy["proxy_type"] == "http"
    assert proxy["port"] == 8080


@pytest.mark.parametrize("value", [None, "", "ftp://host:21", "http://host"])
def test_unusable_proxy_values(value: str | None) -> None:
    assert parse_proxy(value) is None


def test_configured_proxy_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env:1")
    assert resolve_proxy_url("http://configured:2") == "http://configured:2"
    assert resolve_proxy_url(None) == "http://env:1"


class _FakeContent:
    """Тело ответа, приходящее несколькими блоками."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.content = _FakeContent(chunks)


@pytest.mark.asyncio
async def test_read_limited_collects_every_chunk() -> None:
    """Тело собирается целиком: обрезанный файл выглядит валидным, но битый."""
    from max2tg.util.net import read_limited

    response = _FakeResponse([b"RIFF", b"....", b"WEBP", b"payload"])
    assert await read_limited(response, 1024) == b"RIFF....WEBPpayload"


@pytest.mark.asyncio
async def test_read_limited_rejects_oversized_body() -> None:
    from max2tg.util.net import read_limited

    response = _FakeResponse([b"a" * 600, b"b" * 600])
    assert await read_limited(response, 1000) is None
