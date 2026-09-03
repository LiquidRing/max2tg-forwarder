"""Схема и подключение к базе состояния (SQLite через SQLAlchemy async)."""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, Text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("db")


class Base(DeclarativeBase):
    """Базовый класс моделей."""


class MaxAccount(Base):
    """Подключённый аккаунт MAX и его владелец в Telegram."""

    __tablename__ = "max_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Кто из пользователей Telegram владеет этим аккаунтом.
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    #: Имя владельца аккаунта в MAX — идёт префиксом в названия групп.
    nickname: Mapped[str] = mapped_column(String(128), default="MAX")
    #: Идентификатор пользователя внутри MAX.
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Токен сессии MAX (зашифрован мастер-ключом).
    token: Mapped[str | None] = mapped_column(Text, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class TelegramSession(Base):
    """Пользовательская сессия Telegram для автосоздания групп."""

    __tablename__ = "telegram_sessions"

    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: Строка сессии Telethon (зашифрована мастер-ключом).
    session: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Binding(Base):
    """Связь «группа Telegram <-> чат MAX»."""

    __tablename__ = "bindings"

    tg_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: Аккаунт MAX, которому принадлежит чат.
    account_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    max_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str | None] = mapped_column(String(256), default=None)
    #: Адрес картинки MAX, уже перенесённой в аватар группы.
    icon_url: Mapped[str | None] = mapped_column(String(512), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class MessageMap(Base):
    """Соответствие идентификаторов сообщений между платформами.

    Пара не уникальна ни с одной стороны: сообщение MAX может превратиться в
    несколько сообщений Telegram (альбом), а длинное сообщение Telegram — в
    несколько сообщений MAX. Уникальность по стороне Telegram однажды стоила
    потери частей: сохранялась только последняя, а остальные возвращались из
    MAX эхом при переносе истории.
    """

    __tablename__ = "message_map"
    __table_args__ = (
        Index("ix_message_map_tg", "tg_chat_id", "tg_message_id"),
        Index("ix_message_map_max", "account_id", "max_chat_id", "max_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    #: Аккаунт MAX: идентификаторы чатов уникальны только внутри аккаунта.
    account_id: Mapped[int] = mapped_column(Integer, default=0)
    max_chat_id: Mapped[int] = mapped_column(BigInteger)
    max_message_id: Mapped[str] = mapped_column(String(64))
    #: Направление, в котором сообщение было создано: "max2tg" | "tg2max".
    direction: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


def create_engine(database_url: str) -> AsyncEngine:
    """Создать асинхронный движок."""
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Создать фабрику сессий."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_models(engine: AsyncEngine) -> None:
    """Создать таблицы и дополнить схему недостающими колонками."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(connection: Any) -> None:
    """Простейшая миграция: дописать колонки, появившиеся после создания базы.

    Alembic для одной таблицы состояния избыточен, а create_all существующие
    таблицы не трогает — поэтому недостающие колонки добавляются вручную.
    """
    for table, column, definition in (
        ("bindings", "icon_url", "VARCHAR(512)"),
        ("bindings", "account_id", "INTEGER DEFAULT 0"),
        ("message_map", "account_id", "INTEGER DEFAULT 0"),
    ):
        existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    _drop_legacy_unique(connection)


def _drop_legacy_unique(connection: Any) -> None:
    """Снять уникальность по стороне Telegram со старых баз.

    SQLite не умеет удалять ограничение из таблицы, поэтому таблица
    пересоздаётся с переносом строк — их немного, это одна операция при
    первом запуске обновлённого моста.
    """
    schema = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_map'"
    ).fetchone()
    if not schema or "uq_message_map_tg" not in (schema[0] or ""):
        return

    connection.exec_driver_sql("ALTER TABLE message_map RENAME TO message_map_old")
    connection.exec_driver_sql(
        """
        CREATE TABLE message_map (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            tg_chat_id BIGINT NOT NULL,
            tg_message_id BIGINT NOT NULL,
            account_id INTEGER DEFAULT 0,
            max_chat_id BIGINT NOT NULL,
            max_message_id VARCHAR(64) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            created_at INTEGER
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO message_map
            (id, tg_chat_id, tg_message_id, account_id, max_chat_id,
             max_message_id, direction, created_at)
        SELECT id, tg_chat_id, tg_message_id, account_id, max_chat_id,
               max_message_id, direction, created_at
        FROM message_map_old
        """
    )
    connection.exec_driver_sql("DROP TABLE message_map_old")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_message_map_tg ON message_map (tg_chat_id, tg_message_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_message_map_max "
        "ON message_map (account_id, max_chat_id, max_message_id)"
    )
    logger.warning("Снята устаревшая уникальность message_map по стороне Telegram")
