"""Шифрование учётных данных, которые мост хранит за пользователей.

В общем сервисе в базе лежат токены чужих аккаунтов MAX и строки сессий
Telegram. Открытым текстом такое хранить нельзя: файл базы легко утекает
вместе с бэкапом.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

logger = logging.getLogger("crypto")

#: Префикс, по которому видно, что значение зашифровано этим модулем.
PREFIX = "enc:v1:"


class SecretBox:
    """Симметричное шифрование секретов на мастер-ключе.

    Используется ``Fernet`` из ``cryptography``, если библиотека доступна.
    Без ключа мост работает, но предупреждает: это допустимо для личной
    установки и недопустимо, когда аккаунты подключают посторонние люди.
    """

    def __init__(self, master_key: str | None) -> None:
        self._fernet: Any = self._build(master_key)

    @property
    def enabled(self) -> bool:
        """Шифрование действительно работает."""
        return self._fernet is not None

    @staticmethod
    def _build(master_key: str | None) -> Any:
        if not master_key:
            return None
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            logger.error(
                "Пакет cryptography не установлен — секреты будут храниться открытым текстом"
            )
            return None
        # Ключ приводится к формату Fernet, чтобы в .env можно было писать
        # произвольную строку, а не только корректный base64 нужной длины.
        digest = hashlib.sha256(master_key.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        """Лежит ли значение в базе уже зашифрованным."""
        return bool(value and value.startswith(PREFIX))

    def encrypt(self, value: str | None) -> str | None:
        """Зашифровать значение для хранения в базе."""
        if value is None:
            return None
        if self._fernet is None:
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return PREFIX + token

    def decrypt(self, value: str | None) -> str | None:
        """Расшифровать значение из базы.

        Незашифрованные значения возвращаются как есть: так база, созданная
        до появления мастер-ключа, продолжает работать.
        """
        if value is None:
            return None
        if not value.startswith(PREFIX):
            return value
        if self._fernet is None:
            logger.error("В базе зашифрованные данные, но MASTER_KEY не задан")
            return None
        try:
            return str(self._fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8"))
        except Exception:
            logger.error("Не удалось расшифровать секрет — проверьте MASTER_KEY")
            return None
