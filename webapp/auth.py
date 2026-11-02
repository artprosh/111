"""Проверка Telegram WebApp initData.

Алгоритм — из официальной документации Telegram
(https://core.telegram.org/bots/webapps, раздел
«Validating data received via the Mini App»):

1. data-check-string — все полученные поля, кроме `hash`, отсортированные
   по ключу, в формате `key=<value>`, разделённые символом перевода строки;
2. secret_key = HMAC-SHA256(key="WebAppData", msg=<токен бота>);
3. hex(HMAC-SHA256(key=secret_key, msg=data-check-string)) должен совпасть
   с полем `hash`;
4. дополнительно проверяется свежесть `auth_date`.

Только стандартная библиотека — модуль импортируется и тестируется без aiogram.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# Сколько секунд initData считается действительной. Telegram пересоздаёт
# initData при каждом открытии Mini App, поэтому сутки — с запасом на случай,
# если пользователь долго держит приложение открытым.
DEFAULT_MAX_AGE = 24 * 60 * 60


class InitDataError(Exception):
    """initData отсутствует, подделана или устарела."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None

    @property
    def display_name(self) -> str:
        """Как customer_display_name() в main.py: имя + фамилия + (@username)."""
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        if self.username:
            name += f" (@{self.username})"
        return name or "Покупатель"


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age: int = DEFAULT_MAX_AGE,
    now: float | None = None,
) -> TelegramUser:
    if not init_data:
        raise InitDataError("initData пуста")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InitDataError("В initData нет поля hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise InitDataError("Подпись initData не совпадает")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("Некорректный auth_date") from exc
    current = time.time() if now is None else now
    if max_age and current - auth_date > max_age:
        raise InitDataError("initData устарела")

    try:
        raw_user = json.loads(data.get("user", ""))
        return TelegramUser(
            id=int(raw_user["id"]),
            first_name=str(raw_user.get("first_name") or ""),
            last_name=raw_user.get("last_name") or None,
            username=raw_user.get("username") or None,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise InitDataError("В initData нет корректного user") from exc


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Собрать подписанную initData — для тестов и локального dev-сервера."""
    from urllib.parse import urlencode

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})
