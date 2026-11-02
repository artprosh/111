"""Диспетчерская карта для ПК (/dispatch) — вход без Telegram (25.10.2026).

Как входит админ:
1. В Mini App: Админка → «🗺 Карта» → «🖥 Карта для ПК». Бот присылает админу
   ссылку /dispatch/login/<токен>. Ссылка не истекает по времени, но:
   - открыть её можно ОДИН раз (переслали другу после входа — у друга ошибка);
   - новая ссылка отменяет старую неоткрытую.
2. На ПК ссылка открывает страницу с кнопкой «Войти» (сама ссылка вход не
   выполняет: так её не «съест» предпросмотр ссылок в мессенджерах).
   Кнопка → вход: браузер получает httpOnly-cookie, ссылка гасится.
3. Вход действует 24 часа с момента открытия ссылки (SESSION_TTL), потом —
   новая ссылка. Бот сообщает админу о каждом входе, в сообщении — кнопка
   «Отозвать этот вход». В Mini App — список входов, «Выйти» и «Выйти везде».

В базе хранятся только SHA-256 от токенов ссылки и сессии: утечка файла базы
не даёт войти. Доступ ещё раз проверяется на каждом запросе — ID должен быть
в ADMIN_ID (убрали админа — его входы перестают работать сразу).

Модуль без aiohttp/aiogram: логика проверяется тестами и в dev-сервере.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any

SESSION_TTL = 24 * 60 * 60  # вход на ПК — сутки с момента открытия ссылки
COOKIE_NAME = "dsp_sid"
COOKIE_PATH = "/dispatch"
LINK_PATH = "/dispatch/login/"
MAX_UA = 300


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def browser_label(user_agent: str | None) -> str:
    """Коротко, что за браузер: «Chrome · Windows». Только для подписи входа."""
    ua = user_agent or ""
    browser = next(
        (name for key, name in (
            ("Edg/", "Edge"), ("YaBrowser/", "Яндекс Браузер"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
            ("Chrome/", "Chrome"), ("Safari/", "Safari"),
        ) if key in ua),
        "Браузер",
    )
    system = next(
        (name for key, name in (
            ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Android", "Android"), ("iPhone", "iPhone"),
            ("iPad", "iPad"), ("Linux", "Linux"),
        ) if key in ua),
        "",
    )
    return f"{browser} · {system}" if system else browser


class DispatchAuth:
    """Одноразовые ссылки и сессии диспетчерской карты (таблицы в той же базе)."""

    def __init__(self, db, now=time.time) -> None:
        self.db = db
        self.now = now  # подменяется в тестах
        self._tables = False  # таблицы создаются при первом обращении

    def _conn(self):
        if not self._tables:
            self._ensure_tables()
            self._tables = True
        return self.db.connection()

    def _ensure_tables(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dispatch_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_ts INTEGER NOT NULL,
                    used_ts INTEGER,
                    revoked_ts INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_links_admin ON dispatch_links(admin_id);
                CREATE TABLE IF NOT EXISTS dispatch_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    link_id INTEGER,
                    created_ts INTEGER NOT NULL,
                    expires_ts INTEGER NOT NULL,
                    revoked_ts INTEGER,
                    last_seen_ts INTEGER,
                    user_agent TEXT,
                    ip TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_admin ON dispatch_sessions(admin_id);
                """
            )

    # ------------------------------------------------------------ ссылки
    def issue_link(self, admin_id: int) -> str:
        """Новая одноразовая ссылка админу. Старые неоткрытые — отменяются."""
        token = secrets.token_urlsafe(32)
        now = int(self.now())
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE dispatch_links SET revoked_ts = ? WHERE admin_id = ? AND used_ts IS NULL AND revoked_ts IS NULL",
                    (now, int(admin_id)),
                )
                conn.execute(
                    "INSERT INTO dispatch_links (admin_id, token_hash, created_ts) VALUES (?, ?, ?)",
                    (int(admin_id), _hash(token), now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return token

    def link_state(self, token: str) -> str:
        """ok | used | revoked | unknown — для страницы перед кнопкой «Войти»."""
        if not token or len(token) > 100:
            return "unknown"
        with self._conn() as conn:
            row = conn.execute(
                "SELECT used_ts, revoked_ts FROM dispatch_links WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
        if not row:
            return "unknown"
        if row["used_ts"] is not None:
            return "used"
        if row["revoked_ts"] is not None:
            return "revoked"
        return "ok"

    def redeem(self, token: str, user_agent: str | None, ip: str | None) -> tuple[str, dict[str, Any]] | str:
        """Погасить ссылку и открыть сессию. Возвращает (токен сессии, сессия)
        или причину отказа: used | revoked | unknown. Гашение атомарное: из двух
        одновременных попыток войти успешна только одна."""
        if not token or len(token) > 100:
            return "unknown"
        now = int(self.now())
        session_token = secrets.token_urlsafe(32)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                link = conn.execute(
                    "SELECT id, admin_id, used_ts, revoked_ts FROM dispatch_links WHERE token_hash = ?",
                    (_hash(token),),
                ).fetchone()
                if not link:
                    conn.execute("ROLLBACK")
                    return "unknown"
                if link["used_ts"] is not None or link["revoked_ts"] is not None:
                    conn.execute("ROLLBACK")
                    return "used" if link["used_ts"] is not None else "revoked"
                updated = conn.execute(
                    "UPDATE dispatch_links SET used_ts = ? WHERE id = ? AND used_ts IS NULL AND revoked_ts IS NULL",
                    (now, link["id"]),
                )
                if updated.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return "used"
                cur = conn.execute(
                    """INSERT INTO dispatch_sessions
                       (admin_id, token_hash, link_id, created_ts, expires_ts, last_seen_ts, user_agent, ip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (int(link["admin_id"]), _hash(session_token), link["id"], now, now + SESSION_TTL, now,
                     (user_agent or "")[:MAX_UA], (ip or "")[:64]),
                )
                session_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return session_token, self._session_json_by_id(session_id)

    # ------------------------------------------------------------ сессии
    def session(self, session_token: str | None) -> dict[str, Any] | None:
        """Действующая сессия по cookie (не отозвана и не истекла) или None."""
        if not session_token or len(session_token) > 100:
            return None
        now = int(self.now())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dispatch_sessions WHERE token_hash = ?", (_hash(session_token),)
            ).fetchone()
            if not row or row["revoked_ts"] is not None or int(row["expires_ts"]) <= now:
                return None
            if not row["last_seen_ts"] or now - int(row["last_seen_ts"]) >= 60:
                conn.execute("UPDATE dispatch_sessions SET last_seen_ts = ? WHERE id = ?", (now, row["id"]))
        return self._session_json(row)

    def _session_json_by_id(self, session_id: int) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM dispatch_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session_json(row)

    def _session_json(self, row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "admin_id": int(row["admin_id"]),
            "created_at": _iso(row["created_ts"]),
            "expires_at": _iso(row["expires_ts"]),
            "expires_ts": int(row["expires_ts"]),
            "last_seen_at": _iso(row["last_seen_ts"]),
            "browser": browser_label(row["user_agent"]),
            "ip": row["ip"] or None,
        }

    def list_sessions(self, admin_id: int) -> list[dict[str, Any]]:
        now = int(self.now())
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM dispatch_sessions
                   WHERE admin_id = ? AND revoked_ts IS NULL AND expires_ts > ?
                   ORDER BY created_ts DESC""",
                (int(admin_id), now),
            ).fetchall()
        return [self._session_json(r) for r in rows]

    def pending_link(self, admin_id: int) -> dict[str, Any] | None:
        """Есть ли выданная, но ещё не открытая ссылка (для подсказки в Mini App)."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT created_ts FROM dispatch_links
                   WHERE admin_id = ? AND used_ts IS NULL AND revoked_ts IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (int(admin_id),),
            ).fetchone()
        return {"created_at": _iso(row["created_ts"])} if row else None

    def revoke_session(self, session_id: int, admin_id: int) -> bool:
        """Отозвать один вход. Админ может отозвать только свой."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE dispatch_sessions SET revoked_ts = ? WHERE id = ? AND admin_id = ? AND revoked_ts IS NULL",
                (int(self.now()), int(session_id), int(admin_id)),
            )
        return cur.rowcount == 1

    def revoke_all(self, admin_id: int) -> int:
        """«Выйти везде»: все входы админа и его неоткрытые ссылки."""
        now = int(self.now())
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE dispatch_sessions SET revoked_ts = ? WHERE admin_id = ? AND revoked_ts IS NULL AND expires_ts > ?",
                    (now, int(admin_id), now),
                )
                conn.execute(
                    "UPDATE dispatch_links SET revoked_ts = ? WHERE admin_id = ? AND used_ts IS NULL AND revoked_ts IS NULL",
                    (now, int(admin_id)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return cur.rowcount


# ---------------------------------------------------------------- Mini App: «🖥 Карта для ПК»
def install_routes(api) -> None:
    """Маршруты Mini App (только админ): выдать ссылку, список входов, выход."""
    from .core import ApiError

    auth: DispatchAuth = api.dispatch

    async def link(user, body, query) -> dict[str, Any]:
        base = (getattr(api, "public_url", None) or "").rstrip("/")
        if not base:
            raise ApiError(503, "Не задан адрес сервера (WEBAPP_URL) — ссылку не собрать.")
        url = base + LINK_PATH + auth.issue_link(user.id)
        sent = True
        try:
            await api.bridge.dispatch_link(user.id, url)
        except Exception:
            sent = False
        return {"url": url, "sent": sent, "hours": SESSION_TTL // 3600}

    async def sessions(user, body, query) -> dict[str, Any]:
        return {"sessions": auth.list_sessions(user.id), "pending_link": auth.pending_link(user.id),
                "hours": SESSION_TTL // 3600}

    async def revoke(user, body, query, session_id: int) -> dict[str, Any]:
        if not auth.revoke_session(session_id, user.id):
            raise ApiError(404, "Вход не найден или уже закрыт.")
        return await sessions(user, body, query)

    async def revoke_all(user, body, query) -> dict[str, Any]:
        count = auth.revoke_all(user.id)
        return {**(await sessions(user, body, query)), "revoked": count}

    api._route("POST", r"/api/admin/dispatch/link", link, "admin")
    api._route("GET", r"/api/admin/dispatch/sessions", sessions, "admin")
    api._route("POST", r"/api/admin/dispatch/sessions/(\d+)/revoke", revoke, "admin")
    api._route("POST", r"/api/admin/dispatch/sessions/revoke-all", revoke_all, "admin")


# ---------------------------------------------------------------- что можно делать с ПК
# Карта видит и делает только это (через тот же Api.handle, что и Mini App):
# опасные действия админки (очистка базы, настройки, товары) с ПК недоступны.
PC_ALLOWED = (
    ("GET", "/api/admin/orders"),
    ("GET", "/api/admin/map"),
    ("GET", "/api/admin/couriers"),
    ("POST", "/api/admin/orders/assign"),
)


def pc_allowed(method: str, path: str) -> bool:
    return (method, path) in PC_ALLOWED
