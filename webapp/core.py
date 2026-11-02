Warning: truncated output (original token count: 65991)
Total output lines: 4015

"""Логика API Mini App — без aiogram и aiohttp.

Работает с тем же db.Database, что и бот (одна SQLite-база), и повторяет
правила классического бота из main.py: кто что видит, какие статусы
куда можно перевести, когда заказ уходит курьерам. Всё, что связано с
отправкой сообщений в Telegram, делегируется объекту Bridge:
- в рабочем режиме это webapp/bridge.py (вызывает функции из main.py);
- локально это LogBridge (dev_server.py) — просто пишет события в лог.

Благодаря этому ядро запускается и тестируется без доступа к Telegram.
"""
from __future__ import annotations

import asyncio
import json
import base64
import binascii
import hashlib
import logging
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, Awaitable, Callable, Iterable, Protocol

from . import crypto
from .geodb import ATTRIBUTION as ADDRESS_ATTRIBUTION, text_is_moscow
from .auth import TelegramUser
from db import default_partner_reward, is_balloon, partner_reward_for  # баллон по названию товара
from .regions import REGION_LABELS, REGIONS

logger = logging.getLogger(__name__)

# Те же константы, что в main.py (продублированы, чтобы ядро не тянуло aiogram).
STATUS_LABELS = {
    "new": "Активен",
    "accepted": "В работе",
    "in_transit": "В пути",
    "delivered": "Завершён",
    "cancelled": "Отменён",
    "not_delivered": "Не доставлен",
    "disputed": "Спорный",
    "disputed_closed": "Спорный — закрыт",
}
# Покупатель видит спорный заказ мягче — без слова «спорный».
CUSTOMER_STATUS_LABELS = {
    **STATUS_LABELS,
    "new": "Ищем курьера",  # заказ оформлен, курьер ещё не взял
    "disputed": "Ждём связи с вами",
    "disputed_closed": "Закрыт",
}
INDIVIDUAL_CUSTOMER_NOTE = (
    "Это индивидуальный заказ: доставка за пределы Москвы обсуждается отдельно. "
    "Администратор свяжется с вами, чтобы согласовать условия."
)
DISPUTED_CUSTOMER_NOTE = "Вы долго не выходите на связь — администрация скоро с вами свяжется."
CUSTOMER_ACTIVE_STATUSES = {"new", "accepted", "in_transit", "not_delivered", "disputed"}
CUSTOMER_DONE_STATUSES = {"delivered", "cancelled", "disputed_closed"}
HISTORY_PAGE = 30
HISTORY_FILTERS: dict[str, tuple[str, ...] | None] = {
    "delivered": ("delivered",),
    "cancelled": ("cancelled",),
    "not_delivered": ("not_delivered",),
    "disputed": ("disputed", "disputed_closed"),
    "all": None,
}
PAYMENT_METHODS = ("cash_on_delivery", "sbp", "crypto")
PAYMENT_METHOD_LABELS = {
    "cash_on_delivery": "💵 Наличными при получении",
    "sbp": "📱 По СБП при курьере",
    "crypto": "💎 Криптовалютой при курьере",
}
ADDRESS_FIELD_NAMES = {"house": "Дом", "entrance": "Подъезд", "apartment": "Квартира", "floor": "Этаж", "intercom": "Домофон"}
# Объявление на главном экране магазина — до стольких символов.
MAX_ANNOUNCEMENT = 500
# Приветствие в шапке магазина (под «Привет, …!»), меняет админ.
DEFAULT_HOME_GREETING = "Закажите газ для начала или продолжения хорошего вечера."
MAX_GREETING = 300
# Фото оплаты курьер отправляет в группу (лог) только для этих способов.
PHOTO_TO_GROUP_METHODS = ("sbp", "crypto")
# Надписи кнопок выбора оплаты при оформлении.
PAYMENT_METHOD_BUTTONS = {
    "cash_on_delivery": "💵 Оплатить наличными",
    "sbp": "📱 Оплатить по СБП",
    "crypto": "💎 Оплатить криптовалютой",
}
BLOCKED_TEXT = "Оформление заказов для вас недоступно. Свяжитесь с администратором."
PAY_ON_DELIVERY_WARNING = (
    "⚠️ Внимание! При оплате через СБП или криптовалютой перевод нужно сделать "
    "в присутствии курьера при получении баллона — курьер покажет QR-код для оплаты."
)
ADMIN_STATUS_TARGETS = {"new", "in_transit", "delivered", "cancelled"}
QUOTE_TTL = 15 * 60  # столько же, сколько USDT_RATE_MAX_AGE в main.py
COURIER_ARRIVED_COOLDOWN = 60  # как в main.py
MAX_CUSTOMER_NOTE = 500
# Пароль для необратимых действий задаётся только в секретах сервера.
DANGER_PASSWORD = os.getenv("DANGER_PASSWORD", "")
# Пароль кнопки «Выгрузка в Excel».
EXPORT_PASSWORD = DANGER_PASSWORD
EXPORT_TTL = 10 * 60  # ссылка на скачивание файла живёт 10 минут
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ADMIN_IMAGE_BYTES = 8 * 1024 * 1024
MAX_LIFT_PRICE = 100_000

MAX_PROOF_BYTES = 8 * 1024 * 1024
MAX_PRODUCT_PHOTO_CHARS = 1_500_000  # data: URL, ~1.1 МБ картинки
MAX_BG_IMAGE_CHARS = 2_000_000
def _plain_name(name: Any) -> str:
    """Имя без «(@логин)»: курьер и покупатель не должны видеть Telegram друг друга (в старых заказах логин был в имени)."""
    return re.sub(r"\s*\(@[^)]*\)", "", str(name or "")).strip()


HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$")

THEME_ENUMS = {
    "button_style": {"rounded", "pill", "square"},
    "button_fill": {"lux", "solid", "outline", "glass"},
    "button_animation": {"premium", "bounce", "none"},
}
MENU_KEYS = ["catalog", "cart", "orders_active", "orders_history", "call_admin"]


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}  # дополнительные поля ответа (например, code для окон интерфейса)


class Bridge(Protocol):
    """Всё, что требует Telegram. Реализации: webapp/bridge.py и LogBridge."""

    async def dispatch_new_order(self, order_id: int) -> None: ...
    async def customer_cancelled(self, order_id: int) -> None: ...
    async def customer_received(self, order_id: int) -> None: ...
    async def courier_delivered(self, order_id: int, prev_status: str) -> None: ...
    async def courier_not_delivered(self, order_id: int, reason: str | None) -> None: ...
    async def courier_released(self, order_id: int, prev: dict[str, Any]) -> None: ...
    async def order_disputed(self, order_id: int) -> None: ...
    async def dispute_returned(self, order_id: int, prev: dict[str, Any]) -> None: ...
    async def dispute_closed(self, order_id: int, prev: dict[str, Any]) -> None: ...
    async def courier_location(self, order_id: int, lat: float, lon: float, first: bool) -> None: ...
    async def admin_status(self, order_id: int, prev: dict[str, Any] | None, status: str) -> None: ...
    async def admin_assigned(self, order_id: int, prev: dict[str, Any] | None) -> None: ...
    async def call_admin(self, user: TelegramUser, text: str) -> None: ...
    async def support_message(self, user: TelegramUser, text: str | None, has_photo: bool) -> None: ...
    async def support_reply(self, user_id: int) -> None: ...
    async def order_chat(self, order_id: int, to_courier: bool, text: str | None, has_photo: bool) -> None: ...
    async def courier_arrived(self, order_id: int) -> None: ...
    async def get_crypto_rates(self, codes: list[str]) -> dict[str, float]: ...
    async def get_crypto_rates_now(self, codes: list[str]) -> tuple[dict[str, float], float | None]: ...
    async def send_payment_photo(self, order_id: int, data: bytes, mime: str, caption: str) -> str: ...
    async def upload_image(self, data: bytes, mime: str, caption: str) -> str: ...
    async def test_log_group(self) -> bool: ...
    async def send_document(self, chat_id: int, data: bytes, filename: str, caption: str) -> None: ...
    async def customer_review(self, order_id: int, rating: int | None, comment: str | None) -> None: ...
    async def customer_blocked(self, user_id: int, name: str, blocked: bool, reason: str | None) -> None: ...
    async def referral_request(self, user_id: int, name: str, contact: str, is_courier: bool, kind: str,
                               comment: str | None = None) -> None: ...
    async def referral_declined(self, name: str, kind: str, mode: str) -> None: ...
    async def partner_link(self, code: str) -> str: ...
    async def partner_event(self, text: str) -> None: ...
    async def bonus_event(self, text: str) -> None: ...
    async def bonus_gift(self, recipient_id: int, text: str) -> None: ...
    async def bot_link(self) -> str: ...
    async def courier_live_instruction(self, telegram_id: int) -> None: ...
    async def admin_alert(self, text: str) -> None: ...
    async def courier_message(self, telegram_id: int, text: str) -> None: ...
    async def order_changed(self, order_id: int, text: str) -> None: ...
    async def upload_payment_proof(self, data: bytes, mime: str, caption: str) -> tuple[str, Any]: ...
    async def finalize_payment_proof(self, ref: Any, caption: str) -> None: ...
    async def get_usdt_rate(self) -> float | None: ...
    async def dispatch_link(self, telegram_id: int, url: str) -> None: ...
    async def dispatch_login(self, telegram_id: int, session: dict[str, Any]) -> None: ...


# ---------- комментарий к заказу и «доставка ко времени» (25.10.2026)
ORDER_COMMENT_MAX = 300
REFERRAL_COMMENT_MAX = 300  # комментарий к заявке на партнёрскую программу (обязателен)
MSK = timezone(timedelta(hours=3))
SLOT_STEP_MIN = 15
SLOT_LEAD_MIN = 120  # «ко времени» — не раньше чем через 2 часа (решение владельца 25.10.2026)


def delivery_time_slots(now: datetime | None = None) -> list[str]:
    """Время «ко времени» на сегодня по Москве: шаг 15 минут, не раньше чем через 2 часа."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    first = now + timedelta(minutes=SLOT_LEAD_MIN)
    minutes = first.hour * 60 + first.minute + (1 if first.second or first.microsecond else 0)
    minutes = -(-minutes // SLOT_STEP_MIN) * SLOT_STEP_MIN  # вверх до 15 минут
    if first.date() != now.date():
        return []
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(minutes, 24 * 60, SLOT_STEP_MIN)]


def deliver_at_from_slot(value: Any, now: datetime | None = None) -> str | None:
    """«18:30» → UTC ISO сегодняшнего дня по Москве; None — время недоступно."""
    slot = str(value or "").strip()
    if slot not in delivery_time_slots(now):
        return None
    now_msk = (now or datetime.now(timezone.utc)).astimezone(MSK)
    hh, mm = (int(x) for x in slot.split(":"))
    at = now_msk.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def msk_datetime(iso: str | None) -> str:
    """«2026-10-25T09:21:20+00:00» → «25.10.2026 12:21» (по Москве, без секунд)."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso)).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(iso)


def msk_hhmm(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%H:%M")
    except ValueError:
        return None


@dataclass
class Quote:
    method: str
    total: int
    usdt_amount: Decimal | None
    usdt_rate: float | None
    floors: int = 0
    at: float = field(default_factory=time.time)


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


def rub_to_usdt(rub: int, rate: float) -> Decimal:
    """Как в main.py: округление ВВЕРХ до 0.01, чтобы продавец не получил меньше."""
    return (Decimal(rub) / Decimal(str(rate))).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _short(value: Any) -> str:
    """Короткий отпечаток file_id — для адреса картинки: новая картинка → новый адрес."""
    return hashlib.sha1(str(value or "").encode()).hexdigest()[:8]


def _row(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _int(value: Any, name: str, low: int | None = None, high: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"Поле «{name}» должно быть целым числом.")
    if low is not None and result < low:
        raise ApiError(400, f"Поле «{name}» должно быть не меньше {low}.")
    if high is not None and result > high:
        raise ApiError(400, f"Поле «{name}» должно быть не больше {high}.")
    return result


# Уточнения к адресу на экране оформления (ключ, подпись в адресе для курьера).
# Поля адреса после «Город, улица»: (ключ, подпись для курьера, только цифры?).
ADDRESS_EXTRA_FIELDS = (
    ("house", "д.", True), ("building", "корп./стр.", False), ("entrance", "подъезд", True),
    ("apartment", "кв.", True), ("floor", "этаж", True), ("intercom", "домофон", True),
)


def compose_address(parts: dict[str, Any]) -> str:
    """«Лесная улица, Москва, д. 7, корп./стр. 2, подъезд 2, кв. 12, этаж 7, домофон 12К» — одной строкой для курьера."""
    out = [str(parts["line"]).strip()]
    for key, label, _digits in ADDRESS_EXTRA_FIELDS:
        value = str(parts.get(key) or "").strip()
        if value:
            out.append(f"{label} {value}")
    return ", ".join(out)


def normalize_phone(raw: Any) -> str:
    """Телефон покупателя строго «8 916 123 45 67»: 11 цифр, первая 8.
    «+7 916 …» и «7916…» переводим в 8; иначе — ошибка с подсказкой формата."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "7":
        digits = "8" + digits[1:]
    if len(digits) != 11 or digits[0] != "8":
        raise ApiError(400, "Телефон — 11 цифр в формате 8 916 123 45 67.")
    return f"{digits[0]} {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"


def order_number(user_id: Any, order_id: Any) -> str:
    """«622221060_24»: ID покупателя, «_» и порядковый номер заказа (общий для магазина)."""
    return f"{user_id}_{order_id}" if user_id not in (None, "") else str(order_id)


def _safe_phone(raw: Any) -> str:
    """Сохранённый телефон в новом формате; если не подходит — пусто (покупатель введёт заново)."""
    try:
        return normalize_phone(raw) if raw else ""
    except ApiError:
        return ""


def _text(value: Any, name: str, min_len: int = 0, max_len: int = 1000) -> str:
    text = str(value or "").strip()
    if len(text) < min_len:
        raise ApiError(400, f"Заполните поле «{name}».")
    if len(text) > max_len:
        raise ApiError(400, f"Поле «{name}» слишком длинное (максимум {max_len} символов).")
    return text


def _optional_text(value: Any, max_len: int = 1000) -> str | None:
    text = str(value or "").strip()
    return text[:max_len] or None


def decode_data_url(value: str, max_bytes: int) -> tuple[bytes, str]:
    match = DATA_URL.match(value or "")
    if not match:
        raise ApiError(400, "Нужна картинка (PNG, JPEG, WEBP или GIF).")
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError):
        raise ApiError(400, "Картинка повреждена.")
    if len(raw) > max_bytes:
        raise ApiError(413, f"Картинка слишком большая (максимум {max_bytes // (1024 * 1024)} МБ).")
    return raw, match.group(1)


Handler = Callable[..., Awaitable[dict[str, Any]]]


class Api:
    def __init__(self, db, admin_id: "int | Iterable[int]", bridge: Bridge) -> None:
        self.db = db
        self._dev_fails: dict[int, list[float]] = {}  # неверные пароли «Разработчика»: {id админа: [время]}
        # ADMIN_ID может содержать до 20 ID через запятую — принимаем и одно число, и набор.
        self.admin_ids: frozenset[int] = (
            frozenset({int(admin_id)}) if isinstance(admin_id, int) else frozenset(int(a) for a in admin_id)
        )
        self.admin_id = next(iter(admin_id)) if not isinstance(admin_id, int) else admin_id
        self.bridge = bridge
        self.geo = None  # своя база адресов OSM (webapp.geodb.AddressDB) — задаёт main/dev_server
        self.routing = None  # маршруты по дорогам (webapp.routing.RouteService) — задаёт main
        # Уведомления в Telegram — в фоне (см. _safe). dev_server/тесты ставят False.
        self.background = True
        self._tasks: set[asyncio.Task] = set()
        self._order_chain: dict[int, asyncio.Task] = {}
        self._known_users: dict[int, tuple] = {}  # кто уже записан в users в этом запуске
        self.quotes: dict[int, Quote] = {}
        self.exports: dict[str, tuple[bytes, str, float]] = {}  # токен → (файл, имя, когда истекает)
        self.routes: list[tuple[str, re.Pattern[str], Handler, str]] = []
        r = self._route
        # role: any | courier | admin
        r("GET", r"/api/me", self.me)
        r("GET", r"/api/badges", self.badges)
        r("GET", r"/api/catalog", self.catalog)
        r("GET", r"/api/cart", self.cart)
        r("POST", r"/api/cart", self.cart_set)
        r("DELETE", r"/api/cart", self.cart_clear)
        r("GET", r"/api/checkout", self.checkout_info)
        r("GET", r"/api/returns", self.return_options)
        r("GET", r"/api/delivery/quote", self.delivery_quote)
        r("GET", r"/api/admin/settings", self.admin_settings, "admin")
        r("GET", r"/api/admin/payment-qr", self.admin_payment_qr_list, "admin")
        r("POST", r"/api/admin/payment-qr", self.admin_payment_qr_add, "admin")
        r("DELETE", r"/api/admin/payment-qr/(\d+)", self.admin_payment_qr_remove, "admin")
        r("PUT", r"/api/admin/catalog-photo", self.admin_catalog_photo, "admin")
        r("PUT", r"/api/admin/payment-qr/sbp", lambda u, b, q: self.admin_payment_qr(u, b, q, "sbp"), "admin")
        r("PUT", r"/api/admin/payment-qr/crypto", lambda u, b, q: self.admin_payment_qr(u, b, q, "crypto"), "admin")
        r("GET", r"/api/admin/export", self.admin_export_info, "admin")
        r("POST", r"/api/admin/export", self.admin_export, "admin")
        r("POST", r"/api/admin/test-log", self.admin_test_log, "admin")
        r("POST", r"/api/admin/wipe-products", self.admin_wipe_products, "admin")
        r("POST", r"/api/admin/wipe-orders", self.admin_wipe_orders, "admin")
        r("GET", r"/api/admin/customers", self.admin_customers, "admin")
        r("PUT", r"/api/admin/customers/(\d+)/rating", self.admin_customer_rating, "admin")
        r("GET", r"/api/staff/customers/(\d+)/reviews", self.staff_customer_reviews, "staff")
        r("PUT", r"/api/admin/customers/(\d+)/block", self.admin_customer_block, "admin")
        r("PUT", r"/api/admin/customers/(\d+)/bonus-percent", self.admin_customer_bonus_percent, "admin")
        r("POST", r"/api/admin/customers/(\d+)/bonus", self.admin_customer_bonus_adjust, "admin")
        r("GET", r"/api/admin/customers/(\d+)/bonus", self.admin_customer_bonus_history, "admin")
        r("PUT", r"/api/admin/settings", self.admin_settings_update, "admin")
        r("PUT", r"/api/admin/outside-delivery", self.admin_outside_update, "admin")
        r("PUT", r"/api/admin/payment-switch", self.admin_payment_switch, "admin")
        r("PUT", r"/api/admin/announcement", self.admin_announcement, "admin")
        r("PUT", r"/api/admin/home-greeting", self.admin_home_greeting, "admin")
        r("GET", r"/api/admin/address-check", self.admin_address_check, "admin")
        r("POST", r"/api/admin/address-update", self.admin_address_update, "admin")
        r("POST", r"/api/admin/address-rebuild", self.admin_address_rebuild, "admin")
        r("POST", r"/api/orders", self.create_order)
        r("GET", r"/api/orders", self.my_orders)
        r("POST", r"/api/orders/(\d+)/cancel", self.cancel_order)
        r("POST", r"/api/orders/(\d+)/received", self.order_received)
        r("POST", r"/api/orders/(\d+)/rate", self.rate_order)
        r("POST", r"/api/call-admin", self.call_admin)
        # Чат с администрацией (25.10.2026)
        r("GET", r"/api/chat", self.chat_get)
        r("POST", r"/api/chat", self.chat_send)
        r("GET", r"/api/chat/photo/(\d+)", self.chat_photo)
        r("GET", r"/api/admin/chats", self.admin_chats, "admin")
        r("GET", r"/api/admin/chats/(\d+)", self.admin_chat_get, "admin")
        r("POST", r"/api/admin/chats/(\d+)", self.admin_chat_send, "admin")
        r("DELETE", r"/api/admin/chats/(\d+)", self.admin_chat_delete, "admin")
        # Чат курьера и покупателя внутри заказа
        r("GET", r"/api/orders/(\d+)/chat", self.order_chat_get)
        r("POST", r"/api/orders/(\d+)/chat", self.order_chat_send)
        r("GET", r"/api/order-chat/photo/(\d+)", self.order_chat_photo)
        r("GET", r"/api/admin/orders/(\d+)/chat", self.admin_order_chat, "admin")
        r("GET", r"/api/courier/orders", self.courier_orders, "courier")
        r("GET", r"/api/courier/history", self.courier_history, "courier")
        r("POST", r"/api/courier/orders/(\d+)/delivered", self.courier_delivered, "courier")
        # «❌ Заказ не доставлен» у курьера убран (19.10) — вместо него две кнопки:
        r("POST", r"/api/courier/orders/(\d+)/release", self.courier_release, "courier")
        r("POST", r"/api/courier/orders/(\d+)/no-contact", self.courier_no_contact, "courier")
        r("GET", r"/api/admin/disputes", self.admin_disputes, "admin")
        r("GET", r"/api/referral", self.referral)
        r("GET", r"/api/bonus", self.bonus)
        r("POST", r"/api/bonus/transfer", self.bonus_transfer)
        r("GET", r"/api/courier/shift", self.courier_shift, "courier")
        r("POST", r"/api/courier/shift", self.courier_shift_start, "courier")
        r("POST", r"/api/courier/shift/confirm", self.courier_shift_confirm, "courier")
        r("POST", r"/api/courier/shift/end", self.courier_shift_end, "courier")
        r("POST", r"/api/courier/shift/break", self.courier_shift_break, "courier")
        r("POST", r"/api/courier/shift/resume", self.courier_shift_resume, "courier")
        r("POST", r"/api/courier/shift/request/cancel", self.courier_shift_request_cancel, "courier")
        r("POST", r"/api/admin/couriers/(\d+)/shift", self.admin_shift_request, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/shift-end", self.admin_shift_end, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/break-seen", self.admin_break_seen, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/counter-reset", self.admin_counter_reset_request, "admin")
        r("POST", r"/api/admin/couriers/ask-all", self.admin_ask_all, "admin")
        r("POST", r"/api/courier/live", self.courier_live_request, "courier")
        r("GET", r"/api/admin/map", self.admin_map, "admin")
        r("POST", r"/api/courier/assigned/ack", self.courier_assigned_ack, "courier")
        r("POST", r"/api/courier/reload", self.courier_reload, "courier")
        r("POST", r"/api/courier/counters/reset", self.courier_counter_reset, "courier")
        r("POST", r"/api/courier/empties", self.courier_empties_set, "courier")
        # «🔄 Обмен» между курьерами (28.10.2026)
        r("GET", r"/api/courier/transfer", self.courier_transfer_get, "courier")
        r("POST", r"/api/courier/transfer", self.courier_transfer_create, "courier")
        r("POST", r"/api/courier/transfer/cancel", self.courier_transfer_cancel, "courier")
        r("POST", r"/api/admin/transfers/(\d+)", self.admin_transfer_decide, "admin")
        r("POST", r"/api/admin/orders/assign", self.admin_assign_many, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/reload", self.admin_reload_answer, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/counters/reset", self.admin_counter_reset, "admin")
        r("GET", r"/api/admin/couriers/(\d+)/shifts", self.admin_courier_shifts, "admin")
        r("GET", r"/api/admin/shifts/(\d+)", self.admin_shift_detail, "admin")
        r("PUT", r"/api/admin/courier-rate", self.admin_courier_rate, "admin")
        r("PUT", r"/api/admin/courier-rates", self.admin_courier_rates, "admin")
        r("GET", r"/api/admin/alerts", self.admin_alerts, "admin")
        r("POST", r"/api/admin/calls/(\d+)/resolve", self.admin_call_resolve, "admin")
        r("GET", r"/api/address/suggest", self.address_suggest)
        r("DELETE", r"/api/addresses/saved/(\d+)", self.delete_saved_address)
        r("GET", r"/api/address/geolocate", self.address_geolocate)
        r("PUT", r"/api/me/mode", self.set_mode)
        r("POST", r"/api/referral/apply", self.referral_apply)
        r("POST", r"/api/admin/referral/(\d+)/decline", self.admin_referral_decline, "admin")
        r("POST", r"/api/admin/referral/(\d+)/approve", self.admin_referral_approve, "admin")
        r("POST", r"/api/admin/partners/(\d+)/writeoff", self.admin_partner_writeoff, "admin")
        r("POST", r"/api/admin/partners/link-refresh", self.admin_partner_link_refresh, "admin")
        r("GET", r"/api/partner/clients", self.partner_clients)
        r("POST", r"/api/partner/link-ack", self.partner_link_ack)
        r("GET", r"/api/admin/admins", self.admin_admins, "admin")
        r("PUT", r"/api/admin/partners/(\d+)/rewards", self.admin_partner_rewards, "admin")
        r("POST", r"/api/admin/disputes/(\d+)/return", self.admin_dispute_return, "admin")
        r("POST", r"/api/admin/disputes/(\d+)/close", self.admin_dispute_close, "admin")
        r("POST", r"/api/courier/orders/(\d+)/location", self.courier_location, "courier")
        r("POST", r"/api/courier/orders/(\d+)/arrived", self.courier_arrived, "courier")
        r("POST", r"/api/courier/orders/(\d+)/prepay-taken", self.courier_prepay_taken, "courier")
        r("GET", r"/api/courier/payment-qr", self.courier_payment_qr, "courier")
        r("GET", r"/api/courier/orders/(\d+)/pay/(\d+)", self.courier_pay_screen, "courier")
        r("POST", r"/api/courier/orders/(\d+)/payment-confirm", self.courier_payment_confirm, "courier")
        r("POST", r"/api/courier/orders/(\d+)/customer-note", self.courier_customer_note, "courier")
        r("GET", r"/api/admin/orders", self.admin_orders, "admin")
        r("GET", r"/api/admin/history", self.admin_history, "admin")
        r("POST", r"/api/admin/history/delete", self.admin_history_delete, "admin")
        r("POST", r"/api/admin/orders/(\d+)/status", self.admin_status, "admin")
        r("POST", r"/api/admin/orders/(\d+)/assign", self.admin_assign, "admin")
        r("POST", r"/api/admin/orders/(\d+)/delivery-fee", self.admin_delivery_fee, "admin")
        r("POST", r"/api/admin/orders/(\d+)/delivery-toll", self.admin_delivery_toll, "admin")
        r("GET", r"/api/admin/orders/(\d+)/edit-options", self.admin_order_edit_options, "admin")
        r("POST", r"/api/admin/orders/(\d+)/edit", self.admin_order_edit, "admin")
        r("GET", r"/api/admin/products", self.admin_products, "admin")
        r("POST", r"/api/admin/products", self.admin_product_create, "admin")
        r("PUT", r"/api/admin/products/order", self.admin_products_order, "admin")
        r("PATCH", r"/api/admin/products/(\d+)", self.admin_product_update, "admin")
        r("POST", r"/api/admin/products/(\d+)/hide", self.admin_product_hide, "admin")
        r("POST", r"/api/admin/products/(\d+)/restore", self.admin_product_restore, "admin")
        r("DELETE", r"/api/admin/products/(\d+)", self.admin_product_purge, "admin")
        r("GET", r"/api/admin/couriers", self.admin_couriers, "admin")
        r("GET", r"/api/admin/courier-stats", self.admin_courier_stats, "admin")
        r("POST", r"/api/admin/couriers", self.admin_courier_create, "admin")
        r("POST", r"/api/admin/couriers/(\d+)/active", self.admin_courier_active, "admin")
        r("DELETE", r"/api/admin/couriers/(\d+)", self.admin_courier_delete, "admin")
        r("PUT", r"/api/admin/theme", self.admin_theme, "admin")
        r("POST", r"/api/admin/dev/unlock", self.admin_dev_unlock, "admin")
        # 🖥 Диспетчерская карта для ПК (/dispatch): вход по одноразовой ссылке — webapp/dispatch.py
        from .dispatch import DispatchAuth, install_routes

        self.public_url: str | None = None  # https://<домен> для ссылок; задаёт main (WEBAPP_URL) или сервер
        self.dispatch = DispatchAuth(db)
        install_routes(self)

    # ------------------------------------------------------------ routing
    def _route(self, method: str, pattern: str, handler: Handler, role: str = "any") -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), handler, role))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def courier_of(self, user_id: int):
        return self.db.get_active_courier_by_telegram_id(user_id)

    async def handle(
        self, method: str, path: str, user: TelegramUser, body: dict[str, Any], query: dict[str, str]
    ) -> dict[str, Any]:
        path_matched = False
        for route_method, pattern, handler, role in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            path_matched = True
            if route_method != method:
                continue
            if role == "admin" and not self.is_admin(user.id):
                raise ApiError(403, "Раздел доступен только администратору.")
            if role == "staff" and not (self.is_admin(user.id) or self.courier_of(user.id)):
                raise ApiError(403, "Доступно только курьерам и администратору.")
            if role == "courier" and not self.courier_of(user.id):
                raise ApiError(403, "Доступно только зарегистрированным активным курьерам.")
            # Как register_user_middleware в main.py: корзина и заказы ссылаются
            # на users(telegram_id), поэтому пользователь должен существовать.
            # Запись в базу — только для нового пользователя или если в Telegram
            # сменились имя/username (раньше — запись на КАЖДЫЙ запрос).
            known = (user.username, user.first_name, user.last_name)
            if self._known_users.get(user.id) != known:
                self.db.upsert_user(user.id, user.username, user.first_name, user.last_name)
                self._known_users[user.id] = known
            args = [int(g) for g in match.groups()]
            return await handler(user, body, query, *args)
        if path_matched:
            raise ApiError(405, "Метод не поддерживается.")
        raise ApiError(404, "Не найдено.")

    async def _safe(self, coro, what: str, order_id: int | None = None) -> None:
        """Уведомления в Telegram — best-effort, как в боте: их сбой не ломает действие.

        Кнопка в приложении НЕ ждёт рассылку: ответ уходит сразу, а сообщения
        (курьерам, админу, в канал, покупателю) отправляются в фоне. Для одного
        заказа уведомления идут строго по очереди — в том же порядке, что и
        раньше. self.background = False — старое поведение (ждать), для dev/тестов."""
        if not self.background:
            await self._run_notification(coro, what)
            return
        prev = self._order_chain.get(order_id) if order_id is not None else None
        task = asyncio.create_task(self._run_notification(coro, what, prev))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if order_id is not None:
            self._order_chain[order_id] = task
            task.add_done_callback(
                lambda t, k=order_id: self._order_chain.pop(k, None) if self._order_chain.get(k) is t else None
            )

    async def _run_notification(self, coro, what: str, prev: "asyncio.Task | None" = None) -> None:
        if prev is not None:
            try:
                await prev
            except BaseException:
                pass
        try:
            await coro
        except Exception:
            logger.exception("Mini App: не удалось выполнить %s", what)

    async def flush_notifications(self) -> None:
        """Дождаться всех фоновых уведомлений (тесты, остановка сервера)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------ serializers
    def theme(self) -> dict[str, Any]:
        return self.db.get_theme()

    def available_payment_methods(self) -> list[dict[str, str]]:
        methods = [m for m in PAYMENT_METHODS if self._method_available(m)]
        return [{"id": m, "label": PAYMENT_METHOD_BUTTONS[m]} for m in methods]

    @staticmethod
    def product_json(row, admin: bool = False) -> dict[str, Any]:
        p = _row(row)
        gift = bool(p.get("bonus_product_id")) and bool(p.get("bonus_text"))
        photo = p.get("webapp_photo_data_url") or (
            f"/api/media/product/{p['id']}?v={p.get('updated_at', '')}" if p.get("photo_file_id") else None
        )
        data = {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description") or "",
            "price": int(p["price"]),
            "price_exchange": int(p["price_exchange"]) if p.get("price_exchange") is not None else None,
            # 28.10.2026: бонус — товар-подарок из каталога (без товара бонуса нет), описание и
            # к каким вариантам применяется.
            "bonus_text": p.get("bonus_text") if gift else None,
            "bonus_product_id": int(p["bonus_product_id"]) if gift else None,
            "bonus_qty": int(p.get("bonus_qty") or 1) if gift else 1,
            "bonus_desc": (p.get("bonus_desc") or "") if gift else "",
            "bonus_new": bool(p.get("bonus_new", 1)) if gift else False,
            "bonus_exchange": (bool(p.get("bonus_exchange", 1)) and p.get("price_exchange") is not None) if gift else False,
            "photo": photo,
            # «Возврат баллона»: сколько курьер отдаёт покупателю за пустой такой баллон (0 — не принимается).
            "return_price": int(p.get("return_price") or 0),
            "return_points": int(p.get("return_points") or 0),
        }
        if admin:
            data["is_balloon"] = is_balloon(p["name"])
            data["partner_reward"] = partner_reward_for(p["name"], p.get("partner_reward"))
            data["partner_reward_custom"] = p.get("partner_reward") is not None and data["is_balloon"]
            data["partner_reward_default"] = default_partner_reward(p["name"])
            data["is_active"] = bool(p.get("is_active"))
            data["has_bot_photo"] = bool(p.get("photo_file_id"))
            data["has_app_photo"] = bool(p.get("webapp_photo_data_url"))
        return data

    def orders_json(self, rows, view: str) -> list[dict[str, Any]]:
        """Список заказов: товары, отзывы о покупателях и QR оплаты грузятся из
        базы пачкой (3 запроса на весь список), а не по запросу на каждый заказ."""
        rows = [r for r in rows if r]
        if not rows:
            return []
        pre = {
            "items": self.db.get_order_items_bulk([int(r["id"]) for r in rows]),
            "reviews": (
                self.db.get_customer_reviews_bulk([int(r["user_id"]) for r in rows])
                if view != "customer" else {}
            ),
            "blocked": (
                self.db.get_blocked_user_ids([int(r["user_id"]) for r in rows]) if view == "admin" else set()
            ),
            "qr": self.db.get_payment_qr_codes_by_ids(
                [int(r["payment_qr_id"]) for r in rows if "payment_qr_id" in r.keys() and r["payment_qr_id"]]
            ),
        }
        return [self.order_json(r, view, pre) for r in rows]

    def order_json(self, row, view: str, pre: dict[str, Any] | None = None) -> dict[str, Any]:
        """view: customer | courier | admin — как order_text(include_contacts) в боте."""
        o = _row(row)
        order_items = pre["items"].get(int(o["id"]), []) if pre else self.db.get_order_items(int(o["id"]))
        is_return = (o.get("kind") or "order") == "return"
        items = [
            {
                "product_id": int(i["product_id"]) if "product_id" in i.keys() and i["product_id"] is not None else None,
                "name": i["product_name"],
                "variant": "return" if is_return else i["variant"],
                "quantity": int(i["quantity"]),
                "unit_price": int(i["unit_price"]),
                "bonus_text": i["bonus_text"] if "bonus_text" in i.keys() else None,
                "gift": bool(i["is_gift"]) if "is_gift" in i.keys() else False,
            }
            for i in order_items
        ]
        method = o.get("payment_method") or "cash_on_delivery"
        pay_on_delivery = bool(o.get("pay_on_delivery"))
        payment_confirmed = bool(o.get("payment_confirmed_at"))
        paid = method == "cash_on_delivery" or bool(o.get("payment_proof_file_id")) or payment_confirmed
        data: dict[str, Any] = {
            "id": o["id"],
            # Номер для обращений: «Telegram ID покупателя _ порядковый номер заказа».
            "number": order_number(o.get("user_id"), o["id"]),
            "status": o["status"],
            "status_label": (CUSTOMER_STATUS_LABELS if view == "customer" else STATUS_LABELS).get(o["status"], o["status"]),
            "items": items,
            "total": int(o["total"]),
            "bonus_spent": int(o.get("bonus_spent") or 0),
            "points_spent": int(o.get("points_spent") or 0),
            "delivery_fee": int(o.get("delivery_fee") or 0),
            "delivery_km": o.get("delivery_km"),
            # Предоплата за доставку (30.10.2026): отдаётся курьеру, в total не входит.
            "prepay_amount": int(o.get("prepay_amount") or 0),
            "prepay_taken": bool(o.get("prepay_taken_at")),
            # Платная дорога — уже входит в delivery_fee (и в total).
            "delivery_toll": int(o.get("delivery_toll") or 0),
            # Индивидуальный заказ (доставка не по Москве): ждёт администратора.
            "individual": int(o.get("individual") or 0) == 1,
            "individual_any": int(o.get("individual") or 0) > 0,
            "region": o.get("region"),
            "region_label": REGION_LABELS.get(o.get("region") or ""),
            "payment_method": method,
            "payment_label": PAYMENT_METHOD_LABELS.get(method, method),
            "usdt_amount": o.get("usdt_amount"),
            "usdt_rate": o.get("usdt_rate"),
            "has_exchange": any(i["variant"] == "exchange" for i in items),
            "floor_lift": None,
            "pay_on_delivery": pay_on_delivery,
            "payment_confirmed": payment_confirmed,
            "courier_arrived_at": o.get("courier_arrived_at"),
            # Комментарий покупателя и «доставка ко времени» (время — по Москве).
            "comment": o.get("customer_comment"),
            "deliver_at": o.get("deliver_at"),
            "deliver_time": msk_hhmm(o.get("deliver_at")),
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
            "courier": None,
            # «Возврат баллона»: курьер забирает пустые баллоны и отдаёт покупателю return_payout ₽.
            "kind": o.get("kind") or "order",
            "is_return": (o.get("kind") or "order") == "return",
            "return_payout": int(o.get("return_payout") or 0),
            "returns": ([{"product_id": r["product_id"], "name": r["product_name"], "quantity": int(r["quantity"]),
                          "unit_price": int(r["unit_price"])} for r in self.db.get_order_returns(int(o["id"]))]
                        if not is_return and (int(o.get("return_payout") or 0) or int(o.get("return_points") or 0)) else []),
            "return_mode": o.get("return_mode") or "cash",
            "return_points": int(o.get("return_points") or 0),
            "descent": ({"count": int(o.get("descent_count") or 0), "price": int(o.get("descent_price") or 0),
                         "total": int(o.get("descent_count") or 0) * int(o.get("descent_price") or 0)}
                        if int(o.get("descent_count") or 0) else None),
        }
        data["payment_qr"] = None
        if pay_on_delivery and o.get("payment_qr_id"):
            code = (
                pre["qr"].get(int(o["payment_qr_id"])) if pre
                else self.db.get_payment_qr_code_any(int(o["payment_qr_id"]))
            )
            if code:
                quote = crypto.loads(o.get("crypto_quotes")).get(code["currency"] or "") if code["kind"] == "crypto" else None
                data["payment_qr"] = {
                    "id": code["id"],
                    "kind": code["kind"],
                    "label": self.qr_label(code),
                    "currency": code["currency"],
                    "network": code["network"],
                    "active": bool(code["is_active"]),
                    "image": f"/api/media/payqr/{code['id']}?v={_short(code['file_id'])}",
                    "amount": quote["amount"] if quote else None,
                    "rate": quote["rate"] if quote else None,
                }
        floors = int(o.get("floor_lift_floors") or 0)
        if floors:
            price = int(o.get("floor_lift_price") or 0)
            data["floor_lift"] = {"needed": True, "floors": floors, "price": price, "total": floors * price}
        if o.get("courier_id") is not None and o.get("courier_name"):
            count = int(o.get("courier_rating_count") or 0)
            data["courier"] = {
                "id": o["courier_id"],
                "name": o.get("courier_name"),
                # 01.11.2026: покупатель и курьер не знают Telegram друг друга — логин только у админа.
                "username": ((o.get("courier_username") or "").lstrip("@") or None) if view == "admin" else None,
                "rating": round(int(o.get("courier_rating_sum") or 0) / count, 1) if count else None,
            }
        if o.get("courier_last_lat") is not None and o.get("courier_last_lon") is not None:
            data["courier_location"] = {"lat": o["courier_last_lat"], "lon": o["courier_last_lon"]}
        if o.get("location_lat") is not None and o.get("location_lon") is not None:
            data["customer_location"] = {"lat": o["location_lat"], "lon": o["location_lon"]}
        data["disputed_at"] = o.get("disputed_at")
        if view == "customer":
            if o["status"] == "disputed":
                data["status_note"] = DISPUTED_CUSTOMER_NOTE
            if data["individual"] and o["status"] == "new":
                data["status_label"] = "Ждёт администратора"
                data["status_note"] = INDIVIDUAL_CUSTOMER_NOTE
            data["can_cancel"] = o["status"] == "new" and int(o.get("prepay_amount") or 0) == 0
            # «Получил заказ» у покупателя убран: доставку закрывает только курьер.
            data["can_confirm"] = False
            data["address"] = o.get("delivery_address")
        else:
            data["customer"] = {
                "name": _plain_name(o.get("customer_name")),
                "phone": o.get("customer_phone"),
                "username": o.get("username") if view == "admin" else None,
            }
            data["address"] = o.get("delivery_address")
            data["paid"] = paid
            data["can_release"] = self._can_release(o)
            data["crypto_quotes"] = crypto.loads(o.get("crypto_quotes")) if pay_on_delivery else {}
            # Оценки и комментарии курьеров о покупателе — ТОЛЬКО админу и курьерам.
            data["customer_reviews"] = (
                pre["reviews"].get(int(o["user_id"])) if pre and int(o["user_id"]) in pre["reviews"]
                else self.db.get_customer_reviews(int(o["user_id"]))
            )
            if view == "admin":
                # Чёрный список видит только админ.
                data["customer"]["telegram_id"] = int(o["user_id"])
                data["customer"]["id"] = int(o["user_id"])
                data["customer"]["blocked"] = (
                    int(o["user_id"]) in pre["blocked"] if pre else self.db.is_user_blocked(int(o["user_id"]))
                )
            if data["courier"] is not None and view == "admin":
                data["courier"]["phone"] = o.get("courier_phone")
        return data

    # ------------------------------------------------------------ common
    async def me(self, user: TelegramUser, body, query) -> dict[str, Any]:
        courier = self.courier_of(user.id)
        cart = self.db.get_cart(user.id)
        return {
            "user": {"id": user.id, "name": user.display_name, "first_name": user.first_name},
            "roles": {"admin": self.is_admin(user.id), "courier": courier is not None},
            "theme": self.theme(),
            "cart_count": sum(int(i["quantity"]) for i in cart),
            "payment_methods": self.available_payment_methods(),
            "pending_rating": self._pending_rating(user.id),
            "mode": self._saved_mode(user.id),
            "partner": self._partner_short(user.id),
            "bonus": self._bonus_short(user.id),
            "announcement": self.db.get_announcement(),
            "greeting": self.db.get_shop_texts()["home_greeting"],
            # Кнопка «♻️ Возврат баллона» на главной — если хоть один баллон принимается на возврат.
            "returns_available": bool(self.db.return_products()),
        }

    def _bonus_short(self, user_id: int) -> dict[str, Any]:
        """Плашка «💰 Мои бонусы»: остаток, процент покупателя и порог заказа."""
        return {
            "balance": self.db.bonus_balance(user_id),
            "percent": self.db.bonus_percent_for(user_id),
            "min_items_total": self.db.BONUS_MIN_ITEMS_TOTAL,
            "transfer_fee_percent": self.db.BONUS_TRANSFER_FEE_PERCENT,
            "transfer_min": self.db.BONUS_TRANSFER_MIN,
        }

    @staticmethod
    def _bonus_rows(rows) -> list[dict[str, Any]]:
        return [
            {"amount": int(h["amount"]), "kind": h["kind"], "order_id": h.get("order_id"),
             "order_total": h.get("order_total"), "percent": h.get("percent"),
             "reason": h.get("reason"), "created_at": h["created_at"], "fee": h.get("fee"),
             "who": (" ".join(x for x in (h.get("cp_first_name"), h.get("cp_last_name")) if x)
                     + (f" (@{str(h['cp_username']).lstrip('@')})" if h.get("cp_username") else "")) or None
             if h.get("counterpart_id") else None}
            for h in rows
        ]

    def _log_admin(self, user, action: str, **kw: Any) -> None:
        """Записать действие администратора в журнал (лист «Администраторы» в выгрузке)."""
        try:
            self.db.log_admin_action(user.id, user.display_name, action, **kw)
        except Exception:  # журнал не должен ломать само действие
            logger.exception("Не удалось записать действие администратора")

    async def admin_admins(self, user, body, query) -> dict[str, Any]:
        """Список администраторов из ADMIN_ID: кто они (если уже заходили) и сколько действий."""
        counts: dict[int, int] = {}
        for a in self.db.get_admin_actions():
            counts[int(a["admin_id"])] = counts.get(int(a["admin_id"]), 0) + 1
        admins = []
        for aid in sorted(self.admin_ids):
            u = _row(self.db.get_user(aid))
            name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p)
            admins.append({"telegram_id": aid, "name": name or None, "username": u.get("username"),
                           "seen": bool(u), "last_seen": u.get("updated_at"), "actions": counts.get(aid, 0),
                           "me": aid == user.id})
        return {"admins": admins, "max": 20}  # как MAX_ADMINS в config.py

    async def bonus_transfer(self, user, body, query) -> dict[str, Any]:
        """«🎁 Отправить бонусы другу»: {"username": "@friend", "amount": 100} — с отправителя
        100, другу 100 − 10 % (вверх) = 90. {"preview": true} — только посчитать и найти друга."""
        username = str(body.get("username") or "").strip().lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
            raise ApiError(400, "Введите логин друга в Telegram, например @friend.")
        friend = self.db.find_user_by_username(username)
        if not friend:
            raise ApiError(404, f"@{username} ещё не пользовался магазином — пусть сначала откроет бота.")
        if int(friend["telegram_id"]) == user.id:
            raise ApiError(400, "Нельзя отправить бонусы самому себе.")
        if friend["is_blocked"]:
            raise ApiError(409, "Этому пользователю нельзя отправить бонусы.")
        if _row(self.db.get_user(user.id)).get("is_blocked"):
            raise ApiError(403, "Отправка бонусов недоступна.")
        minimum = self.db.BONUS_TRANSFER_MIN
        amount = _int(body.get("amount"), "бонусы", minimum, 100_000_000)
        balance = self.db.bonus_balance(user.id)
        friend_name = " ".join(p for p in (friend["first_name"], friend["last_name"]) if p) or f"@{username}"
        fee = self.db.bonus_transfer_fee(amount)
        if body.get("preview"):
            return {"friend": friend_name, "username": username, "sent": amount, "fee": fee,
                    "received": amount - fee, "balance": balance}
        try:
            res = self.db.transfer_bonus(user.id, int(friend["telegram_id"]), amount)
        except ValueError:
            raise ApiError(409, f"Не хватает бонусов: у вас {balance}.")
        sender = user.display_name
        await self._safe(self.bridge.bonus_gift(
            int(friend["telegram_id"]),
            f"🎁 {sender} отправил вам {res['received']} бонусов 💰. Ими можно оплатить часть следующего заказа."),
            "бонусы другу: уведомление")
        await self._safe(self.bridge.bonus_event(
            f"🎁 Перевод бонусов: {sender} → {friend_name} (@{username}): списано {res['sent']}, "
            f"комиссия {res['fee']}, получено {res['received']}"), "бонусы другу: канал")
        return {"friend": friend_name, "username": username, **res}

    async def bonus(self, user, body, query) -> dict[str, Any]:
        """Экран «💰 Мои бонусы»: остаток, условия и история."""
        return {**self._bonus_short(user.id), "history": self._bonus_rows(self.db.bonus_history(user.id))}

    def _partner_short(self, user_id: int) -> dict[str, Any] | None:
        """Плашка «⭐ Мои баллы» у одобренного партнёра."""
        if not self.db.get_partner(user_id):
            return None
        return {
            "points": self.db.partner_points_balance(user_id),
            # Красное окно «ссылка обновлена» — пока партнёр не нажмёт «Я разослал(а) ссылку».
            "link_notice": self.db.partner_link_notice(user_id),
        }

    def _saved_mode(self, user_id: int) -> str | None:
        """Роль, в которой человек входил в прошлый раз — если она у него ещё есть."""
        mode = _row(self.db.get_user(user_id)).get("app_mode")
        if mode == "admin" and not self.is_admin(user_id):
            return None
        if mode == "courier" and self.courier_of(user_id) is None:
            return None
        return mode if mode in ("admin", "courier", "customer") else None

    async def set_mode(self, user, body, query) -> dict[str, Any]:
        """Запомнить выбор «Как войти?» — следующий вход сразу в этой роли."""
        mode = body.get("mode")
        if mode not in ("admin", "courier", "customer"):
            raise ApiError(400, "Неизвестный режим.")
        if mode == "admin" and not self.is_admin(user.id):
            raise ApiError(403, "Раздел доступен только администратору.")
        if mode == "courier" and self.courier_of(user.id) is None:
            raise ApiError(403, "Доступно только зарегистрированным активным курьерам.")
        self.db.set_user_app_mode(user.id, mode)
        return {"mode": mode}

    def _pending_rating(self, user_id: int) -> dict[str, Any] | None:
        """Заказ, который курьер отметил доставленным, а покупатель ещё не
        оценил. Пока он есть, приложение показывает окно оценки, которое нельзя
        закрыть (после оценки — пропадает навсегда)."""
        row = self.db.get_pending_courier_rating(user_id)
        if not row:
            return None
        return {"order_id": int(row["id"]), "courier_name": row["courier_name"]}

    async def badges(self, user, body, query) -> dict[str, Any]:
        """Счётчики для плиток главного экрана — у каждого пользователя свои.

        orders — активные заказы покупателя и их статусы (чтобы приложение
        могло сказать «Заказ №7: курьер в пути»); courier — свободные заказы
        и заказы в работе; admin — заказы, которые ждут действия админа."""
        cart = self.db.get_cart(user.id)
        active = [
            o for o in self.db.get_user_orders(user.id)
            if o["status"] in CUSTOMER_ACTIVE_STATUSES
        ]
        data: dict[str, Any] = {
            "pending_rating": self._pending_rating(user.id),
            "partner": self._partner_short(user.id),
            "bonus": self._bonus_short(user.id),
            "announcement": self.db.get_announcement(),
            "cart": sum(int(i["quantity"]) for i in cart),
            "orders_active": len(active),
            "orders": {
                str(o["id"]): {
                    "status": o["status"],
                    "label": CUSTOMER_STATUS_LABELS.get(o["status"], o["status"]),
                    "arrived_at": o["courier_arrived_at"] if "courier_arrived_at" in o.keys() else None,
                }
                for o in active
            },
        }
        data["chat_unread"] = self.db.support_unread_for_user(user.id)  # ответы RelaxGas
        # сообщения курьера по активным заказам покупателя: {номер заказа: сколько непрочитано}
        data["order_chat"] = {str(k): v for k, v in self.db.order_chat_unread([int(o["id"]) for o in active], False).items()}
        if (me_courier := self.courier_of(user.id)) is not None:
            shift = self.db.courier_shift(me_courier)
            data["courier"] = {
                # «Вы ещё на смене?» — раз в час, окно нельзя закрыть; busy — заказы в работе.
                "shift_ask": shift["ask"],
                "shift_busy": shift["busy"],
                "available": len(self.db.get_available_orders_for(user.id)),
                "current": len(self.db.get_courier_active_orders(user.id)),
                # сообщения покупателей по заказам курьера в работе
                "chat": {str(k): v for k, v in self.db.or…35991 tokens truncated…ены)" if changed else ""),
                        target_user_id=c["telegram_id"], target_name=c["name"], details=self._stock_text(final))
        if c["telegram_id"]:
            text = (("✅ Администратор подтвердил выход на смену"
                     + (f". Количества исправлены: {self._stock_text(final)}" if changed else "") + ". В путь!")
                    if approve else "❌ Администратор не подтвердил выход на смену. Проверьте товары и отправьте снова.")
            await self._safe(self.bridge.courier_message(int(c["telegram_id"]), text), "ответ на заявку смены")
        return {"ok": True, "approved": approve}

    async def admin_shift_end(self, user, body, query, courier_id: int) -> dict[str, Any]:
        """Подтвердить конец смены (курьер нажал «Завершить смену») или закрыть смену курьера,
        который не отвечает на опрос (28.10.2026). Остаток полных — на следующую смену."""
        c = self.db.get_courier(courier_id)
        if not c:
            raise ApiError(404, "Курьер не найден.")
        status = self.db.courier_shift(c)["status"]
        if status not in ("ending", "silent", "online", "break"):
            raise ApiError(409, "Смена уже закрыта.")
        busy = self.db.courier_busy(courier_id)
        if busy:
            raise ApiError(409, f"У курьера заказов в работе: {busy}. Сначала снимите их или дождитесь доставки.")
        view = self.db.shift_end_view(c)
        reason = "self" if status == "ending" else "admin"
        self.db.close_courier_shift(courier_id, reason)
        self._log_admin(user, "Смена курьера закрыта" + (" (подтверждён конец смены)" if reason == "self" else " администратором"),
                        target_user_id=c["telegram_id"], target_name=c["name"],
                        details=", ".join(f"{i['name']} — {i['qty']}" for i in view["stock"] if i["qty"]) or "товаров нет")
        if c["telegram_id"]:
            await self._safe(self.bridge.courier_message(int(c["telegram_id"]),
                             "🏁 Администратор подтвердил конец смены. Следующая смена начнётся с вашим остатком — нажмите «В путь!»."),
                             "конец смены курьеру")
        return {"ok": True}

    async def admin_break_seen(self, user, body, query, courier_id: int) -> dict[str, Any]:
        """«👌 ОК» на уведомлении «курьер на перерыве» — просто убрать (курьер вернулся — уходит само)."""
        self.db.mark_break_seen(courier_id)
        return {"ok": True}

    async def admin_ask_all(self, user, body, query) -> dict[str, Any]:
        """«📣 Опросить всех»: «Вы ещё на смене?» сразу всем на смене; часовой отсчёт — заново."""
        n = self.db.ask_all_couriers()
        self._log_admin(user, f"Опрос «Вы ещё на смене?» всем курьерам на смене ({n})")
        return {"asked": n}

    async def admin_reload_answer(self, user, body, query, courier_id: int) -> dict[str, Any]:
        """Загрузка на складе: {"approve": true} — остаток курьера = фактические баллоны; false — прежний."""
        approve = bool(body.get("approve"))
        row = self.db.resolve_reload(courier_id, approve)
        if not row:
            raise ApiError(409, "Запрос уже обработан — обновите список.")
        stock = self.db.parse_stock(row["reload_pending"]) or {}
        self._log_admin(user, "Склад: загрузка " + ("подтверждена" if approve else "отклонена"),
                        target_user_id=row["telegram_id"], target_name=row["name"], details=self._stock_text(stock))
        if row["telegram_id"]:
            await self._safe(self.bridge.courier_message(int(row["telegram_id"]), (
                f"✅ Администратор подтвердил загрузку на складе: {self._stock_text(stock)}." if approve
                else "❌ Администратор не подтвердил загрузку на складе — остаток баллонов прежний.")),
                "ответ по складу")
        return await self.admin_couriers(user, {}, {})

    # ---------- «🔄 Обмен» между курьерами (владелец 28.10.2026)
    # Курьер А выбирает курьера Б (только на смене), вписывает, сколько отдать (полные и пустые);
    # 30 секунд можно отменить, потом заявку видит админ (главная + бот); после «✅ Подтвердить»
    # остатки у А уменьшаются, у Б увеличиваются.
    ON_SHIFT = ("online", "break")

    def _on_shift_with_stock(self, c) -> bool:
        return bool(c) and self.db.courier_shift(c)["status"] in self.ON_SHIFT and self.db.courier_stock(c) is not None

    def _transfer_shortage(self, c, items: dict[int, int], empties: dict[str, int]) -> list[str]:
        """Чего у курьера не хватает для передачи: [«Баллон 8 л: нужно 3, есть 1», …]."""
        stock = self.db.courier_stock(c) or {"now": {}}
        have_e = self.db.courier_empties(int(c["id"]))
        names = self.db.product_names(items)
        out = []
        for pid, n in items.items():
            have = int(stock["now"].get(str(pid), 0))
            if n > have:
                out.append(f"{names.get(pid, f'товар {pid}')}: отдать {n}, есть {max(0, have)}")
        for z, n in empties.items():
            have = int(have_e.get(z, 0))
            if n > have:
                out.append(f"♻️ пустые {z} л: отдать {n}, есть {max(0, have)}")
        return out

    def _transfer_text(self, t: dict) -> str:
        parts = [self._stock_text(t["items"])] if any(t["items"].values()) else []
        if any(t["empties"].values()):
            parts.append("♻️ пустые: " + ", ".join(f"{z} л — {n}" for z, n in t["empties"].items() if n))
        return "; ".join(parts) or "ничего"

    def _transfer_json(self, t: dict | None) -> dict[str, Any] | None:
        if not t:
            return None
        to = self.db.get_courier(int(t["to_id"]))
        left = max(0, self.db.TRANSFER_CANCEL_SECONDS - int(self.db.transfer_age(t)))
        return {"id": t["id"], "to": to["name"] if to else "—", "text": self._transfer_text(t),
                "at": t["created_at"], "cancel_left": left}

    async def _transfer_alert(self, transfer_id: int) -> None:
        """Через 30 секунд после «Передать» — админу в бот (один раз; заявка ещё ждёт)."""
        t = self.db.get_transfer(transfer_id)
        if not t or t["status"] != "pending" or self.db.transfer_age(t) < self.db.TRANSFER_CANCEL_SECONDS:
            return
        if not self.db.mark_transfer_alerted(transfer_id):
            return
        a, b = self.db.get_courier(int(t["from_id"])), self.db.get_courier(int(t["to_id"]))
        await self.bridge.admin_alert(f"🔄 Обмен: {a['name'] if a else '—'} → {b['name'] if b else '—'}\n"
                                      f"{self._transfer_text(t)}\nПодтвердить — в приложении, на главной админа.")

    async def _transfer_alerts_catch_up(self) -> None:
        """Уведомления, которые не ушли (сервер перезапускался в эти 30 секунд)."""
        for t in self.db.pending_transfers():
            if not t["alerted"] and self.db.transfer_age(t) >= self.db.TRANSFER_CANCEL_SECONDS:
                await self._safe(self._transfer_alert(int(t["id"])), "обмен: уведомление админу")

    async def _transfer_alert_later(self, transfer_id: int) -> None:
        await asyncio.sleep(self.db.TRANSFER_CANCEL_SECONDS + 1)
        await self._transfer_alert(transfer_id)

    async def courier_transfer_get(self, user, body, query) -> dict[str, Any]:
        courier = self._courier_or_403(user)
        await self._transfer_alerts_catch_up()
        me = int(courier["id"])
        on_shift = self._on_shift_with_stock(courier)
        stock = self.db.courier_stock(courier) if on_shift else None
        return {
            "on_shift": on_shift,
            "couriers": [{"id": int(c["id"]), "name": c["name"]} for c in self.db.get_couriers()
                         if int(c["id"]) != me and self._on_shift_with_stock(c)] if on_shift else [],
            "items": [{"id": it["id"], "name": it["name"], "qty": it["now"]} for it in (stock or {}).get("items", []) if it["now"] > 0],
            "empties": {z: n for z, n in self.db.courier_empties(me).items() if n > 0} if on_shift else {},
            "pending": self._transfer_json(self.db.pending_transfer_from(me)),
            "cancel_seconds": self.db.TRANSFER_CANCEL_SECONDS,
        }

    async def courier_transfer_create(self, user, body, query) -> dict[str, Any]:
        courier = self._courier_or_403(user)
        me = int(courier["id"])
        if not self._on_shift_with_stock(courier):
            raise ApiError(409, "Передать можно только на смене (после «В путь!»).")
        if self.db.pending_transfer_from(me):
            raise ApiError(409, "Предыдущая передача ещё ждёт администратора.")
        try:
            to_id = int(body.get("to_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Выберите курьера.")
        to = self.db.get_courier(to_id)
        if to_id == me or not to:
            raise ApiError(400, "Выберите курьера.")
        if not self._on_shift_with_stock(to):
            raise ApiError(409, f"{to['name']} не на смене — передать можно только курьеру на смене.")
        items: dict[int, int] = {}
        for k, v in (body.get("items") or {}).items():
            n = _int(v, "Количество", 0, 9999)
            if n:
                items[_int(k, "Товар", 1)] = n
        sizes = set(self.db.empty_sizes())
        empties: dict[str, int] = {}
        for k, v in (body.get("empties") or {}).items():
            n = _int(v, "Количество", 0, 9999)
            if n and str(k) in sizes:
                empties[str(k)] = n
        if not items and not empties:
            raise ApiError(400, "Впишите, сколько отдать.")
        short = self._transfer_shortage(courier, items, empties)
        if short:
            raise ApiError(409, "Столько нет: " + "; ".join(short))
        tid = self.db.create_transfer(me, to_id, items, empties)
        if self.background:
            task = asyncio.create_task(self._run_notification(self._transfer_alert_later(tid), "обмен: уведомление админу"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return await self.courier_transfer_get(user, {}, {})

    async def courier_transfer_cancel(self, user, body, query) -> dict[str, Any]:
        courier = self._courier_or_403(user)
        t = self.db.pending_transfer_from(int(courier["id"]))
        if not t:
            raise ApiError(409, "Заявки уже нет.")
        if self.db.transfer_age(t) >= self.db.TRANSFER_CANCEL_SECONDS:
            raise ApiError(409, "Отменить можно только в первые 30 секунд — заявка уже у администратора.")
        self.db.set_transfer_status(int(t["id"]), "cancelled")
        return await self.courier_transfer_get(user, {}, {})

    async def admin_transfer_decide(self, user, body, query, transfer_id: int) -> dict[str, Any]:
        t = self.db.get_transfer(transfer_id)
        if not t or t["status"] != "pending":
            raise ApiError(409, "Заявки уже нет.")
        if self.db.transfer_age(t) < self.db.TRANSFER_CANCEL_SECONDS:
            raise ApiError(409, "Курьер ещё может отменить заявку — подождите 30 секунд.")
        a, b = self.db.get_courier(int(t["from_id"])), self.db.get_courier(int(t["to_id"]))
        what = self._transfer_text(t)
        if body.get("approve"):
            if not self._on_shift_with_stock(a):
                raise ApiError(409, f"{a['name'] if a else 'Курьер'} уже не на смене — отклоните заявку.")
            if not self._on_shift_with_stock(b):
                raise ApiError(409, f"{b['name'] if b else 'Курьер'} уже не на смене — отклоните заявку.")
            short = self._transfer_shortage(a, t["items"], t["empties"])
            if short:
                raise ApiError(409, f"У {a['name']} уже столько нет: " + "; ".join(short) + " — отклоните заявку.")
            if not self.db.set_transfer_status(transfer_id, "approved", user.display_name):
                raise ApiError(409, "Заявки уже нет.")
            self.db.apply_transfer(t, self.db.product_names(t["items"]), a["name"], b["name"])
            self._log_admin(user, "Обмен между курьерами: подтверждён", target_name=f"{a['name']} → {b['name']}", details=what)
            if a["telegram_id"]:
                await self._safe(self.bridge.courier_message(int(a["telegram_id"]), f"✅ Администратор подтвердил передачу курьеру {b['name']}: {what}"),
                                 "обмен: ответ курьеру")
            if b["telegram_id"]:
                await self._safe(self.bridge.courier_message(int(b["telegram_id"]), f"🔄 Вам передал {a['name']}: {what}. Остатки обновлены."),
                                 "обмен: ответ курьеру")
        else:
            if not self.db.set_transfer_status(transfer_id, "rejected", user.display_name):
                raise ApiError(409, "Заявки уже нет.")
            self._log_admin(user, "Обмен между курьерами: отклонён", target_name=f"{a['name'] if a else '—'} → {b['name'] if b else '—'}", details=what)
            if a and a["telegram_id"]:
                await self._safe(self.bridge.courier_message(int(a["telegram_id"]), f"✖️ Администратор отклонил передачу курьеру {b['name'] if b else '—'}: {what}"),
                                 "обмен: ответ курьеру")
        return {"alerts": self._admin_alerts()}

    def _admin_alerts(self) -> list[dict[str, Any]]:
        """Уведомления на главном экране админа — висят, пока вопрос не решён.
        source: courier (жёлтая рамка) / customer (бирюзовая)."""
        out: list[dict[str, Any]] = []
        for c in self.db.reload_requests():
            stock = self.db.parse_stock(c["reload_pending"]) or {}
            try:
                handed = json.loads(c["reload_empties"]) if c["reload_empties"] else None
            except ValueError:
                handed = None
            out.append({"key": f"reload:{c['id']}", "kind": "reload", "source": "courier", "id": c["id"],
                        "at": c["reload_pending_at"], "title": f"🏭 {c['name']} — загрузка на складе",
                        "text": self._stock_text(stock), "empties_handed": handed})
        # 28.10.2026: конец смены ждёт подтверждения; не отвечает на опрос; перерыв (до «ОК» или возвращения).
        for c in self.db.get_couriers():
            shift = self.db.courier_shift(c)
            st = shift["status"]
            if st in ("ending", "silent"):
                view = self.db.shift_end_view(c)
                title = f"🏁 {c['name']} завершил смену" if st == "ending" else f"⚠️ {c['name']} не отвечает на опрос «Вы ещё на смене?»"
                at = shift.get("end_requested_at") if st == "ending" else shift.get("silent_since")
                out.append({"key": f"{st}:{c['id']}:{at}", "kind": "shift_end" if st == "ending" else "silent", "source": "courier",
                            "id": c["id"], "at": at, "title": title, "busy": shift.get("busy", 0),
                            "text": "Остаток полных: " + (", ".join(f"{i['name']} — {i['qty']}" for i in view["stock"] if i["qty"]) or "ничего нет"),
                            "end": view})
            elif st == "break" and not c["break_seen_at"]:
                out.append({"key": f"break:{c['id']}:{shift.get('ended_at')}", "kind": "break", "source": "courier", "id": c["id"],
                            "at": shift.get("ended_at"), "title": f"☕ {c['name']} на перерыве", "text": ""})
        for t in self.db.pending_transfers():  # «🔄 Обмен» — после 30 секунд на отмену (28.10.2026)
            if self.db.transfer_age(t) < self.db.TRANSFER_CANCEL_SECONDS:
                continue
            a, b = self.db.get_courier(int(t["from_id"])), self.db.get_courier(int(t["to_id"]))
            out.append({"key": f"transfer:{t['id']}", "kind": "transfer", "source": "courier", "id": t["id"], "at": t["created_at"],
                        "title": f"🔄 Обмен: {a['name'] if a else '—'} → {b['name'] if b else '—'}", "text": self._transfer_text(t)})
        for c in self.db.get_couriers():  # запрос курьера обнулить деньги (28.10.2026)
            req = self.db.counter_reset_request(c)
            if req:
                out.append({"key": f"reset:{c['id']}:{req['at']}", "kind": "counter_reset", "source": "courier", "id": c["id"],
                            "at": req["at"], "title": self._reset_request_text(c, req["names"]), "text": ""})
        for c in self.db.shift_requests():  # «В путь!» ждёт подтверждения (26.10.2026)
            req = self.db.shift_request(c)
            if not req:
                continue
            where = {"left": " (остаток прошлой смены)", "warehouse": " — 🏭 со склада"}.get(req.get("source"), "")
            out.append({"key": f"shift:{c['id']}:{req['at']}", "kind": "shift", "source": "courier", "id": c["id"],
                        "empties_handed": req.get("empties") if req.get("source") == "warehouse" else None,
                        "at": req["at"], "title": f"🚴 {c['name']} хочет выйти на смену{where}",
                        "text": self._stock_text({int(k): v for k, v in req["stock"].items()}),
                        "items": req["items"]})
        for o in self.db.get_disputed_orders():
            o = _row(o)
            out.append({"key": f"disputed:{o['id']}", "kind": "disputed", "source": "courier", "id": o["id"],
                        "at": o.get("disputed_at") or o.get("updated_at"),
                        "title": f"📵 Спорный заказ №{o['id']} — покупатель не выходит на связь",
                        "text": (o.get("delivery_address") or "").split("\n")[0]})
        for o in self.db.get_admin_open_orders():
            o = _row(o)
            if o["status"] == "not_delivered":
                out.append({"key": f"notdel:{o['id']}", "kind": "not_delivered", "source": "courier", "id": o["id"],
                            "at": o.get("updated_at"), "title": f"⚠️ Заказ №{o['id']} не доставлен",
                            "text": (o.get("delivery_address") or "").split("\n")[0]})
            elif int(o.get("individual") or 0) == 1 and o["status"] == "new":
                out.append({"key": f"individual:{o['id']}", "kind": "individual", "source": "customer", "id": o["id"],
                            "at": o.get("created_at"), "title": f"🧾 Индивидуальная доставка — заказ №{o['id']}",
                            "text": (o.get("delivery_address") or "").split("\n")[0]})
        for a in self.db.open_admin_calls():
            name = " ".join(p for p in (a.get("first_name"), a.get("last_name")) if p) or f"ID {a['user_id']}"
            out.append({"key": f"call:{a['id']}", "kind": "call", "source": "customer", "id": a["id"],
                        "at": a["created_at"], "title": f"📞 {name} зовёт администратора", "text": a["text"],
                        "username": (a.get("username") or "").lstrip("@") or None, "phone": a.get("last_phone")})
        pending = self.db.count_pending_referral_requests()
        if (pending or {}).get("partner"):
            out.append({"key": "partner", "kind": "partner", "source": "customer", "id": 0, "at": None,
                        "title": f"💼 Заявки на партнёрскую программу: {pending['partner']}", "text": "Партнёры и заявки"})
        return out

    async def admin_alerts(self, user, body, query) -> dict[str, Any]:
        await self._transfer_alerts_catch_up()
        return {"alerts": self._admin_alerts()}

    async def admin_call_resolve(self, user, body, query, call_id: int) -> dict[str, Any]:
        if not self.db.resolve_admin_call(call_id, user.id):
            raise ApiError(409, "Уже решено.")
        self._log_admin(user, "Вызов администратора: решено", details=f"обращение №{call_id}")
        return {"alerts": self._admin_alerts()}

    async def admin_courier_create(self, user, body, query) -> dict[str, Any]:
        import sqlite3

        name = _text(body.get("name"), "Имя", 2, 100)
        phone = _optional_text(body.get("phone"), 40)
        raw_tg = body.get("telegram_id")
        telegram_id = None if raw_tg in (None, "") else _int(raw_tg, "Telegram ID", 1)
        username = (_optional_text(body.get("username"), 64) or "").lstrip("@").strip() or None
        if username and " " in username:
            raise ApiError(400, "Username не должен содержать пробелов.")
        try:
            courier_id = self.db.add_courier(name, phone, telegram_id, username)
        except sqlite3.IntegrityError:
            raise ApiError(409, "Такой Telegram ID уже используется.")
        return {"id": courier_id}

    async def admin_courier_active(self, user, body, query, courier_id: int) -> dict[str, Any]:
        courier = self.db.set_courier_active(courier_id, bool(body.get("active")))
        if not courier:
            raise ApiError(404, "Курьер не найден.")
        return {"ok": True, "is_active": bool(courier["is_active"])}

    # ------------------------------------------------------------ admin: theme
    async def admin_settings(self, user, body, query) -> dict[str, Any]:
        catalog = self.db.get_catalog_photo()
        qr = {m: self.db.get_payment_qr(m) for m in ("sbp", "crypto")}
        return {
            "floor_lift_price": self.db.get_floor_lift_price(),
            "catalog_photo": f"/api/media/catalog/main?v={_short(catalog)}" if catalog else None,
            "payment_qr": {m: (f"/api/media/qr/{m}?v={_short(v)}" if v else None) for m, v in qr.items()},
            "payment_switches": self.db.get_payment_switches(),
            # Доставка за пределы Москвы: база + ₽ за км; key — задан ли ключ маршрутов в Amvera.
            "outside": {**self.db.outside_prices(), "key": bool(self.routing is not None and self.routing.enabled)},
            "announcement": self.db.get_announcement(),
            "home_greeting": self.db.get_shop_texts()["home_greeting"],
            "home_greeting_default": DEFAULT_HOME_GREETING,
            "courier_delivery_rate": self.db.courier_delivery_rate(),
            "courier_rates": self.db.courier_rates(),
        }

    async def admin_address_check(self, user, body, query) -> dict[str, Any]:
        """Состояние базы адресов: загружена ли, когда собрана, сколько домов."""
        if self.geo is None:
            return {"ok": False, "message": "База адресов не подключена."}
        st = self.geo.status()
        if not st["loaded"]:
            msg = st["last_error"] or "База адресов ещё не скачана — нажмите «🔄 Скачать свежую»."
            if st.get("token_var") and st["token_var"] != "GEODB_GITHUB_TOKEN":
                msg += f" Токен найден под именем «{st['token_var']}» — лучше переименовать в GEODB_GITHUB_TOKEN."
            return {"ok": False, **st, "message": msg}
        rb = st.get("rebuild") or {}
        return {"ok": True, **st, "message": (
            f"Работает: {st['houses']:,} домов, из них в Москве {st['moscow_houses']:,}; улиц {st['streets']:,}. "
            f"Собрана {msk_datetime(st['built_at'])}.").replace(",", " ")
            + (f" {rb['message']}" if rb.get("message") else "")
            + (f" Последняя ошибка обновления: {st['last_error']}" if st["last_error"] and rb.get("state") != "error" else "")}

    async def admin_address_rebuild(self, user, body, query) -> dict[str, Any]:
        """«🛠 Обновить карты» — собрать базу заново из свежего OpenStreetMap на GitHub и скачать."""
        if self.geo is None:
            raise ApiError(409, "База адресов не подключена.")
        try:
            await self.geo.start_rebuild()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, str(exc))
        return await self.admin_address_check(user, body, query)

    async def admin_address_update(self, user, body, query) -> dict[str, Any]:
        """«🔄 Скачать свежую базу сейчас» — из Releases на GitHub."""
        if self.geo is None:
            raise ApiError(409, "База адресов не подключена.")
        res = await self.geo.update_from_github(force=True)
        if res.get("error"):
            raise ApiError(502, res["error"])
        return await self.admin_address_check(user, body, query)

    async def admin_home_greeting(self, user, body, query) -> dict[str, Any]:
        """Текст приветствия в шапке магазина; {"text": ""} — вернуть стандартный."""
        if len(str(body.get("text") or "").strip()) > MAX_GREETING:
            raise ApiError(400, f"Приветствие — не длиннее {MAX_GREETING} символов.")
        text = self.db.set_home_greeting(_optional_text(body.get("text"), MAX_GREETING))
        return {"home_greeting": text}

    async def admin_payment_switch(self, user, body, query) -> dict[str, Any]:
        """Ползунки «📱 СБП» / «💎 Криптовалюта»: {"method": "sbp", "enabled": false}."""
        method = body.get("method")
        if method not in ("sbp", "crypto"):
            raise ApiError(400, "Неизвестный способ оплаты.")
        enabled = bool(body.get("enabled"))
        switches = self.db.set_payment_switch(method, enabled)
        label = "СБП" if method == "sbp" else "Криптовалюта"
        await self._safe(self.bridge.bonus_event(
            f"💳 Оплата «{label}» {'включена' if enabled else 'выключена'} для всех покупателей"), "переключатель оплаты")
        return {"payment_switches": switches, "payment_methods": self.available_payment_methods()}

    async def admin_announcement(self, user, body, query) -> dict[str, Any]:
        """📢 Объявление на главном экране магазина. {"text": ""} — убрать."""
        text = _optional_text(body.get("text"), MAX_ANNOUNCEMENT)
        if len(str(body.get("text") or "").strip()) > MAX_ANNOUNCEMENT:
            raise ApiError(400, f"Объявление — не длиннее {MAX_ANNOUNCEMENT} символов.")
        ann = self.db.set_announcement(text)
        await self._safe(self.bridge.bonus_event(
            f"📢 Объявление {'опубликовано: ' + text if ann else 'убрано'}"), "объявление")
        return {"announcement": ann}

    # ------------------------------------------------------------ admin: QR для оплаты при курьере
    async def admin_payment_qr_list(self, user, body, query) -> dict[str, Any]:
        return {
            "codes": [self.qr_json(c) for c in self.db.get_payment_qr_codes()],
            "currencies": [
                {"code": k, "label": v[2], "networks": crypto.NETWORKS.get(k, [])}
                for k, v in crypto.CURRENCIES.items()
            ],
        }

    async def admin_payment_qr_add(self, user, body, query) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in ("sbp", "crypto"):
            raise ApiError(400, "Выберите вид: СБП или криптовалюта.")
        currency = network = None
        if kind == "crypto":
            # Название не вводится: собирается из монеты и сети (их выбирают из списков).
            currency = str(body.get("currency") or "").upper()
            if currency not in crypto.CURRENCIES:
                raise ApiError(400, "Выберите криптовалюту из списка.")
            networks = crypto.NETWORKS.get(currency, [])
            network = str(body.get("network") or "").strip() or (networks[0] if networks else None)
            if networks and network not in networks:
                raise ApiError(400, "Выберите сеть из списка.")
            title = f"{currency} · {network}" if network else currency
        else:
            # СБП: название необязательно (нужно, если банков несколько).
            title = _optional_text(body.get("title"), 60) or "СБП"
        file_id = await self._upload_admin_image(
            body.get("image"), f"💳 Новый QR для оплаты при курьере: {title} (из приложения)"
        )
        self.db.add_payment_qr_code(kind, title, currency, network, file_id)
        return await self.admin_payment_qr_list(user, {}, {})

    async def admin_payment_qr_remove(self, user, body, query, code_id: int) -> dict[str, Any]:
        if not self.db.remove_payment_qr_code(code_id):
            raise ApiError(404, "QR-код не найден.")
        return await self.admin_payment_qr_list(user, {}, {})

    # ------------------------------------------------------------ admin: функции классической админки
    async def _upload_admin_image(self, value: Any, caption: str) -> str:
        """Картинка из приложения → Telegram (в чат админа), чтобы получить
        file_id — бот хранит фото шапки и QR именно так."""
        raw, mime = decode_data_url(str(value or ""), MAX_ADMIN_IMAGE_BYTES)
        try:
            return await self.bridge.upload_image(raw, mime, caption)
        except Exception:
            logger.exception("Mini App: не удалось загрузить картинку в Telegram")
            raise ApiError(502, "Не удалось загрузить картинку. Попробуйте ещё раз.")

    async def admin_catalog_photo(self, user, body, query) -> dict[str, Any]:
        """Как «🖼 Фото каталога» в боте: {image: data-URL} — заменить, {image: null} — убрать."""
        if body.get("image") in (None, ""):
            self.db.set_catalog_photo(None)
        else:
            self.db.set_catalog_photo(await self._upload_admin_image(body["image"], "🖼 Новое фото шапки каталога (из приложения)"))
        return await self.admin_settings(user, {}, {})

    async def admin_payment_qr(self, user, body, query, method: str) -> dict[str, Any]:
        """Как «💎 QR крипта» / «📱 QR СБП» в боте. Без QR способ оплаты скрыт у покупателей."""
        label = PAYMENT_METHOD_LABELS[method]
        if body.get("image") in (None, ""):
            self.db.set_payment_qr(method, None)
        else:
            self.db.set_payment_qr(method, await self._upload_admin_image(body["image"], f"💳 Новый QR для оплаты: {label} (из приложения)"))
        return await self.admin_settings(user, {}, {})

    # ------------------------------------------------------------ admin: выгрузка в Excel
    async def admin_export_info(self, user, body, query) -> dict[str, Any]:
        from .export import last_completed_shift_day

        return {"default_day": last_completed_shift_day().isoformat()}

    async def admin_export(self, user, body, query) -> dict[str, Any]:
        """{password, deliver: download|chat} — общая выгрузка всех доставленных заказов."""
        from datetime import datetime as _dt

        from .export import MSK, build_xlsx, msk

        if not EXPORT_PASSWORD:
            raise ApiError(503, "Защитный пароль не настроен на сервере.")
        if not secrets.compare_digest(str(body.get("password") or "").strip(), EXPORT_PASSWORD):
            raise ApiError(403, "Неверный пароль выгрузки.")
        # Выгрузка всегда общая — с первого заказа до последнего; нужные даты
        # владелец выбирает фильтром в самой таблице (суточная убрана 18.10).
        stamp = _dt.now(MSK)
        # kind == "reset": таблица №2 «Скачать и обнулить» — только текущий период (с последнего сброса).
        reset = body.get("kind") == "reset"
        resets = await asyncio.to_thread(self.db.export_resets)
        period = (resets[-1]["at"] if resets else None, None) if reset else None
        data = await asyncio.to_thread(self.db.get_sales_lines, None, None, period)
        since = f" (с {msk(data['first_order_at']):%d.%m.%Y})" if data.get("first_order_at") else ""
        title = f"Relax Gas — полная выгрузка с первого заказа{since}, на {stamp:%d.%m.%Y %H:%M} МСК"
        filename = f"RelaxGas_polnaya_vygruzka_{stamp:%Y-%m-%d_%H-%M}.xlsx"
        if reset:
            start = f"с {msk(resets[-1]['at']):%d.%m.%Y %H:%M}" if resets else "с первого заказа"
            title = f"Relax Gas — таблица №2 за период {start} по {stamp:%d.%m.%Y %H:%M} МСК (после скачивания обнулена)"
            filename = f"RelaxGas_tablica2_{stamp:%Y-%m-%d_%H-%M}.xlsx"
        codes = sorted({x["crypto_currency"] for x in data["lines"] if x.get("crypto_currency")})
        rates, rates_at = {}, None
        if codes:
            # «Курс на момент выгрузки» — запрашиваем свежий, при сбое — последний сохранённый.
            try:
                rates, rates_at = await self.bridge.get_crypto_rates_now(codes)
            except Exception:
                logger.exception("Mini App: курсы для выгрузки недоступны")
        # Полная выгрузка: к бухгалтерии («Продажи») добавляются листы
        # «Все заказы», «Клиенты», «Курьеры» (и «Крипта»).
        # Сборка файла занимает секунды на большой истории — в отдельном потоке,
        # чтобы бот и приложение в это время отвечали остальным.
        full = await asyncio.to_thread(self.db.get_full_export, period)
        full["admin_ids"] = sorted(self.admin_ids)
        full["resets"] = [] if reset else resets
        content = await asyncio.to_thread(build_xlsx, data, title, rates=rates, rates_at=rates_at, full=full)
        summary = {
            "lines": len(data["lines"]),
            # баллоны (подъём — услуга, не считается), доставки (заказы) и подъёмы
            "quantity": sum(int(x["quantity"]) for x in data["lines"] if x.get("line_kind") not in ("lift", "bonus", "delivery", "points")),
            "deliveries": len({(x["order_no"], x["created_at"], bool(x.get("archived"))) for x in data["lines"]}),
            "lifts": sum(1 for x in data["lines"] if x.get("line_kind") == "lift"),
            "total": sum(int(x["line_total"]) for x in data["lines"]),
            "wipes": len(data["wipes"]),
            "unfinished": data["unfinished"],
        }
        if reset:
            # Страховка: файл сначала уходит админу в чат с ботом (ссылка живёт всего 10 минут),
            # и только после успешной сборки начинается новый период. Данные приложения не удаляются.
            try:
                await self.bridge.send_document(user.id, content, filename, f"📊 {title}")
                summary["backup_sent"] = True
            except Exception:
                logger.exception("Mini App: копия таблицы №2 не отправлена в чат")
                summary["backup_sent"] = False
            at = _dt.now(timezone.utc).isoformat()
            await asyncio.to_thread(self.db.add_export_reset, at, user.id, user.display_name)
            self._log_admin(user, "Выгрузка: скачать и обнулить таблицу №2", details=f"доставок в файле: {summary['deliveries']}")
            summary["reset_at"] = at
        elif body.get("deliver") == "chat":
            try:
                await self.bridge.send_document(user.id, content, filename, f"📊 {title}")
            except Exception:
                logger.exception("Mini App: не удалось отправить выгрузку в чат")
                raise ApiError(502, "Не удалось отправить файл в чат. Попробуйте ещё раз.")
            return {"sent": True, "file_name": filename, "summary": summary}
        now = time.time()
        for token in [t for t, (_, _, exp) in self.exports.items() if exp < now]:
            self.exports.pop(token, None)
        token = secrets.token_urlsafe(24)
        self.exports[token] = (content, filename, now + EXPORT_TTL)
        return {"url": f"/export/{token}/{filename}", "file_name": filename, "summary": summary}

    def get_export(self, token: str) -> tuple[bytes, str] | None:
        """Файл по одноразовой ссылке (токен знает только админ, ссылка живёт 10 минут)."""
        item = self.exports.get(token)
        if not item or item[2] < time.time():
            self.exports.pop(token, None)
            return None
        return item[0], item[1]

    async def admin_test_log(self, user, body, query) -> dict[str, Any]:
        try:
            sent = await self.bridge.test_log_group()
        except Exception:
            logger.exception("Mini App: тестовое сообщение в канал не отправлено")
            raise ApiError(502, "Не удалось отправить. Проверьте, что бот добавлен в канал и может писать.")
        if not sent:
            raise ApiError(409, "Канал не настроен: переменная LOG_GROUP_CHAT_ID не задана.")
        return {"ok": True}

    def _check_danger_password(self, body: dict[str, Any]) -> None:
        if not DANGER_PASSWORD:
            raise ApiError(503, "Защитный пароль не настроен на сервере.")
        if not secrets.compare_digest(str(body.get("password") or "").strip(), DANGER_PASSWORD):
            raise ApiError(403, "Неверный пароль.")

    async def admin_wipe_products(self, user, body, query) -> dict[str, Any]:
        self._check_danger_password(body)
        self.quotes.clear()
        return {"deleted": self.db.wipe_all_products()}

    async def admin_wipe_orders(self, user, body, query) -> dict[str, Any]:
        self._check_danger_password(body)
        self.quotes.clear()
        return {"deleted": self.db.wipe_all_order_history()}

    async def admin_product_purge(self, user, body, query, product_id: int) -> dict[str, Any]:
        if not self.db.hard_delete_product(product_id):
            raise ApiError(404, "Товар уже удалён или не найден.")
        return {"ok": True}

    async def admin_courier_delete(self, user, body, query, courier_id: int) -> dict[str, Any]:
        from db import CourierBusyError  # импорт здесь: ядро не зависит от db при импорте

        try:
            deleted = self.db.delete_courier(courier_id)
        except CourierBusyError:
            raise ApiError(409, "У курьера есть заказ в работе. Сначала завершите его или снимите курьера с заказа.")
        except Exception:
            logger.exception("Mini App: не удалось удалить курьера %s", courier_id)
            raise ApiError(409, "Не удалось удалить курьера — на него ссылаются другие записи.")
        if not deleted:
            raise ApiError(404, "Курьер не найден (возможно, уже удалён).")
        return {"ok": True}

    async def admin_customers(self, user, body, query) -> dict[str, Any]:
        """Как «👥 Покупатели» в боте + отзывы курьеров о покупателе."""
        counts = self.db.get_purchase_counts()
        balances = self.db.bonus_balances()
        phones = self.db.customer_phones()
        users = self.db.get_users()
        reviews = self.db.get_customer_reviews_bulk([int(u["telegram_id"]) for u in users], limit=3)
        result = []
        for u in users:
            uid = int(u["telegram_id"])
            name = " ".join(p for p in (u["first_name"], u["last_name"]) if p) or "Покупатель"
            result.append({
                "telegram_id": uid,
                "name": name,
                "username": (u["username"] or "").lstrip("@") or None,
                "purchases": counts.get(uid, 0),
                "admin_rating": u["admin_rating"],
                "admin_note": u["admin_note"],
                "reviews": reviews[uid],
                "blocked": bool(u["is_blocked"]),
                "blocked_reason": u["blocked_reason"],
                "blocked_at": u["blocked_at"],
                "updated_at": u["updated_at"],
                "phones": phones.get(uid, []),
                "bonus_balance": balances.get(uid, 0),
                "bonus_percent": u["bonus_percent"],  # None — по умолчанию
            })
        return {"customers": result, "bonus_default_percent": self.db.BONUS_DEFAULT_PERCENT,
                "bonus_min_items_total": self.db.BONUS_MIN_ITEMS_TOTAL}

    def _customer_name(self, telegram_id: int) -> str:
        u = _row(self.db.get_user(telegram_id))
        if not u:
            raise ApiError(404, "Покупатель не найден.")
        return " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or "Покупатель"

    async def admin_customer_bonus_percent(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        """Индивидуальный процент бонусов. {"percent": null} — вернуть «по умолчанию»."""
        name = self._customer_name(telegram_id)
        raw = body.get("percent")
        if raw in (None, ""):
            percent = None
        else:
            try:
                percent = round(float(str(raw).replace(",", ".")), 1)
            except ValueError:
                raise ApiError(400, "Процент — число, например 2 или 3.5.")
            if not 0 <= percent <= 100:
                raise ApiError(400, "Процент — от 0 до 100.")
        self.db.set_user_bonus_percent(telegram_id, percent)
        shown = f"{percent:g} %" if percent is not None else f"по умолчанию ({self.db.BONUS_DEFAULT_PERCENT:g} %)"
        self._log_admin(user, "Бонусы: процент", target_user_id=telegram_id, target_name=name, details=shown)
        await self._safe(self.bridge.bonus_event(
            f"💰 {name}: процент бонусов — {shown} — админ {user.display_name}"), "процент бонусов")
        return {"telegram_id": telegram_id, "bonus_percent": percent,
                "effective_percent": self.db.bonus_percent_for(telegram_id)}

    async def admin_customer_bonus_adjust(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        """Вручную: {"amount": 500} — начислить, {"amount": -200} — списать (не больше остатка)."""
        name = self._customer_name(telegram_id)
        amount = _int(body.get("amount"), "бонусы", -100_000_000, 100_000_000)
        if amount == 0:
            raise ApiError(400, "Укажите, сколько бонусов начислить или списать.")
        reason = _optional_text(body.get("reason"), 200)
        try:
            balance = self.db.adjust_customer_bonus(telegram_id, amount, reason)
        except ValueError:
            raise ApiError(409, f"Списать можно не больше {self.db.bonus_balance(telegram_id)} бонусов.")
        verb = f"начислено {amount}" if amount > 0 else f"списано {-amount}"
        self._log_admin(user, "Бонусы: начисление" if amount > 0 else "Бонусы: списание",
                        target_user_id=telegram_id, target_name=name, amount=amount, balance_after=balance, details=reason)
        await self._safe(self.bridge.bonus_event(
            f"💰 {name}: вручную {verb} бонусов" + (f" ({reason})" if reason else "") + f". Остаток: {balance}"
            + f" — админ {user.display_name}"
        ), "бонусы вручную")
        return {"telegram_id": telegram_id, "bonus_balance": balance}

    async def admin_customer_bonus_history(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        self._customer_name(telegram_id)
        return {
            "balance": self.db.bonus_balance(telegram_id),
            "percent": self.db.bonus_percent_for(telegram_id),
            "history": self._bonus_rows(self.db.bonus_history(telegram_id)),
        }

    async def admin_customer_block(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        """Чёрный список: {"blocked": true, "reason": "..."} — добавить, {"blocked": false} — убрать."""
        if telegram_id in self.admin_ids:
            raise ApiError(400, "Нельзя заблокировать администратора.")
        blocked = bool(body.get("blocked"))
        reason = _optional_text(body.get("reason"), 300) if blocked else None
        row = self.db.set_user_blocked(telegram_id, blocked, reason)
        if not row:
            raise ApiError(404, "Покупатель не найден.")
        name = " ".join(p for p in (row["first_name"], row["last_name"]) if p) or "Покупатель"
        self._log_admin(user, "Чёрный список: добавлен" if blocked else "Чёрный список: убран",
                        target_user_id=telegram_id, target_name=name, details=reason)
        await self._safe(self.bridge.customer_blocked(telegram_id, name, blocked, reason), "чёрный список")
        cancelled: list[int] = []
        if blocked:
            # Активные заказы заблокированного аннулируются — как «❌ Отменить» у админа
            # (те же уведомления курьерам и покупателю), чтобы они не висели вечно.
            for current in self.db.get_user_orders(telegram_id):
                if current["status"] not in CUSTOMER_ACTIVE_STATUSES:
                    continue
                order_id = int(current["id"])
                if self.db.update_order_status(order_id, "cancelled"):
                    cancelled.append(order_id)
                    await self._safe(
                        self.bridge.admin_status(order_id, _row(current), "cancelled"),
                        f"отмена заказа {order_id} (чёрный список)", order_id,
                    )
        return {"telegram_id": telegram_id, "blocked": blocked, "blocked_reason": reason, "cancelled": cancelled}

    async def staff_customer_reviews(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        """«Все отзывы» о покупателе — только курьерам и админу."""
        summary = self.db.get_customer_reviews(telegram_id, limit=0)
        return {**summary, "reviews": self.db.get_all_customer_reviews(telegram_id)}

    async def admin_customer_rating(self, user, body, query, telegram_id: int) -> dict[str, Any]:
        rating = _int(body.get("rating"), "оценка", 1, 5)
        note = _optional_text(body.get("note"), 500)
        if len(str(body.get("note") or "").strip()) > 500:
            raise ApiError(400, "Заметка должна быть не длиннее 500 символов.")
        if not self.db.set_user_admin_rating(telegram_id, rating, note):
            raise ApiError(404, "Покупатель не найден.")
        return {"ok": True, "admin_rating": rating, "admin_note": note}

    async def admin_outside_update(self, user, body, query) -> dict[str, Any]:
        """Доставка за пределы Москвы: {"base": 500, "per_km": 40}; per_km = 0 — авторасчёт выключен."""
        base = _int(body.get("base"), "база доставки", 0, 100_000)
        per_km = _int(body.get("per_km"), "цена за километр", 0, 10_000)
        res = self.db.set_outside_prices(base, per_km)
        self._log_admin(user, f"Доставка за МКАД: {base} ₽ + {per_km} ₽/км")
        return {**res, "key": bool(self.routing is not None and self.routing.enabled)}

    async def _outside_quote(self, lat: float | None, lon: float | None) -> dict[str, Any] | None:
        """Стоимость доставки за пределы Москвы по дорогам: база + ₽ × км за границей Москвы
        (км вверх до целого). None — посчитать нельзя (нет ключа, цены, точки или ответа)."""
        prices = self.db.outside_prices()
        if (lat is None or lon is None or prices["per_km"] <= 0 or self.routing is None
                or not self.routing.enabled or not self._geo_ready()):
            return None
        if self.geo.point_in_moscow(lat, lon):
            return None
        key = f"{lat:.4f},{lon:.4f}"
        km = self.db.route_cached(key)
        if km is None:
            from .routing import outside_km
            try:
                coords = await self.routing.route(lat, lon)
            except Exception:
                logger.exception("Маршрут за пределы Москвы не построен (%s)", key)
                return None
            km = outside_km(coords, self.geo.point_in_moscow)
            self.db.route_remember(key, km)
        kmi = max(1, math.ceil(km - 1e-9))
        return {"km": kmi, "fee": prices["base"] + prices["per_km"] * kmi, "base": prices["base"], "per_km": prices["per_km"]}

    async def delivery_quote(self, user, body, query) -> dict[str, Any]:
        """Оформление: стоимость доставки за пределы Москвы — по точке (lat, lon) или по дому из базы."""
        lat = lon = None
        if query.get("lat") and query.get("lon"):
            lat, lon = self._coords(query.get("lat"), query.get("lon"))
        elif query.get("street_id") and query.get("house") and self._geo_ready():
            try:
                found = self.geo.house_point(int(query["street_id"]), str(query["house"])[:20], (query.get("building") or None))
            except (TypeError, ValueError):
                found = None
            if found:
                lat, lon = found["lat"], found["lon"]
        quote = await self._outside_quote(lat, lon)
        return {"available": quote is not None, **(quote or {})}

    async def admin_settings_update(self, user, body, query) -> dict[str, Any]:
        price = _int(body.get("floor_lift_price"), "цена подъёма", 0, MAX_LIFT_PRICE)
        return {"floor_lift_price": self.db.set_floor_lift_price(price)}

    DEV_PW_MIN = 6

    def _check_dev_password(self, user, pw: str) -> None:
        """Проверка пароля «Разработчика» для опасных действий (удаление истории). 5 ошибок — блок на 10 минут."""
        now = time.time()
        fails = [t for t in self._dev_fails.get(user.id, []) if now - t < 600]
        if len(fails) >= 5:
            raise ApiError(429, "Слишком много неверных попыток. Подождите 10 минут.")
        if not self.db.dev_password_is_set():
            raise ApiError(403, "Сначала задайте пароль: «Админка» → «Разработчик» (первый ввод создаёт пароль).")
        if not self.db.dev_password_check(pw):
            fails.append(now)
            self._dev_fails[user.id] = fails
            raise ApiError(403, "Неверный пароль.")
        self._dev_fails.pop(user.id, None)

    async def admin_dev_unlock(self, user, body, query) -> dict[str, Any]:
        """Вход в «Разработчик» по паролю. Пароль ещё не задан — первый ввод его создаёт (от 6 символов).
        После 5 неверных попыток подряд вход блокируется на 10 минут."""
        pw = str(body.get("password") or "")
        now = time.time()
        fails = [t for t in self._dev_fails.get(user.id, []) if now - t < 600]
        if len(fails) >= 5:
            raise ApiError(429, "Слишком много неверных попыток. Подождите 10 минут.")
        if not self.db.dev_password_is_set():
            if len(pw) < self.DEV_PW_MIN:
                raise ApiError(400, f"Придумайте пароль от {self.DEV_PW_MIN} символов — он будет защищать «Разработчик».")
            self.db.dev_password_set(pw)
            self._log_admin(user, "Задал пароль раздела «Разработчик»")
            return {"ok": True, "created": True}
        if not self.db.dev_password_check(pw):
            fails.append(now)
            self._dev_fails[user.id] = fails
            raise ApiError(403, "Неверный пароль.")
        self._dev_fails.pop(user.id, None)
        return {"ok": True, "created": False}

    async def admin_theme(self, user, body, query) -> dict[str, Any]:
        if not self.db.dev_password_check(str(body.get("dev_password") or "")):
            raise ApiError(403, "Нужен пароль раздела «Разработчик».")
        theme = body.get("theme") or {}
        if not isinstance(theme, dict):
            raise ApiError(400, "Некорректная тема.")
        clean: dict[str, Any] = {}
        for key in ("bg_color", "bg_gradient_to", "accent_color"):
            if key in theme:
                if not HEX_COLOR.match(str(theme[key])):
                    raise ApiError(400, f"Цвет {key} должен быть в формате #RRGGBB.")
                clean[key] = str(theme[key])
        for key, allowed in THEME_ENUMS.items():
            if key in theme:
                if theme[key] not in allowed:
                    raise ApiError(400, f"Недопустимое значение {key}.")
                clean[key] = theme[key]
        if "menu_order" in theme:
            order = theme["menu_order"]
            if not isinstance(order, list) or sorted(order) != sorted(MENU_KEYS):
                raise ApiError(400, "Порядок меню должен содержать все пункты ровно по одному разу.")
            clean["menu_order"] = order
        if "bg_image_data_url" in theme:
            value = theme["bg_image_data_url"]
            if value:
                if len(str(value)) > MAX_BG_IMAGE_CHARS:
                    raise ApiError(413, "Фоновая картинка слишком большая — уменьшите её.")
                decode_data_url(str(value), MAX_BG_IMAGE_CHARS)
                clean["bg_image_data_url"] = str(value)
            else:
                clean["bg_image_data_url"] = None
        for key in ("bg_image_pos_x", "bg_image_pos_y"):
            if key in theme:
                clean[key] = _int(theme[key], key, 0, 100)
        return {"theme": self.db.set_theme(clean)}

    # ------------------------------------------------------------ media
    def media_file_id(self, kind: str, ident: str) -> str | None:
        """file_id в Telegram для картинок, которые фронтенд показывает через
        /api/media/... (фото товаров из бота, шапка каталога, QR оплаты).
        Скриншоты оплат сюда намеренно НЕ входят — они видны только админу в Telegram."""
        if kind == "product":
            product = self.db.get_product(int(ident)) if ident.isdigit() else None
            return product["photo_file_id"] if product and product["is_active"] else None
        if kind == "qr" and ident in ("sbp", "crypto"):
            return self.db.get_payment_qr(ident)
        if kind == "catalog":
            return self.db.get_catalog_photo()
        if kind == "payqr" and ident.isdigit():
            code = self.db.get_payment_qr_code(int(ident))
            return code["file_id"] if code else None
        return None
