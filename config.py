from __future__ import annotations

import os
from pathlib import Path


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Required secret {name} is not set. Add it in the hosting environment variables."
        )
    return value


BOT_TOKEN = _required_env("TELEGRAM_BOT_TOKEN")

MAX_ADMINS = 20  # до 20 администраторов (владелец, 25.10.2026)


def parse_admin_ids(raw: str) -> tuple[int, ...]:
    """ADMIN_ID: один или несколько Telegram ID через запятую (до MAX_ADMINS).
    Пробелы вокруг запятых допускаются: «111,222» и «111, 222» — одно и то же."""
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        raise RuntimeError("ADMIN_ID is empty. Set one or more numeric Telegram user IDs separated by commas.")
    ids: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError(
                f"ADMIN_ID: «{part}» is not a numeric Telegram user ID. "
                "Use IDs separated by commas, e.g. 111111111,222222222"
            ) from exc
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_ADMINS:
        raise RuntimeError(f"ADMIN_ID: at most {MAX_ADMINS} admins are supported, got {len(ids)}.")
    return tuple(ids)


ADMIN_IDS = parse_admin_ids(_required_env("ADMIN_ID"))
# Первый ID — «главный»: в его чат бот загружает служебные фото (file_id).
ADMIN_ID = ADMIN_IDS[0]

_group_chat_id = os.getenv("GROUP_CHAT_ID")
try:
    GROUP_CHAT_ID = int(_group_chat_id) if _group_chat_id else None
except ValueError as exc:
    raise RuntimeError("GROUP_CHAT_ID must be a numeric Telegram chat ID.") from exc

# A separate group where key order events (new order, taken by courier,
# delivered, admin called) are duplicated for admins/couriers to keep an eye
# on activity and talk to each other. Optional — if unset, nothing is sent.
_log_group_chat_id = os.getenv("LOG_GROUP_CHAT_ID")
try:
    LOG_GROUP_CHAT_ID = int(_log_group_chat_id) if _log_group_chat_id else None
except ValueError as exc:
    raise RuntimeError("LOG_GROUP_CHAT_ID must be a numeric Telegram chat ID.") from exc


ROOT_DIR = Path(__file__).resolve().parent


def _default_db_path() -> Path:
    """Prefer Amvera's persistent volume (/data — declared as
    persistenceMount in amvera.yaml) so the SQLite database survives
    redeploys: on Amvera, every new deploy gets a brand-new container
    filesystem, and anything NOT under /data is wiped — including a
    database that used to live under the app's own code folder, which is
    exactly what happened before this fix (products, their photos, and
    orders were all lost on the first redeploy after go-live).
    Falls back to a local ./data folder when /data isn't usable (e.g.
    running outside Amvera, such as on a laptop or in this sandbox),
    so local development and testing still work with no extra setup."""
    persistent_dir = Path("/data")
    try:
        persistent_dir.mkdir(parents=True, exist_ok=True)
        probe = persistent_dir / ".write_test"
        probe.touch()
        probe.unlink()
        return persistent_dir / "bot.sqlite3"
    except OSError:
        return ROOT_DIR / "data" / "bot.sqlite3"


DB_PATH = Path(os.getenv("SQLITE_PATH", str(_default_db_path())))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------ Mini App
# Публичный HTTPS-адрес, по которому Telegram открывает Mini App (например,
# бесплатный домен Amvera https://<проект>-<логин>.<регион>.amvera.tech/).
# Telegram открывает Mini App только по https. Пока переменная не задана,
# кнопка «🖥 Открыть приложение» в боте не показывается, а бот работает как раньше.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip() or None

# Где слушает веб-сервер Mini App. Amvera по умолчанию публикует порт 80
# (run.containerPort в amvera.yaml), и приложение должно слушать 0.0.0.0 —
# см. https://docs.amvera.ru/general/FAQ/502-503-error.html
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
try:
    WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "80"))
except ValueError as exc:
    raise RuntimeError("WEBAPP_PORT must be a port number.") from exc

# WEBAPP_ENABLED=0 полностью выключает веб-сервер (бот работает как раньше).
WEBAPP_ENABLED = os.getenv("WEBAPP_ENABLED", "1").strip() not in ("0", "false", "no", "")

# Бот — только вход в приложение и уведомления (28.10.2026); прежнего меню бота больше нет,
# переменная WEBAPP_ONLY не используется.

# Ключ openrouteservice — маршрут по дорогам для доставки за пределы Москвы
# (500 ₽ + цена за км задаются в админке). Пусто — авторасчёт выключен.
ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
