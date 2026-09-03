"""Запуск моста: python -m max2tg [login]."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from .app import login_telegram, main

USAGE = """Использование:
  max2tg          запустить мост
  max2tg login    войти в пользовательскую сессию Telegram (нужно для /sync)
"""


def run() -> None:
    """Синхронная точка входа для консольного скрипта."""
    argument = sys.argv[1] if len(sys.argv) > 1 else ""
    with contextlib.suppress(KeyboardInterrupt):
        if argument == "login":
            asyncio.run(login_telegram())
        elif argument in ("", "run"):
            asyncio.run(main())
        else:
            print(USAGE)


if __name__ == "__main__":
    run()
