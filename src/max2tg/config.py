"""Конфигурация приложения (env / .env)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки моста. Читаются из окружения и файла .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    tg_bot_token: str = Field(alias="TG_BOT_TOKEN")
    tg_admin_ids: list[int] = Field(default_factory=list, alias="TG_ADMIN_IDS")
    # Прокси для Bot API. Если не задан, берётся из HTTPS_PROXY/HTTP_PROXY/ALL_PROXY.
    tg_proxy: str | None = Field(default=None, alias="TG_PROXY")

    # --- Пользовательская сессия Telegram (нужна для /sync: групп и папок) ---
    tg_api_id: int | None = Field(default=None, alias="TG_API_ID")
    tg_api_hash: str | None = Field(default=None, alias="TG_API_HASH")
    tg_phone: str | None = Field(default=None, alias="TG_PHONE")
    tg_session: str = Field(default="telegram_user", alias="TG_SESSION")
    # Прокси для MTProto. Если не задан, берётся TG_PROXY, затем окружение.
    tg_userbot_proxy: str | None = Field(default=None, alias="TG_USERBOT_PROXY")
    tg_folder_name: str = Field(default="MAX (СКАМ)", alias="TG_FOLDER_NAME")
    # Сколько групп создавать за один вызов /sync: Telegram быстро уходит во flood wait.
    sync_group_limit: int = Field(default=20, alias="SYNC_GROUP_LIMIT")
    sync_create_delay: float = Field(default=3.0, alias="SYNC_CREATE_DELAY")
    # Смену фото Telegram ограничивает жёстче отправки сообщений.
    sync_avatar_delay: float = Field(default=2.0, alias="SYNC_AVATAR_DELAY")
    # Сколько последних сообщений чата MAX переносить при первой привязке.
    # 0 отключает перенос истории.
    history_import_limit: int = Field(default=100, alias="HISTORY_IMPORT_LIMIT")
    # Пауза между сообщениями истории: массовая выгрузка рвёт соединение с MAX.
    history_import_delay: float = Field(default=0.4, alias="HISTORY_IMPORT_DELAY")

    # --- Многопользовательский режим ---
    # Ключ шифрования токенов MAX и сессий Telegram. Обязателен, если мостом
    # пользуется кто-то кроме владельца сервера.
    master_key: str | None = Field(default=None, alias="MASTER_KEY")
    # Пускать ли посторонних пользователей. Если выключено, работать могут
    # только те, кто перечислен в TG_ADMIN_IDS.
    allow_public_signup: bool = Field(default=True, alias="ALLOW_PUBLIC_SIGNUP")
    # Сколько аккаунтов MAX разрешено подключить одному пользователю.
    max_accounts_per_user: int = Field(default=3, alias="MAX_ACCOUNTS_PER_USER")

    # --- MAX ---
    max_device_type: str = Field(default="WEB", alias="MAX_DEVICE_TYPE")
    max_token_suffix: str | None = Field(default=None, alias="MAX_TOKEN_SUFFIX")
    max_password: str | None = Field(default=None, alias="MAX_PASSWORD")
    # Пересылать в Telegram собственные сообщения, написанные из клиента MAX.
    # Сообщения, отправленные самим мостом, отсеиваются по таблице соответствий.
    max_forward_own_messages: bool = Field(default=True, alias="MAX_FORWARD_OWN_MESSAGES")

    # --- Хранилище ---
    database_url: str = Field(default="sqlite+aiosqlite:///./max2tg.sqlite3", alias="DATABASE_URL")
    state_dir: Path = Field(default=Path("."), alias="STATE_DIR")

    # --- Лимиты вложений ---
    # Bot API отдаёт боту файлы не больше 20 МиБ и принимает не больше 50 МиБ.
    tg_download_limit: int = Field(default=20 * 1024 * 1024, alias="TG_DOWNLOAD_LIMIT")
    tg_upload_limit: int = Field(default=50 * 1024 * 1024, alias="TG_UPLOAD_LIMIT")
    max_upload_limit: int = Field(default=2 * 1024 * 1024 * 1024, alias="MAX_UPLOAD_LIMIT")
    # Длина одной части сообщения в MAX. Сервер рвёт соединение на слишком
    # длинных сообщениях, поэтому предел взят с запасом.
    max_text_limit: int = Field(default=3000, alias="MAX_TEXT_LIMIT")

    # --- Троттлинг ---
    tg_global_rate: float = Field(default=25.0, alias="TG_GLOBAL_RATE")
    tg_chat_rate: float = Field(default=1.0, alias="TG_CHAT_RATE")
    max_global_rate: float = Field(default=4.0, alias="MAX_GLOBAL_RATE")
    max_chat_rate: float = Field(default=1.0, alias="MAX_CHAT_RATE")

    # --- Прочее ---
    media_group_delay: float = Field(default=1.0, alias="MEDIA_GROUP_DELAY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Как переносить форматирование Telegram -> MAX: elements | tags | plain
    tg_to_max_formatting: str = Field(default="elements", alias="TG_TO_MAX_FORMATTING")

    @field_validator("tg_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Разрешить перечисление админов строкой "1,2,3".

        Единственный идентификатор pydantic-settings успевает разобрать как
        число — тогда до валидатора доезжает int, а не строка.
        """
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @field_validator("tg_to_max_formatting")
    @classmethod
    def _check_formatting(cls, value: str) -> str:
        allowed = {"elements", "tags", "plain"}
        if value not in allowed:
            raise ValueError(f"TG_TO_MAX_FORMATTING должен быть одним из {sorted(allowed)}")
        return value


def load_settings() -> Settings:
    """Загрузить настройки из окружения."""
    return Settings()  # type: ignore[call-arg]
