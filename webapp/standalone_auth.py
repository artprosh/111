"""Phone-based sessions for the standalone mobile app.

Telegram is deliberately not used for authentication here. A one-time SMS code
proves control of the phone number; the code and session token are never stored
in plaintext in SQLite. Configure SMSRU_API_ID, APP_AUTH_SECRET,
APP_ADMIN_PHONE, and APP_ADMIN_ID on the server before enabling this module.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .auth import TelegramUser

logger = logging.getLogger(__name__)

OTP_TTL = 5 * 60
OTP_MAX_ATTEMPTS = 5
OTP_PHONE_WINDOW = 15 * 60
OTP_PHONE_LIMIT = 3
OTP_PHONE_DAILY_LIMIT = 10
OTP_IP_WINDOW = 15 * 60
OTP_IP_LIMIT = 60
SESSION_TTL = 30 * 24 * 60 * 60
SMS_TIMEOUT = 12


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_phone(raw: Any) -> tuple[str, str]:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        e164 = "+7" + digits[1:]
    elif len(digits) == 10:
        e164 = "+7" + digits
    else:
        raise AuthError(400, "Введите российский номер телефона в формате +7 900 000-00-00.")
    return e164, e164[1:]


@dataclass(frozen=True)
class AuthResult:
    token: str
    user: TelegramUser


class StandaloneAuth:
    """SMS OTP and opaque bearer sessions backed by the existing SQLite file."""

    def __init__(self, db, auth_secret: str | None = None, sms_api_id: str | None = None) -> None:
        self.db = db
        self.secret = (auth_secret or os.getenv("APP_AUTH_SECRET", "")).encode()
        self.sms_api_id = (sms_api_id or os.getenv("SMSRU_API_ID", "")).strip()
        if len(self.secret) < 32:
            raise RuntimeError("APP_AUTH_SECRET must contain at least 32 characters.")
        if not self.sms_api_id:
            raise RuntimeError("SMSRU_API_ID is not configured on the server.")
        admin_phone = os.getenv("APP_ADMIN_PHONE", "").strip()
        if not admin_phone:
            raise RuntimeError("APP_ADMIN_PHONE is not configured on the server.")
        self.admin_phone, _ = normalize_phone(admin_phone)
        try:
            admin_id = (os.getenv("APP_ADMIN_ID") or os.getenv("ADMIN_ID", "")).split(",", 1)[0].strip()
            self.admin_id = int(admin_id)
        except ValueError as exc:
            raise RuntimeError("APP_ADMIN_ID must be the existing numeric admin ID.") from exc
        self._initialize()

    def _initialize(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_auth_codes (
                    phone TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    first_name TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS app_auth_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_hash TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_app_auth_events_phone_time
                    ON app_auth_events(phone_hash, created_at);
                CREATE INDEX IF NOT EXISTS idx_app_auth_events_ip_time
                    ON app_auth_events(ip_hash, created_at);
                CREATE TABLE IF NOT EXISTS app_accounts (
                    phone TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_app_sessions_user
                    ON app_sessions(user_id, expires_at);
                """
            )

    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(self.secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    async def request_code(self, phone_raw: Any, ip: str | None) -> dict[str, Any]:
        phone, sms_phone = normalize_phone(phone_raw)
        now = int(time.time())
        phone_hash = self._digest("phone", phone)
        ip_hash = self._digest("ip", ip or "unknown")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM app_auth_events WHERE created_at < ?", (now - 24 * 60 * 60,))
            phone_count = conn.execute(
                "SELECT COUNT(*) FROM app_auth_events WHERE phone_hash = ? AND created_at >= ?",
                (phone_hash, now - OTP_PHONE_WINDOW),
            ).fetchone()[0]
            phone_day_count = conn.execute(
                "SELECT COUNT(*) FROM app_auth_events WHERE phone_hash = ? AND created_at >= ?",
                (phone_hash, now - 24 * 60 * 60),
            ).fetchone()[0]
            ip_count = conn.execute(
                "SELECT COUNT(*) FROM app_auth_events WHERE ip_hash = ? AND created_at >= ?",
                (ip_hash, now - OTP_IP_WINDOW),
            ).fetchone()[0]
            if phone_count >= OTP_PHONE_LIMIT or phone_day_count >= OTP_PHONE_DAILY_LIMIT or ip_count >= OTP_IP_LIMIT:
                conn.rollback()
                raise AuthError(429, "Слишком много запросов. Попробуйте позже.")
            conn.execute(
                "INSERT INTO app_auth_events(phone_hash, ip_hash, created_at) VALUES (?, ?, ?)",
                (phone_hash, ip_hash, now),
            )
            code = f"{secrets.randbelow(1_000_000):06d}"
            conn.execute(
                "INSERT OR REPLACE INTO app_auth_codes(phone, code_hash, expires_at, attempts, first_name) "
                "VALUES (?, ?, ?, 0, '')",
                (phone, self._digest("otp", phone + ":" + code), now + OTP_TTL),
            )
            conn.commit()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=SMS_TIMEOUT)) as session:
                async with session.post(
                    "https://sms.ru/sms/send",
                    data={
                        "api_id": self.sms_api_id,
                        "to": sms_phone,
                        "msg": f"Код входа Relax Gas: {code}. Никому не сообщайте этот код.",
                        "json": "1",
                    },
                ) as response:
                    response.raise_for_status()
                    result = await response.json(content_type=None)
            if result.get("status") != "OK":
                raise RuntimeError(f"SMS provider returned status {result.get('status')!r}")
        except Exception as exc:
            logger.warning("SMS OTP delivery failed: %s", type(exc).__name__)
            with self.db.connection() as conn:
                conn.execute("DELETE FROM app_auth_codes WHERE phone = ?", (phone,))
            raise AuthError(502, "Не удалось отправить SMS. Попробуйте позже.") from exc
        return {"ok": True, "expires_in": OTP_TTL}

    def _account_user_id(self, conn, phone: str, first_name: str, now: int) -> int:
        existing = conn.execute("SELECT user_id FROM app_accounts WHERE phone = ?", (phone,)).fetchone()
        if existing:
            return int(existing[0])

        if phone == self.admin_phone:
            user_id, display_name = self.admin_id, "Администратор"
        else:
            courier_rows = conn.execute(
                "SELECT id, name, telegram_id, phone FROM couriers WHERE is_active = 1 AND phone IS NOT NULL"
            ).fetchall()
            matched = None
            for courier in courier_rows:
                try:
                    candidate, _ = normalize_phone(courier["phone"])
                except AuthError:
                    continue
                if candidate == phone:
                    matched = courier
                    break
            if matched:
                display_name = str(matched["name"] or first_name)
                user_id = int(matched["telegram_id"]) if matched["telegram_id"] else 0
                if not user_id:
                    while True:
                        candidate_id = secrets.randbelow((1 << 52) - 1) + 1
                        occupied = conn.execute("SELECT 1 FROM users WHERE telegram_id = ?", (candidate_id,)).fetchone()
                        if not occupied:
                            user_id = candidate_id
                            break
                    conn.execute("UPDATE couriers SET telegram_id = ? WHERE id = ? AND telegram_id IS NULL",
                                 (user_id, int(matched["id"])))
            else:
                display_name = first_name.strip()
                if len(display_name) < 2:
                    raise AuthError(400, "Введите имя для регистрации.")
                while True:
                    user_id = secrets.randbelow((1 << 52) - 1) + 1
                    occupied = conn.execute("SELECT 1 FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
                    if not occupied:
                        break

        conn.execute(
            "INSERT OR IGNORE INTO users(telegram_id, username, first_name, last_name, created_at, updated_at) "
            "VALUES (?, NULL, ?, NULL, datetime('now'), datetime('now'))",
            (user_id, display_name),
        )
        conn.execute("INSERT INTO app_accounts(phone, user_id, created_at) VALUES (?, ?, ?)",
                     (phone, user_id, now))
        return user_id

    def verify_code(self, phone_raw: Any, code_raw: Any, first_name_raw: Any = "") -> AuthResult:
        phone, _ = normalize_phone(phone_raw)
        code = re.sub(r"\D", "", str(code_raw or ""))
        if len(code) != 6:
            raise AuthError(400, "Введите шестизначный код из SMS.")
        first_name = str(first_name_raw or "").strip()[:64]
        now = int(time.time())
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM app_auth_codes WHERE phone = ?", (phone,)).fetchone()
            if not row or int(row["expires_at"]) < now:
                conn.execute("DELETE FROM app_auth_codes WHERE phone = ?", (phone,))
                conn.commit()
                raise AuthError(400, "Код истёк. Запросите новый код.")
            if int(row["attempts"]) >= OTP_MAX_ATTEMPTS:
                conn.execute("DELETE FROM app_auth_codes WHERE phone = ?", (phone,))
                conn.commit()
                raise AuthError(429, "Превышено число попыток. Запросите новый код.")
            supplied = self._digest("otp", phone + ":" + code)
            if not hmac.compare_digest(supplied, str(row["code_hash"])):
                conn.execute("UPDATE app_auth_codes SET attempts = attempts + 1 WHERE phone = ?", (phone,))
                conn.commit()
                raise AuthError(400, "Неверный код.")
            user_id = self._account_user_id(conn, phone, first_name, now)
            conn.execute("DELETE FROM app_auth_codes WHERE phone = ?", (phone,))
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            conn.execute(
                "DELETE FROM app_sessions WHERE user_id = ? AND token_hash NOT IN "
                "(SELECT token_hash FROM app_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 4)",
                (user_id, user_id),
            )
            conn.execute("INSERT INTO app_sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                         (token_hash, user_id, now + SESSION_TTL, now))
            profile = conn.execute("SELECT first_name, last_name, username FROM users WHERE telegram_id = ?",
                                   (user_id,)).fetchone()
            conn.commit()
        user = TelegramUser(
            id=user_id,
            first_name=str(profile["first_name"] or first_name or "Покупатель"),
            last_name=profile["last_name"],
            username=profile["username"],
        )
        return AuthResult(token, user)

    def user_for_token(self, token: str) -> TelegramUser | None:
        if not token or len(token) > 256:
            return None
        now = int(time.time())
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT u.telegram_id, u.first_name, u.last_name, u.username "
                "FROM app_sessions s JOIN users u ON u.telegram_id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at >= ?",
                (digest, now),
            ).fetchone()
            if not row:
                return None
        return TelegramUser(int(row["telegram_id"]), str(row["first_name"] or "Покупатель"),
                            row["last_name"], row["username"])

    def logout(self, token: str) -> None:
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            with self.db.connection() as conn:
                conn.execute("DELETE FROM app_sessions WHERE token_hash = ?", (digest,))
