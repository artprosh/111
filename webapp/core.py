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
                "chat": {str(k): v for k, v in self.db.order_chat_unread(
                    [int(o["id"]) for o in self.db.get_courier_active_orders(user.id)], True).items()},
                # «Вам назначили заказ» — окно, которое нельзя пропустить, пока не нажмёт «Понятно».
                "assigned": [{"id": int(o["id"]), "address": o["delivery_address"], "region": REGION_LABELS.get(o["region"] or "")}
                             for o in self.db.courier_unacked_assignments(user.id)],
            }
        if self.is_admin(user.id):
            rows = self.db.get_admin_open_orders()
            new = sum(1 for o in rows if o["status"] == "new")
            problem = sum(1 for o in rows if o["status"] == "not_delivered")
            data["admin"] = {
                "new": new, "not_delivered": problem, "attention": new + problem,
                "shift_requests": len(self.db.shift_requests()),  # «В путь!» ждёт подтверждения
                "reload_requests": len(self.db.reload_requests()),
                "alerts": len(self._admin_alerts()),
                "chats": self.db.support_unread_for_admin(),  # покупатели, ждущие ответа в чате
                "disputed": len(self.db.get_disputed_orders()),
                # заявки «🤝 Реферальная программа», ждущие решения: 💼 партнёрские, 🎁 бонусные
                "referral": self.db.count_pending_referral_requests(),
            }
        return data

    async def catalog(self, user, body, query) -> dict[str, Any]:
        photo = self.db.get_catalog_photo()
        return {
            "products": [self.product_json(p) for p in self.db.get_products()],
            "banner": f"/api/media/catalog/main?v={_short(photo)}" if photo else None,
        }

    # ------------------------------------------------------------ cart
    def cart_payload(self, user_id: int) -> dict[str, Any]:
        items = self.db.get_cart(user_id)
        lines = []
        gifts: dict[int, dict[str, Any]] = {}  # 🎁 подарки к товарам (28.10.2026) — как будут в заказе
        for i in items:
            if i["gift_id"] is not None:
                g = gifts.setdefault(int(i["gift_id"]), {"product_id": int(i["gift_id"]), "name": i["gift_name"], "quantity": 0})
                g["quantity"] += int(i["quantity"]) * max(1, int(i["bonus_qty"] or 1))
            lines.append(
                {
                    "product_id": int(i["product_id"]),
                    "variant": i["variant"],
                    "name": i["name"],
                    "quantity": int(i["quantity"]),
                    "price": int(i["price"]),
                    "line_total": int(i["line_total"]),
                    "bonus_text": i["bonus_text"],
                    "bonus_desc": (i["bonus_desc"] if "bonus_desc" in i.keys() else None) or "",
                }
            )
        return {
            "items": lines,
            "gifts": list(gifts.values()),
            "total": sum(x["line_total"] for x in lines),
            "count": sum(x["quantity"] for x in lines),
            "has_exchange": any(x["variant"] == "exchange" for x in lines),
            # Баллонов в корзине — столько подъёмов на этаж можно выбрать максимум (28.10.2026).
            "balloons": sum(x["quantity"] for x in lines if is_balloon(x["name"])),
        }

    async def cart(self, user, body, query) -> dict[str, Any]:
        return self.cart_payload(user.id)

    async def cart_set(self, user, body, query) -> dict[str, Any]:
        product_id = _int(body.get("product_id"), "товар", 1)
        variant = body.get("variant", "new")
        if variant not in ("new", "exchange"):
            raise ApiError(400, "Некорректный вариант товара.")
        quantity = _int(body.get("quantity"), "количество", 0, 999)
        if quantity == 0:
            self.db.remove_cart_item(user.id, product_id, variant)
        else:
            try:
                self.db.set_cart_item(user.id, product_id, variant, quantity)
            except ValueError as exc:
                raise ApiError(409, str(exc))
        return self.cart_payload(user.id)

    async def cart_clear(self, user, body, query) -> dict[str, Any]:
        self.db.clear_user_cart(user.id)
        self.quotes.pop(user.id, None)
        return self.cart_payload(user.id)

    # ------------------------------------------------------------ checkout
    async def checkout_info(self, user, body, query) -> dict[str, Any]:
        saved = _row(self.db.get_user(user.id))
        return {
            "name": user.display_name,
            "phone": saved.get("last_phone") or "",
            "address": saved.get("last_address") or "",
            "payment_methods": self.available_payment_methods(),
            "pay_on_delivery_warning": PAY_ON_DELIVERY_WARNING,
            "blocked_text": BLOCKED_TEXT if self.db.is_user_blocked(user.id) else None,
            "crypto_options": await self._crypto_options(),
            "cart": self.cart_payload(user.id),
            "floor_lift_price": self.db.get_floor_lift_price(),
            # 01.11.2026: возврат пустых баллонов в этом же заказе (покупка + возврат)
            "return_items": (await self.return_options(user, {}, {}))["items"],
            # «⬇️ Спуск пустого баллона» в возврате: цена за 1 спуск (= ставка курьеру), 0 — услуги нет.
            "descent_price": self.db.courier_rates()["descent"],
            "bonus": self._bonus_short(user.id),
            # Партнёрские баллы (1 балл = 1 ₽) — можно оплатить ими товары; только у партнёров.
            "partner_points": self.db.partner_points_balance(user.id) if self.db.get_partner(user.id) else None,
            "customer_name": (saved.get("last_customer_name") or "").split(" (@")[0] or user.first_name or "",
            "saved_addresses": self.db.saved_addresses(user.id),
            "phone_formatted": _safe_phone(saved.get("last_phone")),
            "address_suggest": self._geo_ready(),
            "address_attribution": ADDRESS_ATTRIBUTION if self._geo_ready() else None,
            # «Доставка ко времени»: только сегодня (по Москве), шаг 15 минут, не раньше чем через 2 часа.
            "time_slots": delivery_time_slots(),
            "comment_max": ORDER_COMMENT_MAX,
        }

    def _lift_floors(self, body: dict[str, Any], user_id: int | None = None) -> tuple[int, int]:
        """Подъём на этаж (28.10.2026): цена — за подъём одного баллона, покупатель выбирает,
        сколько подъёмов (0 — «заберу сам», не больше числа баллонов в заказе). Хранится в
        orders.floor_lift_floors (число подъёмов) × floor_lift_price. Старое приложение
        присылает «да/нет» — это 1 подъём."""
        raw = body.get("floor_lift_count")
        if raw is not None:
            floors = _int(raw, "Количество подъёмов", 0, 999)
            if floors and user_id is not None:
                balloons = self.cart_payload(user_id)["balloons"]
                if floors > balloons:
                    raise ApiError(400, f"Подъёмов не может быть больше, чем баллонов в заказе ({balloons}).")
        else:
            raw = body.get("floor_lift")
            if raw is None:
                raw = body.get("floor_lift_floors")  # совместимость со старой версией приложения
            floors = 1 if raw and str(raw).strip().lower() not in ("0", "false", "no", "") else 0
        price = self.db.get_floor_lift_price()
        if floors and price <= 0:
            raise ApiError(409, "Услуга подъёма на этаж сейчас недоступна — уберите её из заказа.")
        return floors, price

    def _method_available(self, method: str) -> bool:
        """Наличные — всегда; СБП/крипта — если переключатель в админке включён и
        админ добавил хотя бы один QR этого вида (курьер показывает QR на месте)."""
        if method == "cash_on_delivery":
            return True
        if not self.db.get_payment_switches().get(method, True):
            return False
        return bool(self.db.get_payment_qr_codes(method))

    async def _crypto_options(self) -> list[dict[str, Any]]:
        """Кнопки «Выберите валюту» для покупателя: все крипто-QR из админки,
        с текущим курсом (если доступен) — приложение показывает примерную
        сумму; точная фиксируется при оформлении заказа."""
        codes = self.db.get_payment_qr_codes("crypto")
        if not codes:
            return []
        try:
            rates = await self.bridge.get_crypto_rates(sorted({c["currency"] for c in codes if c["currency"]}))
        except Exception:
            logger.exception("Mini App: курсы криптовалют недоступны")
            rates = {}
        options = []
        for c in codes:
            cur = c["currency"] or ""
            info = crypto.CURRENCIES.get(cur)
            options.append({
                "id": c["id"],
                "emoji": self.qr_emoji(c),
                "label": self.qr_label(c),
                "currency": cur,
                "network": c["network"],
                "rate": rates.get(cur),
                "decimals": info[1] if info else 2,
            })
        return options

    def _chosen_qr(self, method: str, raw) -> int | None:
        """QR, по которому покупатель будет платить. Крипта — тот, что он выбрал
        (если кошелёк один — он сам); СБП — первый QR СБП из админки."""
        codes = self.db.get_payment_qr_codes(method)
        if method == "sbp":
            return int(codes[0]["id"]) if codes else None
        if raw in (None, ""):
            if len(codes) == 1:
                return int(codes[0]["id"])
            raise ApiError(400, "Выберите валюту и сеть для оплаты.")
        try:
            code_id = int(raw)
        except (TypeError, ValueError):
            raise ApiError(400, "Выберите валюту и сеть для оплаты.")
        if not any(int(c["id"]) == code_id for c in codes):
            raise ApiError(409, "Эта валюта сейчас недоступна — выберите другую.")
        return code_id

    async def _crypto_quotes(self, total: int) -> dict[str, Any]:
        """Суммы во всех монетах, для которых есть кошельки, по курсу «сейчас»."""
        codes = sorted({c["currency"] for c in self.db.get_payment_qr_codes("crypto") if c["currency"]})
        if not codes:
            return {}
        try:
            rates = await self.bridge.get_crypto_rates(codes)
        except Exception:
            logger.exception("Mini App: курсы криптовалют недоступны")
            return {}
        return crypto.build_quotes(total, rates)

    async def return_options(self, user, body, query) -> dict[str, Any]:
        """«Возврат баллона»: какие баллоны принимаются и сколько за них отдаёт курьер."""
        return {"items": [{"id": p["id"], "name": p["name"], "return_price": int(p["return_price"]),
                           "return_points": int(p["return_points"] or 0),
                           "photo": self.product_json(p)["photo"]} for p in self.db.return_products()]}

    async def create_order(self, user, body, query) -> dict[str, Any]:
        """Оформление заказа. СБП и криптовалюта теперь — оплата ПРИ КУРЬЕРЕ:
        без QR и скриншота у покупателя, заказ сразу уходит курьерам; суммы в
        криптовалютах фиксируются по курсу в момент оформления."""
        if self.db.is_user_blocked(user.id):  # чёрный список — проверка на сервере
            raise ApiError(403, BLOCKED_TEXT)
        # «Возврат баллона» (25.10.2026): тот же адрес и время, но без корзины и оплаты —
        # курьер забирает пустые баллоны и отдаёт покупателю их цену возврата.
        is_return = body.get("kind") == "return"
        return_items: list[tuple[int, int]] = []
        if is_return:
            method, qr_id = "cash_on_delivery", None
            return_items = self._return_items(body.get("return_items"))
        else:
            method = body.get("payment_method")
            if method not in PAYMENT_METHODS:
                raise ApiError(400, "Выберите способ оплаты.")
            if not self._method_available(method):
                raise ApiError(409, "Этот способ оплаты сейчас недоступен.")
            qr_id = self._chosen_qr(method, body.get("payment_qr_id")) if method != "cash_on_delivery" else None
        phone = normalize_phone(body.get("phone"))
        lat = body.get("lat")
        lon = body.get("lon")
        parts = None
        address_is_location = lat is not None and lon is not None
        if address_is_location:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                raise ApiError(400, "Некорректные координаты.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ApiError(400, "Некорректные координаты.")
            # Точно как в боте (address_location_shared).
            address = f"📍 Геопозиция клиента (координаты {lat:.6f}, {lon:.6f})"
            extra = _optional_text(body.get("address"), 300)
            if extra:
                address += f"\nКомментарий: {extra}"
        elif isinstance(body.get("address_parts"), dict):
            # Новое оформление: «Город, улица» (+ подсказки из своей базы OSM), дом и
            # подъезд / квартира / этаж / домофон; точка с карты — по желанию.
            parts = self._address_parts(body["address_parts"])
            address = compose_address(parts)
            lat = lon = None
            point = body.get("point")
            if isinstance(point, dict) and point.get("lat") is not None and point.get("lon") is not None:
                lat, lon = self._coords(point.get("lat"), point.get("lon"))
            elif parts.get("street_id") and parts.get("house") and self._geo_ready():
                # Улица из подсказки + номер дома → точка из базы (курьер увидит её на карте).
                found = self.geo.house_point(int(parts["street_id"]), parts["house"], parts.get("building"))
                if found:
                    lat, lon = found["lat"], found["lon"]
        else:
            lat = lon = None
            address = _text(body.get("address"), "Адрес", 10, 300)
            parts = None
        typed_name = _optional_text(body.get("customer_name"), 64)
        if typed_name is not None and len(typed_name) < 2:
            raise ApiError(400, "Как к вам обращаться — минимум 2 буквы.")
        name = typed_name if typed_name else user.display_name  # 01.11.2026: @логин в имя заказа не пишем
        floors, _lift_price = self._lift_floors(body, user.id) if not is_return else (0, 0)
        # ⬇️ Спуск пустого баллона в обычном заказе убран (владелец 28.10.2026, 17:07): при «Замене» покупатель
        # за спуск не платит; спуск — только в отдельной услуге «Возврат баллона». descent_count здесь не принимаем.
        bonus_spend = 0 if is_return else _int(body.get("bonus_spend") or 0, "бонусы", 0, 100_000_000)
        points_spend = 0 if is_return else _int(body.get("points_spend") or 0, "баллы", 0, 100_000_000)
        comment = str(body.get("comment") or "").strip() or None
        if comment and len(comment) > ORDER_COMMENT_MAX:
            raise ApiError(400, f"Комментарий — не длиннее {ORDER_COMMENT_MAX} символов.")
        deliver_at = None
        if body.get("deliver_time"):
            deliver_at = deliver_at_from_slot(body.get("deliver_time"))
            if deliver_at is None:
                raise ApiError(409, "Это время уже недоступно. Выберите другое время доставки.")
        if not is_return and not self.db.get_cart(user.id):
            raise ApiError(409, "Корзина пуста. Добавьте товар и попробуйте снова.")
        from db import BonusError  # импорт здесь: ядро не зависит от db при импорте

        # Доставка в Москве — как обычно; в другой город — индивидуальный заказ:
        # курьерам не рассылается, администратор свяжется с покупателем.
        line = (parts or {}).get("line") if not address_is_location else None
        if not address_is_location and not parts:
            line = address
        moscow = self._is_moscow(line, lat, lon, (parts or {}).get("street_id"))
        individual = moscow is False
        # 30.10.2026: за пределы Москвы — предоплата за доставку. Окно с суммой и «Да, готов» показывает
        # интерфейс; без согласия (prepay_agreed) заказ не создаётся. Не посчиталось — индивидуальный заказ.
        outside_quote = None
        if individual and not is_return:
            outside_quote = await self._outside_quote(lat, lon)
            if outside_quote and not body.get("prepay_agreed"):
                raise ApiError(409, "Доставка за пределы Москвы — по предоплате. Подтвердите, что готовы внести предоплату.",
                               {"code": "prepay_required", "prepay": outside_quote})
        mixed_returns: list[tuple[int, int]] = []
        if not is_return and body.get("return_items"):
            mixed_returns = self._return_items(body.get("return_items"))
        try:
            if is_return:
                try:
                    descents = _int(body.get("descent_count") or 0, "Количество спусков", 0, 999)
                    if descents > sum(q for _pid, q in return_items):
                        raise ApiError(400, "Спусков не может быть больше, чем баллонов в возврате.")
                    order_id = self.db.create_return_order(user.id, name, phone, address, lat, lon, return_items,
                                                           individual=individual, descent_count=descents,
                                                           mode="points" if body.get("return_mode") == "points" else "cash")
                except ValueError as exc:
                    raise ApiError(409, str(exc))
            else:
                order_id = self.db.create_order_from_cart(
                    user.id, name, phone, address, lat, lon, payment_method=method,
                    floor_lift_floors=floors, bonus_spend=bonus_spend, individual=individual,
                    points_spend=points_spend, return_items=mixed_returns,
                    return_mode="points" if body.get("return_mode") == "points" else "cash",
                )
        except BonusError as exc:
            if exc.reason == "points_balance":
                raise ApiError(409, f"У вас {exc.limit} баллов — списать больше нельзя.")
            if exc.reason == "points_too_much":
                raise ApiError(409, f"Баллами можно оплатить только товары и не всю сумму: сейчас не больше {exc.limit}.")
            if exc.reason == "balance":
                raise ApiError(409, f"У вас {exc.limit} бонусов — списать больше нельзя.")
            raise ApiError(409, f"Бонусами можно оплатить только товары и не всю сумму: не больше {exc.limit} ₽.")
        except ValueError:
            raise ApiError(409, "Корзина пуста. Добавьте товар и попробуйте снова.")
        if comment or deliver_at:
            self.db.set_order_extras(order_id, comment, deliver_at)
        if individual and not is_return and outside_quote:
            # За пределами Москвы: доставка посчитана — обычный заказ; её оплачивает предоплата курьеру.
            self.db.set_outside_delivery(order_id, outside_quote["fee"], outside_quote["km"], outside_quote.get("per_km"))
            self.db.set_prepaid(order_id)
        # Регион курьеров по точке дома (Центр — внутри ТТК, иначе Север/Восток/Юг/Запад).
        region = self._order_region(lat, lon, address) if not individual else None
        if region:
            self.db.set_order_region(order_id, region)
        self.db.save_last_delivery_info(user.id, typed_name or name, phone, None if address_is_location else address)
        if not address_is_location and parts:
            self.db.remember_address(user.id, {**parts, "lat": lat, "lon": lon, "moscow": moscow})
        if method != "cash_on_delivery":
            quotes = {}
            if method == "crypto":
                quotes = await self._crypto_quotes(int(self.db.get_order(order_id)["total"]))
            self.db.set_order_pay_on_delivery(order_id, crypto.dumps(quotes) if quotes else None, qr_id)
            usdt = quotes.get("USDT")
            if usdt:  # для старых текстов бота (сумма в USDT)
                self.db.set_order_usdt(order_id, float(usdt["amount"]), float(usdt["rate"]))
        await self._safe(self.bridge.dispatch_new_order(order_id), f"рассылка заказа {order_id}", order_id)
        order = self.db.get_order(order_id)
        return {"order": self.order_json(order, "customer")}

    @staticmethod
    def _return_items(raw: Any) -> list[tuple[int, int]]:
        """[{product_id, quantity}] — баллоны на возврат, 1–20 штук каждого."""
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "Выберите баллоны для возврата.")
        out: dict[int, int] = {}
        for it in raw[:20]:
            if not isinstance(it, dict):
                raise ApiError(400, "Некорректный список баллонов.")
            pid = _int(it.get("product_id"), "баллон", 1)
            qty = _int(it.get("quantity"), "количество", 0, 20)
            if qty:
                out[pid] = out.get(pid, 0) + qty
        if not out:
            raise ApiError(400, "Выберите баллоны для возврата.")
        return list(out.items())

    @staticmethod
    def _coords(lat: Any, lon: Any) -> tuple[float, float]:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise ApiError(400, "Некорректные координаты.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ApiError(400, "Некорректные координаты.")
        return lat, lon

    @staticmethod
    def _address_parts(raw: dict[str, Any]) -> dict[str, Any]:
        line = _text(raw.get("line"), "Город, улица", 3, 200)
        parts: dict[str, Any] = {"line": line}
        for key, label, digits in ADDRESS_EXTRA_FIELDS:
            value = _optional_text(raw.get(key), 20 if digits else 30)
            if not value:
                continue
            if digits and not value.isdigit():
                raise ApiError(400, f"Поле «{ADDRESS_FIELD_NAMES[key]}» — только цифры.")
            parts[key] = value
        # Дом обязателен, если его нет в строке адреса (старые сохранённые адреса — одной строкой).
        if not parts.get("house") and not re.search(r"\d", line):
            raise ApiError(400, "Укажите номер дома.")
        if raw.get("street_id") not in (None, ""):
            try:
                parts["street_id"] = int(raw["street_id"])
            except (TypeError, ValueError):
                raise ApiError(400, "Некорректная улица.")
        return parts

    async def delete_saved_address(self, user, body, query, index: int) -> dict[str, Any]:
        """Красный ✕ у сохранённого адреса при оформлении. line — защита от удаления не того адреса."""
        items = self.db.saved_addresses(user.id)
        line = _optional_text(body.get("line"), 200)
        if index >= len(items) or (line and items[index].get("line") != line):
            raise ApiError(409, "Список адресов изменился — откройте оформление заново.")
        self.db.forget_address(user.id, index)
        return {"saved_addresses": self.db.saved_addresses(user.id)}

    # ------------------------------------------------------------ адрес: своя база OpenStreetMap
    def _order_region(self, lat: float | None, lon: float | None, address: str | None) -> str | None:
        """Регион заказа: по точке дома; точки нет — находим дом/улицу по тексту адреса."""
        if not self._geo_ready():
            return None
        if lat is None or lon is None:
            point = self.geo.locate_address(address)
            if not point:
                return None
            lat, lon = point
        return self.geo.region(lat, lon)

    def _backfill_regions(self, rows) -> None:
        """Открытые заказы без региона (оформлены до загрузки карты ТТК или без точки) —
        определить регион сейчас, чтобы они разошлись по кнопкам регионов у админа."""
        if not self._geo_ready():
            return
        for o in rows:
            o = _row(o)
            if o.get("region") or int(o.get("individual") or 0) > 0 or o.get("status") not in ("new", "accepted", "in_transit"):
                continue
            region = self._order_region(o.get("location_lat"), o.get("location_lon"), o.get("delivery_address"))
            if region:
                self.db.set_order_region(int(o["id"]), region)

    def _geo_ready(self) -> bool:
        return bool(self.geo is not None and self.geo.loaded)

    async def address_suggest(self, user, body, query) -> dict[str, Any]:
        """Подсказки «Город, улица» из своей базы — ничего никуда не отправляется."""
        q = (query.get("q") or "").strip()
        if not self._geo_ready():
            return {"enabled": False, "suggestions": []}
        if len(q) < 2:
            return {"enabled": True, "suggestions": []}
        items = await asyncio.to_thread(self.geo.suggest, q[:100])
        return {"enabled": True, "suggestions": items, "attribution": ADDRESS_ATTRIBUTION}

    async def address_geolocate(self, user, body, query) -> dict[str, Any]:
        """«📍 Выбрать на карте»: ближайший дом из базы (до 300 м) и Москва ли это."""
        lat, lon = self._coords(query.get("lat"), query.get("lon"))
        if not self._geo_ready():
            return {"enabled": False, "address": None, "moscow": None, "reason": "not_configured"}
        found = await asyncio.to_thread(self.geo.nearest, lat, lon)
        moscow = self.geo.point_in_moscow(lat, lon)
        if not found:
            return {"enabled": True, "address": None, "moscow": moscow, "reason": "empty"}
        return {"enabled": True, "address": found["street"], "street_id": found["street_id"],
                "house": found["house"], "moscow": moscow if moscow is not None else found["moscow"]}

    def _is_moscow(self, line: str | None, lat: float | None, lon: float | None,
                   street_id: int | None = None) -> bool | None:
        """Доставка по Москве или в другой город: по точке (граница Москвы из OSM),
        по выбранной улице из базы, иначе — по слову «Москва» в адресе."""
        if self._geo_ready():
            if lat is not None and lon is not None:
                inside = self.geo.point_in_moscow(lat, lon)
                if inside is not None:
                    return inside
            if street_id:
                st = self.geo.street(int(street_id))
                if st:
                    return st["moscow"]
        if line:
            return text_is_moscow(line)
        return None

    # ------------------------------------------------------------ customer orders
    def _own_order(self, user_id: int, order_id: int):
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != user_id:
            raise ApiError(404, "Заказ не найден.")
        return order

    async def my_orders(self, user, body, query) -> dict[str, Any]:
        scope = query.get("scope", "active")
        statuses = CUSTOMER_ACTIVE_STATUSES if scope == "active" else CUSTOMER_DONE_STATUSES
        orders = [o for o in self.db.get_user_orders(user.id) if o["status"] in statuses]
        result = self.orders_json(orders, "customer")
        if scope != "active":
            rated = self._rated_order_ids([o["id"] for o in result])
            for o in result:
                o["can_rate"] = o["status"] == "delivered" and o["courier"] is not None and o["id"] not in rated
        payload: dict[str, Any] = {"orders": result}
        if scope != "active":
            payload["purchase_count"] = self.db.get_user_purchase_count(user.id)
        return payload

    def _rated_order_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        with self.db.connection() as conn:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT order_id FROM courier_ratings WHERE order_id IN ({marks})", ids
            ).fetchall()
        return {int(r["order_id"]) for r in rows}

    async def cancel_order(self, user, body, query, order_id: int) -> dict[str, Any]:
        mine = self._own_order(user.id, order_id)
        if mine and int(_row(mine).get("prepay_amount") or 0) > 0:
            raise ApiError(409, "По заказу внесена предоплата — отмена только через администратора.")
        order = self.db.cancel_order(order_id, user.id)
        if not order:
            raise ApiError(409, "Заказ уже принят курьером или отменён.")
        await self._safe(self.bridge.customer_cancelled(order_id), f"отмена заказа {order_id}", order_id)
        return {"order": self.order_json(order, "customer")}

    async def order_received(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Раньше — «Получил заказ» у покупателя. Убрано: если покупатель
        закрывал заказ сам, пока курьер вёз его, у курьера заказ пропадал и он
        не мог нажать «Доставлен». Теперь доставку закрывает только курьер."""
        self._own_order(user.id, order_id)
        raise ApiError(409, "Получение заказа подтверждает курьер — дождитесь его, пожалуйста.")

    async def rate_order(self, user, body, query, order_id: int) -> dict[str, Any]:
        self._own_order(user.id, order_id)
        rating = _int(body.get("rating"), "оценка", 1, 5)
        order = self.db.rate_courier(order_id, user.id, rating)
        if not order:
            raise ApiError(409, "Оценка уже сохранена или заказ нельзя оценить.")
        return {"ok": True, "rating": rating}

    async def call_admin(self, user, body, query) -> dict[str, Any]:
        text = _text(body.get("text"), "Сообщение", 1, 2000)
        self.db.add_admin_call(user.id, text)  # висит в уведомлениях админа, пока не «Решено»
        try:
            await self.bridge.call_admin(user, text)
        except Exception:
            logger.exception("Mini App: не удалось переслать вопрос администратору")
            raise ApiError(502, "Не удалось отправить сообщение администратору. Попробуйте позже.")
        return {"ok": True}

    # ------------------------------------------------------------ чат с администрацией
    CHAT_TEXT_MAX = 2000
    CHAT_PHOTO_MAX = 3_000_000

    def _chat_payload(self, body: dict[str, Any], user_id: int) -> tuple[str | None, tuple[bytes, str] | None, int | None]:
        text = str(body.get("text") or "").strip() or None
        if text and len(text) > self.CHAT_TEXT_MAX:
            raise ApiError(400, f"Сообщение — не длиннее {self.CHAT_TEXT_MAX} символов.")
        photo = decode_data_url(body["photo"], self.CHAT_PHOTO_MAX) if body.get("photo") else None
        if not text and not photo:
            raise ApiError(400, "Напишите сообщение или прикрепите фото.")
        order_id = None
        if body.get("order_id"):
            oid = _int(body.get("order_id"), "заказ", 1)
            order = self.db.get_order(oid)
            if order and int(order["user_id"]) == int(user_id):  # только заказ этого покупателя
                order_id = oid
        return text, photo, order_id

    @staticmethod
    def _chat_msg(m: dict[str, Any], view: str, admin_names: dict[int, str] | None = None) -> dict[str, Any]:
        out = {"id": int(m["id"]), "text": m.get("text"), "photo": bool(m.get("has_photo")),
               "order_id": m.get("order_id"), "at": m["created_at"], "from_admin": bool(m["from_admin"]),
               "read": bool(m.get("read_at"))}
        out["mine"] = out["from_admin"] if view == "admin" else not out["from_admin"]
        if view == "admin" and out["from_admin"] and admin_names is not None:
            out["admin_name"] = admin_names.get(int(m.get("admin_id") or 0))  # видит только админ
        return out

    def _too_fast(self, user_id: int, from_admin: bool) -> bool:
        recent = [m for m in self.db.support_messages(user_id, 20) if bool(m["from_admin"]) == from_admin]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        return sum(1 for m in recent if datetime.fromisoformat(m["created_at"]) > cutoff) >= 15

    async def chat_get(self, user, body, query) -> dict[str, Any]:
        """Чат покупателя с «Администрацией»: кто из админов ответил — не видно."""
        msgs = self.db.support_messages(user.id)
        self.db.mark_support_read(user.id, by_admin=False)
        return {"messages": [self._chat_msg(m, "customer") for m in msgs]}

    async def chat_send(self, user, body, query) -> dict[str, Any]:
        text, photo, order_id = self._chat_payload(body, user.id)
        if self._too_fast(user.id, False):
            raise ApiError(429, "Слишком много сообщений подряд. Подождите минуту.")
        self.db.add_support_message(user.id, False, text, order_id=order_id, photo=photo)
        await self._safe(self.bridge.support_message(user, text, bool(photo)), "чат: уведомление админам")
        return await self.chat_get(user, body, query)

    async def chat_photo(self, user, body, query, message_id: int) -> dict[str, Any]:
        """Фото из чата — только участникам переписки (покупателю — своё, админу — любое)."""
        found = self.db.support_photo(message_id)
        if not found or (found[0] != user.id and not self.is_admin(user.id)):
            raise ApiError(404, "Фото не найдено.")
        _uid, data, mime = found
        return {"src": f"data:{mime};base64,{base64.b64encode(data).decode()}"}

    def _admin_names(self) -> dict[int, str]:
        names = {}
        for aid in self.admin_ids:
            u = _row(self.db.get_user(aid))
            names[aid] = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or f"ID {aid}"
        return names

    async def admin_chats(self, user, body, query) -> dict[str, Any]:
        threads = []
        for t in self.db.support_threads():
            name = " ".join(p for p in (t.get("first_name"), t.get("last_name")) if p) or f"ID {t['user_id']}"
            threads.append({"user_id": int(t["user_id"]), "name": name, "is_courier": self.courier_of(int(t["user_id"])) is not None, "username": t.get("username"),
                            "last": (t.get("text") or ("📷 Фото" if t.get("has_photo") else "")), "last_from_admin": bool(t["from_admin"]),
                            "at": t["created_at"], "unread": int(t["unread"] or 0)})
        return {"threads": threads}

    async def admin_chat_get(self, user, body, query, user_id: int) -> dict[str, Any]:
        u = _row(self.db.get_user(user_id))
        if not u:
            raise ApiError(404, "Покупатель не найден.")
        msgs = self.db.support_messages(user_id)
        self.db.mark_support_read(user_id, by_admin=True)
        names = self._admin_names()
        orders = [{"id": int(o["id"]), "status_label": STATUS_LABELS.get(o["status"], o["status"])}
                  for o in self.db.get_user_orders(user_id)][:5]
        return {"is_courier": self.courier_of(user_id) is not None, "customer": {"user_id": user_id, "name": " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or f"ID {user_id}",
                             "username": u.get("username"), "phone": _safe_phone(u.get("last_phone")) if u.get("last_phone") else None},
                "orders": orders, "messages": [self._chat_msg(m, "admin", names) for m in msgs]}

    async def admin_chat_delete(self, user, body, query, user_id: int) -> dict[str, Any]:
        """✕ у чата: удалить всю переписку с покупателем (если он напишет снова — новый чат)."""
        n = self.db.delete_support_chat(user_id)
        if not n:
            raise ApiError(404, "Чат не найден.")
        u = _row(self.db.get_user(user_id))
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or f"ID {user_id}"
        self._log_admin(user, f"Удалён чат с покупателем ({n} сообщ.)", target_user_id=user_id, target_name=name)
        return await self.admin_chats(user, body, query)

    async def admin_chat_send(self, user, body, query, user_id: int) -> dict[str, Any]:
        """Ответ от имени «Администрация» — покупатель не видит, кто из админов написал."""
        if not self.db.get_user(user_id):
            raise ApiError(404, "Покупатель не найден.")
        text, photo, order_id = self._chat_payload(body, user_id)
        first_unread = self.db.support_unread_for_user(user_id) == 0
        self.db.add_support_message(user_id, True, text, admin_id=user.id, order_id=order_id, photo=photo)
        if first_unread:  # одно уведомление в бот, пока покупатель не прочитал
            await self._safe(self.bridge.support_reply(user_id), "чат: уведомление покупателю")
        self.db.mark_support_read(user_id, by_admin=True)
        return await self.admin_chat_get(user, body, query, user_id)

    # ------------------------------------------------------------ чат курьера и покупателя в заказе
    ORDER_CHAT_OPEN = ("accepted", "in_transit")
    # Чат виден в приложении, пока заказ не доставлен; после доставки / отмены — только в Excel.
    ORDER_CHAT_VISIBLE = ("accepted", "in_transit", "not_delivered", "disputed")

    def _order_chat_role(self, user, order, want: str | None) -> str:
        """Кто открыл чат заказа: покупатель (свой заказ), курьер (заказ у него) или админ (только читает)."""
        roles = []
        if int(order["user_id"]) == int(user.id):
            roles.append("customer")
        courier = self.courier_of(user.id)
        if courier is not None and order["courier_id"] is not None and int(order["courier_id"]) == int(courier["id"]):
            roles.append("courier")
        if self.is_admin(user.id):
            roles.append("admin")
        if not roles:
            raise ApiError(404, "Заказ не найден.")
        return want if want in roles else roles[0]

    def _order_chat_json(self, order, role: str) -> dict[str, Any]:
        msgs = self.db.order_messages(int(order["id"]))
        if role in ("customer", "courier"):
            self.db.mark_order_chat_read(int(order["id"]), by_courier=role == "courier")
        out = []
        for m in msgs:
            from_courier = bool(m["from_courier"])
            out.append({"id": int(m["id"]), "text": m.get("text"), "photo": bool(m.get("has_photo")), "at": m["created_at"],
                        "from_courier": from_courier, "read": bool(m.get("read_at")),
                        "mine": (role == "courier") == from_courier if role != "admin" else False})
        o = _row(order)
        return {"order_id": int(order["id"]), "role": role, "status": o.get("status"),
                "courier_name": o.get("courier_name"), "can_send": role != "admin" and o.get("status") in self.ORDER_CHAT_OPEN
                and o.get("courier_id") is not None, "messages": out}

    async def order_chat_get(self, user, body, query, order_id: int) -> dict[str, Any]:
        order = self.db.get_order(order_id)
        if not order:
            raise ApiError(404, "Заказ не найден.")
        if order["status"] not in self.ORDER_CHAT_VISIBLE:
            self._order_chat_role(user, order, query.get("as"))  # чужим — «не найден», как раньше
            raise ApiError(410, "Заказ закрыт — чат удалён из приложения. Переписка сохранена в выгрузке Excel.")
        return self._order_chat_json(order, self._order_chat_role(user, order, query.get("as")))

    async def order_chat_send(self, user, body, query, order_id: int) -> dict[str, Any]:
        order = self.db.get_order(order_id)
        if not order:
            raise ApiError(404, "Заказ не найден.")
        role = self._order_chat_role(user, order, body.get("as") or query.get("as"))
        if role == "admin":
            raise ApiError(403, "Администратор переписку курьера и покупателя только читает.")
        if order["status"] not in self.ORDER_CHAT_OPEN or order["courier_id"] is None:
            raise ApiError(409, "Чат открыт, пока курьер везёт заказ.")
        text, photo, _ = self._chat_payload(body, int(order["user_id"]))
        from_courier = role == "courier"
        recent = [m for m in self.db.order_messages(order_id, 20) if bool(m["from_courier"]) == from_courier]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        if sum(1 for m in recent if datetime.fromisoformat(m["created_at"]) > cutoff) >= 15:
            raise ApiError(429, "Слишком много сообщений подряд. Подождите минуту.")
        # Уведомление в бот — одно, пока вторая сторона не прочитала.
        first_unread = not self.db.order_chat_unread([order_id], for_courier=not from_courier)
        self.db.add_order_message(order_id, from_courier, text, courier_id=int(order["courier_id"]) if from_courier else None, photo=photo)
        if first_unread:
            await self._safe(self.bridge.order_chat(order_id, not from_courier, text, bool(photo)), "чат заказа: уведомление", order_id)
        return self._order_chat_json(self.db.get_order(order_id), role)

    async def order_chat_photo(self, user, body, query, message_id: int) -> dict[str, Any]:
        found = self.db.order_message_photo(message_id)
        if not found:
            raise ApiError(404, "Фото не найдено.")
        order = self.db.get_order(found[0])
        if not order:
            raise ApiError(404, "Фото не найдено.")
        self._order_chat_role(user, order, None)  # только участники заказа и админ
        if order["status"] not in self.ORDER_CHAT_VISIBLE and not self.is_admin(user.id):
            raise ApiError(410, "Заказ закрыт — фото доступно только в выгрузке Excel.")
        return {"src": f"data:{found[2]};base64,{base64.b64encode(found[1]).decode()}"}

    async def admin_order_chat(self, user, body, query, order_id: int) -> dict[str, Any]:
        """История заказов: переписка курьера и покупателя по заказу — админ только читает, в любом статусе
        (в приложении у курьера и покупателя чат исчезает после доставки / отмены)."""
        order = self.db.get_order(order_id)
        if not order:
            raise ApiError(404, "Заказ не найден.")
        o = _row(order)
        out = []
        for m in self.db.order_messages(order_id, 1000):
            from_courier = bool(m["from_courier"])
            who = (m.get("courier_name") or o.get("courier_name") or "Курьер") if from_courier else (_plain_name(m.get("customer_name") or o.get("customer_name")) or "Покупатель")
            out.append({"id": int(m["id"]), "text": m.get("text"), "photo": bool(m.get("has_photo")), "at": m["created_at"],
                        "from_courier": from_courier, "who": who, "read": bool(m.get("read_at")), "mine": False})
        return {"order_id": order_id, "status_label": STATUS_LABELS.get(o.get("status"), o.get("status")), "messages": out}

    # ------------------------------------------------------------ courier
    @staticmethod
    def _need_prepay(order) -> None:
        """Пока курьер не нажал «Взял предоплату», действия по заказу (на месте, доставлен, спор, оплата) закрыты."""
        o = _row(order)
        if int(o.get("prepay_amount") or 0) > 0 and not o.get("prepay_taken_at"):
            raise ApiError(409, "Сначала возьмите предоплату с клиента за доставку и нажмите «Взял предоплату».")

    def _courier_owned_active(self, user_id: int, order_id: int):
        """Как courier_owned_active_order() в main.py."""
        order = self.db.get_order(order_id)
        if (
            not order
            or order["courier_telegram_id"] != user_id
            or order["status"] not in ("accepted", "in_transit")
        ):
            raise ApiError(409, "Этот заказ сейчас не у вас в работе.")
        return order

    async def courier_orders(self, user, body, query) -> dict[str, Any]:
        scope = query.get("scope", "available")
        if scope == "current":
            rows = self.db.get_courier_active_orders_full(user.id)
        else:
            rows = list(reversed(self.db.get_available_orders_for(user.id)))
        return {
            "orders": self.orders_json(rows, "courier"),
            "completed": self.db.get_courier_completed_count(user.id),
            "shift": self._shift_json(user.id),
        }

    def _shift_json(self, telegram_id: int) -> dict[str, Any]:
        courier = self.courier_of(telegram_id)
        shift = self.db.courier_shift(courier)
        return {
            "active": shift["active"],
            "status": shift["status"],
            "ask": shift["ask"],
            "busy": shift["busy"],
            "ended_at": shift["ended_at"],
            "end_reason": shift["end_reason"],
            "stock": self.db.courier_stock(courier),
            # Все позиции каталога — их курьер вносит перед «В путь!» и на складе (26.10.2026).
            "stock_items": self.db.stock_catalog(),
            "stock_sizes": self.db.empty_sizes(),  # литры пустых баллонов (по баллонам каталога, 28.10.2026)
            # «В путь!» ждёт администратора; отказ — курьер правит и отправляет снова.
            "request": self.db.shift_request(courier),
            # 28.10.2026: остаток прошлой смены (с ним «В путь!» без ввода); конец смены ждёт администратора.
            "stock_left": self._stock_items(self.db.stock_left(courier)) if courier else None,
            "end_requested_at": shift.get("end_requested_at"),
            "silent_since": shift.get("silent_since"),
            "request_declined_at": courier["shift_request_declined_at"] if courier else None,
            # Деньги и пустые баллоны (25.10.2026) — копятся до обнуления, не зависят от смены.
            "counters": self.db.courier_counters(int(courier["id"])) if courier else None,
            # Запрос «обнулить» ждёт администратора (28.10.2026) — курьер сам деньги не обнуляет.
            "reset_request": self.db.counter_reset_request(courier) if courier else None,
            "empties": self.db.courier_empties(int(courier["id"])) if courier else None,
            # «💰 Деньги» за смену (28.10.2026): разбор заработка, сдать в кассу, что доплатит магазин.
            "money": self.db.shift_money(courier) if courier else None,
            "delivery_rate": self.db.courier_delivery_rate(),
            "courier_rates": self.db.courier_rates(),
            "reload_pending": self._named_stock(courier["reload_pending"]) if courier and courier["reload_pending"] else None,
            # Трансляция геопозиции в чате с ботом — обязательна на смене (24.10.2026).
            "live": self.db.courier_live(courier),
        }

    async def courier_shift(self, user, body, query) -> dict[str, Any]:
        return self._shift_json(user.id)

    def _courier_or_403(self, user):
        courier = self.courier_of(user.id)
        if courier is None:
            raise ApiError(403, "Только для активных курьеров.")
        return courier

    async def courier_live_request(self, user, body, query) -> dict[str, Any]:
        """Receive or stop the standalone app's courier location stream."""
        courier = self._courier_or_403(user)
        if body.get("stop"):
            self.db.stop_courier_app_location(user.id)
            return {"ok": True, "live": self.db.courier_live(courier)}
        # Keep the legacy Telegram client working when it requests the old
        # chat-based location flow without coordinates. Standalone APK always
        # sends its first GPS point and never opens a bot chat.
        if body.get("lat") is None or body.get("lon") is None:
            link = await self.bridge.bot_link()
            if link:
                await self._safe(self.bridge.courier_live_instruction(user.id), "инструкция по геопозиции")
                return {"ok": True, "link": link}
            raise ApiError(400, "Передайте координаты геопозиции.")
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
        except (TypeError, ValueError):
            raise ApiError(400, "Не удалось получить геопозицию.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ApiError(400, "Некорректные координаты.")
        updated = self.db.set_courier_app_location(user.id, lat, lon)
        if not updated:
            raise ApiError(403, "Курьер не найден или отключён.")
        return {"ok": True, "live": self.db.courier_live(updated)}

    async def courier_shift_confirm(self, user, body, query) -> dict[str, Any]:
        """«Да, работаем!» в окне «Вы ещё на смене?» — следующий вопрос через час."""
        courier = self._courier_or_403(user)
        self.db.confirm_courier_shift(int(courier["id"]))
        return self._shift_json(user.id)

    async def courier_shift_end(self, user, body, query) -> dict[str, Any]:
        """«🏁 Закончить смену» / «Нет, закончил работу» (28.10.2026): смена не закрывается сразу —
        администратор видит остатки (полные, пустые с замен и с возвратов, деньги) и подтверждает."""
        courier = self._courier_or_403(user)
        if self.db.courier_busy(int(courier["id"])):
            raise ApiError(409, "Сначала завершите заказы в работе.")
        status = self.db.courier_shift(courier)["status"]
        if status in ("online", "silent", "break"):
            self.db.request_shift_end(int(courier["id"]))
            await self._safe(self.bridge.admin_alert(
                f"🏁 Курьер {courier['name']} завершил смену — проверьте остатки и подтвердите в приложении."), "конец смены")
        return self._shift_json(user.id)

    async def courier_shift_break(self, user, body, query) -> dict[str, Any]:
        """«Взять перерыв» — офлайн, только без заказов в работе."""
        courier = self._courier_or_403(user)
        shift = self.db.courier_shift(courier)
        if not shift["active"]:
            raise ApiError(409, "Вы не на смене.")
        if shift["busy"]:
            raise ApiError(409, "Перерыв можно взять, когда нет заказов в работе.")
        self.db.end_courier_shift(int(courier["id"]), "break")
        return self._shift_json(user.id)

    async def courier_shift_resume(self, user, body, query) -> dict[str, Any]:
        """«Вернуться на смену» после перерыва — регионы и баллоны прежние."""
        courier = self._courier_or_403(user)
        if self.db.courier_shift(courier)["status"] != "break":
            raise ApiError(409, "Вы не на перерыве.")
        self.db.resume_courier_shift(int(courier["id"]))
        return self._shift_json(user.id)

    async def courier_shift_start(self, user, body, query) -> dict[str, Any]:
        """«В путь!»: заявка на смену (регион курьер не выбирает — работа по регионам убрана 28.10.2026)."""
        courier = self.courier_of(user.id)
        if courier is None:
            raise ApiError(403, "Только для активных курьеров.")
        regions: list[str] = []
        shift = self.db.courier_shift(courier)
        if shift["active"] or shift["status"] in ("silent", "break"):
            raise ApiError(409, "Вы уже на смене.")
        elif shift["status"] == "ending":
            raise ApiError(409, "Ждём, пока администратор подтвердит конец прошлой смены.")
        else:
            # «В путь!» (26.10.2026): заявка ждёт подтверждения администратора. 28.10.2026:
            # есть остаток прошлой смены — начинает с ним (ничего не вводит); «Стартую со склада» —
            # вводит всё, что в машине, и сдаёт пустые («Понял, не забуду»); первая смена — вводит всё.
            left = self.db.stock_left(courier)
            empties = None
            if body.get("warehouse"):
                if body.get("empties_ack") is not True:
                    raise ApiError(400, "Подтвердите: «Понял, не забуду» сдать пустые баллоны.")
                stock, source = self._shift_stock(body.get("stock")), "warehouse"
            elif left is not None:
                stock, source = left, "left"
            else:
                stock, source = self._shift_stock(body.get("stock")), "entry"
            if source == "warehouse":
                prev = self.db.shift_request(courier)
                empties = prev["empties"] if prev and prev.get("source") == "warehouse" else self.db.hand_over_empties(int(courier["id"]))
            first = self.db.shift_request(courier) is None
            self.db.request_shift(user.id, regions, stock, source, empties)
            if first:
                where = {"left": " (остаток прошлой смены)", "warehouse": " со склада"}.get(source, "")
                await self._safe(self.bridge.admin_alert(
                    f"🚴 Курьер {courier['name']} хочет выйти на смену{where}: {self._stock_text(stock)}. Подтвердите в приложении."),
                    "заявка на смену")
        return self._shift_json(user.id)

    async def courier_shift_request_cancel(self, user, body, query) -> dict[str, Any]:
        """«✏️ Изменить» на экране ожидания: заявка снимается, курьер правит и отправляет снова."""
        self._courier_or_403(user)
        self.db.cancel_shift_request(user.id)
        return self._shift_json(user.id)

    def _shift_stock(self, raw: Any) -> dict[int, int]:
        """Товары с собой (начало смены / склад): {"<id товара>": штук} по всему каталогу —
        убранные крестиком поля не присылаются (значит 0)."""
        if not isinstance(raw, dict):
            raise ApiError(400, "Введите, какие товары и сколько штук у вас с собой.")
        known = {p["id"]: p["name"] for p in self.db.stock_catalog()}
        stock = {pid: 0 for pid in known}
        for key, value in raw.items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                raise ApiError(400, "Неизвестный товар.")
            if pid not in known:
                raise ApiError(400, "Такого товара нет в каталоге.")
            stock[pid] = _int(value, known[pid], 0, 999)
        return stock

    def _stock_items(self, stock: dict[int, int] | None) -> list[dict[str, Any]] | None:
        if stock is None:
            return None
        names = self.db.product_names(stock)
        return [{"id": k, "name": names[k], "qty": v} for k, v in stock.items() if k in names]

    def _named_stock(self, raw: str | None) -> dict[str, int] | None:
        """Сохранённые остатки (формат базы) → {название товара: штук}."""
        parsed = self.db.parse_stock(raw)
        if parsed is None:
            return None
        names = self.db.product_names(parsed)
        return {names.get(k, f"товар {k}"): v for k, v in parsed.items()}

    def _shortage_text(self, shortage: dict[str, tuple[int, int]]) -> str:
        return "Не хватает товаров: " + ", ".join(f"{name} — нужно {need}, у вас {have}"
                                                 for name, (need, have) in shortage.items()) + "."

    async def courier_reload(self, user, body, query) -> dict[str, Any]:
        """«🏭 Заехать на склад»: {"stock": {"2": 5, "5": 3}} — фактические баллоны после загрузки."""
        courier = self.courier_of(user.id)
        if courier is None:
            raise ApiError(403, "Только для активных курьеров.")
        if not self.db.courier_shift(courier)["active"]:
            raise ApiError(409, "Сначала нажмите «В путь!».")
        stock = self._shift_stock(body.get("stock"))
        if body.get("empties_ack") is not True:
            raise ApiError(400, "Подтвердите: «Понял, не забуду» сдать пустые баллоны.")
        empties = (json.loads(courier["reload_empties"]) if courier["reload_pending"] and courier["reload_empties"]
                   else self.db.hand_over_empties(int(courier["id"])))
        self.db.courier_reload(user.id, stock, empties)
        await self._safe(self.bridge.admin_alert(
            f"🏭 Курьер {courier['name']} загрузился на складе: {self._stock_text(stock)}. Подтвердите в приложении."),
            "загрузка на складе")
        return self._shift_json(user.id)

    def _stock_text(self, stock: dict) -> str:
        """{id товара: штук} → «Баллон 8 л — 3, Шарики — 10» (нули не пишем)."""
        items = {int(k): int(v) for k, v in stock.items()}
        names = self.db.product_names(items)
        parts = [f"{names.get(k, f'товар {k}')} — {v}" for k, v in items.items() if v]
        return ", ".join(parts) or "ничего нет"

    async def courier_assigned_ack(self, user, body, query) -> dict[str, Any]:
        """«Понятно» в окне «Вам назначили заказ»."""
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise ApiError(400, "Нет заказов.")
        self.db.ack_assignments(user.id, [_int(i, "заказ", 1) for i in ids[:100]])
        return {"ok": True}

    @staticmethod
    def _page(query) -> tuple[int, int]:
        try:
            offset = max(0, int(query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        return HISTORY_PAGE, offset

    async def courier_history(self, user, body, query) -> dict[str, Any]:
        """Личная история курьера: только его доставленные заказы."""
        limit, offset = self._page(query)
        rows, total, _total_sum = self.db.get_orders_history(
            statuses=("delivered",), courier_telegram_id=user.id, limit=limit, offset=offset
        )
        # Общую сумму доставок курьеру не показываем (решение владельца, 18.10).
        return {
            "orders": self.orders_json(rows, "courier"),
            "total": total,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    async def courier_delivered(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«Доставлен» — только вместе с оценкой покупателя (1–5); комментарий необязателен."""
        current = self._courier_owned_active(user.id, order_id)
        self._need_prepay(current)
        # Порядок: «🚪 Я на месте» → (оплата) → «✅ Заказ доставлен».
        if not current["courier_arrived_at"]:
            raise ApiError(409, "Сначала нажмите «🚪 Я на месте» — кнопка «Заказ доставлен» появится после неё.")
        if current["pay_on_delivery"] and not current["payment_confirmed_at"]:
            raise ApiError(409, "Сначала примите оплату и подтвердите её фото («📷 Подтвердить оплату»).")
        if body.get("rating") in (None, ""):
            raise ApiError(400, "Сначала оцените покупателя — выберите от 1 до 5 звёзд.")
        rating = _int(body.get("rating"), "оценка", 1, 5)
        comment = _optional_text(body.get("comment"), MAX_CUSTOMER_NOTE)
        order = self.db.update_order_status(order_id, "delivered")
        if not order:
            raise ApiError(409, "Заказ уже отмечен как доставленный или отменён.")
        self.db.add_customer_review(
            order_id, int(order["user_id"]), order["courier_id"], order["courier_name"], rating, comment
        )
        await self._safe(self.bridge.customer_review(order_id, rating, comment), f"отзыв о покупателе {order_id}", order_id)
        await self._safe(
            self.bridge.courier_delivered(order_id, current["status"]), f"доставка заказа {order_id}", order_id
        )
        return {"order": self.order_json(order, "courier")}

    @staticmethod
    def _can_release(o: dict[str, Any]) -> bool:
        """«↩️ Взял заказ по ошибке» — только пока курьер не нажал «Я на месте»
        (и, значит, оплату ещё не принимал)."""
        return (
            o.get("status") in ("accepted", "in_transit")
            and not o.get("courier_arrived_at")
            and not o.get("payment_confirmed_at")
        )

    async def courier_release(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«↩️ Взял заказ по ошибке»: заказ снимается с курьера и снова уходит всем курьерам."""
        current = self._courier_owned_active(user.id, order_id)
        if not self._can_release(_row(current)):
            raise ApiError(409, "После «Я на месте» отказаться от заказа нельзя. Если покупатель не выходит на связь — нажмите «📵 Покупатель не выходит на связь».")
        order = self.db.update_order_status(order_id, "new")
        if not order:
            raise ApiError(409, "Заказ уже недоступен.")
        await self._safe(self.bridge.courier_released(order_id, _row(current)), f"возврат заказа {order_id}", order_id)
        return {"ok": True, "order_id": order_id}

    async def courier_no_contact(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«📵 Покупатель не выходит на связь»: заказ уходит от курьера в «⚠️ Спорные заказы» к админу."""
        self._need_prepay(self._courier_owned_active(user.id, order_id))
        order = self.db.update_order_status(order_id, "disputed")
        if not order:
            raise ApiError(409, "Статус заказа изменить не удалось.")
        await self._safe(self.bridge.order_disputed(order_id), f"спорный заказ {order_id}", order_id)
        return {"ok": True, "order_id": order_id}

    # ------------------------------------------------------------ «🤝 Реферальная программа»
    async def referral(self, user, body, query) -> dict[str, Any]:
        """«🤝 Партнёрская программа». Админ — список заявок (в режиме админа);
        остальные и админ в режиме покупателя (?view=customer) — своя заявка.
        Программа лояльности для клиентов убрана (20.10) — только партнёрская."""
        if self.is_admin(user.id) and query.get("view") != "customer":
            requests = []
            for r in self.db.get_referral_requests():
                name = " ".join(p for p in (r["first_name"], r["last_name"]) if p) or "Покупатель"
                is_courier = r["courier_id"] is not None
                if r["kind"] != "partner":
                    continue  # заявки на лояльность больше не показываем
                requests.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "user_id": r["user_id"],
                    "name": name,
                    "username": (r["username"] or "").lstrip("@") or None,
                    "is_courier": is_courier,
                    "courier_name": r["courier_name"],
                    "courier_active": bool(r["courier_active"]) if is_courier else None,
                    "phone": (r["courier_phone"] if is_courier and r["courier_phone"] else r["last_phone"]) or None,
                    "purchases": int(r["purchases"] or 0),
                    "created_at": r["created_at"],
                    "comment": r["comment"],
                })
            partners = []
            for p in self.db.get_partners():
                pname = " ".join(x for x in (p["first_name"], p["last_name"]) if x) or "Партнёр"
                partners.append({
                    "user_id": p["user_id"], "name": pname,
                    "username": (p["username"] or "").lstrip("@") or None,
                    "is_courier": p["courier_id"] is not None,
                    "phone": (p["courier_phone"] if p["courier_id"] is not None and p["courier_phone"] else p["last_phone"]) or None,
                    "code": p["partner_code"], "link": await self.bridge.partner_link(p["partner_code"]),
                    "approved_at": p["approved_at"],
                    "personal": [r for r in self.db.partner_reward_table(int(p["user_id"]), include_zero=True)
                                 if r["personal"]],
                    "invited": int(p["invited"] or 0), "rewarded_orders": int(p["rewarded_orders"] or 0),
                    "earned": int(p["earned"] or 0), "written_off": int(p["written_off"] or 0), "balance": p["balance"],
                })
            return {"admin": True, "requests": requests, "partners": partners,
                    "rewards": self.db.partner_reward_table(include_zero=True)}
        data: dict[str, Any] = {"admin": False, "requests": self.db.get_user_referral_requests(user.id)}
        partner = self.db.get_partner(user.id)
        if partner:
            history = []
            for h in self.db.get_partner_points_history(user.id):
                who = " ".join(x for x in (h.get("first_name"), h.get("last_name")) if x)
                history.append({
                    "points": int(h["points"]), "kind": h["kind"], "created_at": h["created_at"],
                    "order_id": h.get("order_id"), "order_total": h.get("order_total"),
                    "reason": h.get("reason"), "who": who or None,
                })
            data["partner"] = {
                "code": partner["partner_code"], "link": await self.bridge.partner_link(partner["partner_code"]),
                "rewards": self.db.partner_reward_table(user.id),
                "invited": self.db.get_invited_count(user.id),
                "points": self.db.partner_points_balance(user.id),
                "history": history,
                "link_notice": self.db.partner_link_notice(user.id),
            }
        return data

    async def admin_partner_link_refresh(self, user, body, query) -> dict[str, Any]:
        """«📣 Выслать всем партнёрам новую ссылку»: у каждого партнёра в приложении
        появится окно, которое не закрыть, пока он не подтвердит рассылку."""
        version = self.db.bump_partner_link_version()
        self._log_admin(user, "Партнёрам: ссылка обновлена (окно с просьбой разослать)", details=f"версия {version}")
        return {"ok": True, "version": version, "partners": len(self.db.get_partners())}

    async def partner_clients(self, user, body, query) -> dict[str, Any]:
        """Список @username покупателей партнёра — для рассылки им новой ссылки (в самом приложении)."""
        if not self.db.get_partner(user.id):
            raise ApiError(403, "Это только для партнёров.")
        lines = []
        for c in self.db.get_partner_clients(user.id):
            username = (c.get("username") or "").lstrip("@")
            if username:
                lines.append("@" + username)
            else:
                name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or "Покупатель"
                lines.append(f"{name} (ID {c['telegram_id']}, без username)")
        return {"count": len(lines), "text": "\n".join(lines)}

    async def partner_link_ack(self, user, body, query) -> dict[str, Any]:
        if not self.db.get_partner(user.id):
            raise ApiError(403, "Это только для партнёров.")
        self.db.ack_partner_link(user.id)
        return {"ok": True}

    async def admin_referral_approve(self, user, body, query, request_id: int) -> dict[str, Any]:
        """«✅ Одобрить»: человек получает ссылку. Баллы — за каждый баллон в доставленных
        заказах приглашённых клиентов, по ставке баллона (Админка → Товары)."""
        row = self.db.approve_partner(request_id, 0, 0)
        if not row:
            raise ApiError(409, "Заявка уже обработана — обновите список.")
        u = _row(self.db.get_user(int(row["user_id"])))
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or "Партнёр"
        await self._safe(self.bridge.partner_event(
            f"✅ {name} — партнёр: ссылка выдана"), "одобрение партнёра")
        return await self.referral(user, {}, {})

    async def admin_partner_rewards(self, user, body, query, partner_id: int) -> dict[str, Any]:
        """Личная программа партнёра: {"rewards": {"<product_id>": 250, "<product_id>": ""}} —
        число — своя ставка за 1 баллон, пусто — общая ставка товара."""
        if not self.db.get_partner(partner_id):
            raise ApiError(404, "Партнёр не найден.")
        raw = body.get("rewards")
        if not isinstance(raw, dict):
            raise ApiError(400, "Нет ставок.")
        balloons = {r["product_id"]: r for r in self.db.partner_reward_table(include_zero=True)}
        changes: dict[int, int | None] = {}
        for key, value in raw.items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                raise ApiError(400, "Неверный товар.")
            if pid not in balloons:
                raise ApiError(400, "Личную ставку можно задать только баллону из каталога.")
            changes[pid] = None if value in (None, "") else _int(value, "баллы", 0, 1_000_000)
        self.db.set_partner_personal_rewards(partner_id, changes)
        u = _row(self.db.get_user(partner_id))
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or "Партнёр"
        table = self.db.partner_reward_table(partner_id, include_zero=True)
        text = ", ".join(f"{r['name']} — {r['points']}{' (личная)' if r['personal'] else ''}" for r in table)
        self._log_admin(user, "Партнёр: личная программа", target_user_id=partner_id, target_name=name, details=text)
        await self._safe(self.bridge.partner_event(
            f"⭐ Партнёр {name}: программа — {text} — админ {user.display_name}"), "личная программа партнёра")
        return await self.referral(user, {}, {})

    async def admin_partner_writeoff(self, user, body, query, partner_id: int) -> dict[str, Any]:
        """«➖ Списать баллы» — не больше, чем есть у партнёра."""
        if not self.db.get_partner(partner_id):
            raise ApiError(404, "Партнёр не найден.")
        points = _int(body.get("points"), "баллы", 1, 100_000_000)
        reason = _optional_text(body.get("reason"), 200)
        try:
            left = self.db.write_off_partner_points(partner_id, points, reason)
        except ValueError:
            raise ApiError(409, f"Можно списать не больше {self.db.partner_points_balance(partner_id)} баллов.")
        u = _row(self.db.get_user(partner_id))
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or "Партнёр"
        self._log_admin(user, "Партнёр: списание баллов", target_user_id=partner_id, target_name=name,
                        amount=-points, balance_after=left, details=reason)
        await self._safe(self.bridge.partner_event(
            f"➖ У партнёра {name} списано {points} баллов (осталось {left})" + (f": {reason}" if reason else "")
            + f" — админ {user.display_name}"),
            "списание баллов партнёра")
        return await self.referral(user, {}, {})

    async def admin_referral_decline(self, user, body, query, request_id: int) -> dict[str, Any]:
        """{mode: temp | forever | dismiss}: на 7 дней / навсегда / «просто отклонить»
        (убрать из списка, человек может подать снова сразу). Старое {forever: bool} тоже понимается."""
        mode = body.get("mode") or ("forever" if body.get("forever") else "temp")
        if mode not in ("temp", "forever", "dismiss"):
            raise ApiError(400, "Неизвестный вариант отклонения.")
        row = self.db.decline_referral_request(request_id, mode)
        if not row:
            raise ApiError(409, "Заявка уже обработана — обновите список.")
        u = _row(self.db.get_user(int(row["user_id"])))
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or "Покупатель"
        await self._safe(self.bridge.referral_declined(name, row["kind"], mode), "отклонение заявки на реферал")
        return await self.referral(user, {}, {})

    async def referral_apply(self, user, body, query) -> dict[str, Any]:
        """{kind: partner | loyalty} — заявка на партнёрскую программу или систему лояльности."""
        kind = body.get("kind") or "partner"
        if kind != "partner":
            raise ApiError(400, "Сейчас доступна только партнёрская программа.")
        # Комментарий обязателен: по нему администратор отличает своих людей от случайных.
        comment = str(body.get("comment") or "").strip()
        if len(comment) < 2:
            raise ApiError(400, "Напишите комментарий к заявке — без него заявку не отправить.")
        if len(comment) > REFERRAL_COMMENT_MAX:
            raise ApiError(400, f"Комментарий — не длиннее {REFERRAL_COMMENT_MAX} символов.")
        result = self.db.add_referral_request(user.id, kind, comment)
        if result == "forever":
            raise ApiError(409, "Заявка на эту программу отклонена.")
        if result == "waiting":
            raise ApiError(409, "Подать заявку снова можно будет позже — дата указана на экране.")
        if result in ("created", "again"):
            courier = self.courier_of(user.id)
            name = " ".join(p for p in (user.first_name, user.last_name) if p) or "Покупатель"
            contact = f"@{user.username}" if user.username else f"ID {user.id}"
            await self._safe(
                self.bridge.referral_request(user.id, name, contact, courier is not None, kind, comment),
                "заявка на реферальную программу",
            )
        return await self.referral(user, {}, {"view": "customer"})

    # ------------------------------------------------------------ admin: «⚠️ Спорные заказы»
    async def admin_disputes(self, user, body, query) -> dict[str, Any]:
        return {"orders": self.orders_json(self.db.get_disputed_orders(), "admin")}

    def _disputed(self, order_id: int):
        order = self.db.get_order(order_id)
        if not order:
            raise ApiError(404, "Заказ не найден.")
        if order["status"] != "disputed":
            raise ApiError(409, "Этот заказ уже не в «Спорных» — обновите список.")
        return order

    async def admin_dispute_return(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«🔁 Вернуть в работу»: заказ снова «Активен» и уходит всем курьерам."""
        current = self._disputed(order_id)
        order = self.db.update_order_status(order_id, "new")
        if not order:
            raise ApiError(409, "Не удалось вернуть заказ в работу.")
        await self._safe(self.bridge.dispute_returned(order_id, _row(current)), f"возврат спорного {order_id}", order_id)
        return await self.admin_disputes(user, {}, {})

    async def admin_dispute_close(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«🗑 Удалить заказ»: убирается из «Спорных» и из активных у покупателя,
        остаётся в истории со статусом «Спорный — закрыт». В Excel не попадает
        (туда идут только доставленные)."""
        current = self._disputed(order_id)
        order = self.db.update_order_status(order_id, "disputed_closed")
        if not order:
            raise ApiError(409, "Не удалось закрыть заказ.")
        await self._safe(self.bridge.dispute_closed(order_id, _row(current)), f"закрытие спорного {order_id}", order_id)
        return await self.admin_disputes(user, {}, {})

    async def courier_location(self, user, body, query, order_id: int) -> dict[str, Any]:
        order = self._courier_owned_active(user.id, order_id)
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
        except (TypeError, ValueError):
            raise ApiError(400, "Некорректные координаты.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ApiError(400, "Некорректные координаты.")
        first = order["courier_last_lat"] is None
        self.db.set_order_courier_location(order_id, lat, lon)
        courier = self.courier_of(user.id)
        if courier is not None:  # и на карту админа
            self.db.set_courier_point(int(courier["id"]), lat, lon)
        await self._safe(
            self.bridge.courier_location(order_id, lat, lon, first), f"геопозиция по заказу {order_id}", order_id
        )
        return {"ok": True}

    async def courier_prepay_taken(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«💵 Взял предоплату»: после неё курьеру открываются «Я на месте», «Доставлен» и остальные кнопки."""
        self._courier_owned_active(user.id, order_id)
        courier = self.courier_of(user.id)
        order = self.db.take_prepay(order_id, int(courier["id"])) if courier else None
        if not order:
            raise ApiError(409, "Предоплата по этому заказу не требуется или уже отмечена.")
        amount = int(order["prepay_amount"])
        await self._safe(self.bridge.bonus_event(
            f"💵 Заказ №{order_id}: курьер {user.display_name} взял предоплату за доставку — {money(amount)}"),
            f"предоплата {order_id}", order_id)
        return {"ok": True, "order": self.order_json(order, "courier")}

    async def courier_arrived(self, user, body, query, order_id: int) -> dict[str, Any]:
        """«Я на месте» — покупателю приходит уведомление «Курьер на месте»."""
        current = self._courier_owned_active(user.id, order_id)
        self._need_prepay(current)
        sent = current["courier_arrived_at"] if "courier_arrived_at" in current.keys() else None
        if sent:
            try:
                ago = (datetime.now(timezone.utc) - datetime.fromisoformat(sent)).total_seconds()
            except ValueError:
                ago = COURIER_ARRIVED_COOLDOWN
            if ago < COURIER_ARRIVED_COOLDOWN:
                raise ApiError(429, "Покупатель уже уведомлён. Повторно можно через минуту.")
        order = self.db.mark_courier_arrived(order_id, user.id)
        if not order:
            raise ApiError(409, "Этот заказ сейчас не у вас в работе.")
        try:
            await self.bridge.courier_arrived(order_id)
            notified = True
        except Exception:
            logger.exception("Mini App: не удалось уведомить покупателя о прибытии, заказ %s", order_id)
            notified = False
        return {"order": self.order_json(order, "courier"), "notified": notified}

    # ------------------------------------------------------------ courier: оплата при курьере
    @staticmethod
    def qr_emoji(c) -> str:
        if c["kind"] == "sbp":
            return crypto.SBP_EMOJI
        return crypto.EMOJI.get(c["currency"] or "", "💎")

    @staticmethod
    def qr_label(c) -> str:
        """«💵 USDT · TRC-20 (Tron)» / «📱 СБП · Сбер» — смайлик, валюта, сеть."""
        if c["kind"] == "crypto" and c["currency"]:
            text = c["currency"] + (f" · {c['network']}" if c["network"] else "")
        else:
            title = (c["title"] or "").strip()
            text = title if title.upper().startswith("СБП") else (f"СБП · {title}" if title else "СБП")
        return f"{Api.qr_emoji(c)} {text}"

    @staticmethod
    def qr_json(c) -> dict[str, Any]:
        info = crypto.CURRENCIES.get(c["currency"] or "")
        return {
            "id": c["id"],
            "kind": c["kind"],
            "emoji": Api.qr_emoji(c),
            "label": Api.qr_label(c),
            "title": c["title"],
            "currency": c["currency"],
            "currency_label": info[2] if info else None,
            "network": c["network"],
            "image": f"/api/media/payqr/{c['id']}?v={_short(c['file_id'])}",
        }

    async def courier_payment_qr(self, user, body, query) -> dict[str, Any]:
        """Все QR для приёма оплаты + какие способы нужны заказам курьера в работе
        (кнопки остальных способов в приложении неактивны)."""
        orders = [self.db.get_order(int(o["id"])) for o in self.db.get_courier_active_orders(user.id)]
        need = [
            {"order_id": o["id"], "method": o["payment_method"], "confirmed": bool(o["payment_confirmed_at"])}
            for o in orders if o and o["pay_on_delivery"]
        ]
        return {"codes": [self.qr_json(c) for c in self.db.get_payment_qr_codes()], "orders": need}

    async def courier_pay_screen(self, user, body, query, order_id: int, code_id: int) -> dict[str, Any]:
        """QR + сумма к оплате по заказу (₽ для СБП, монеты по курсу оформления для крипты)."""
        order = self._courier_owned_active(user.id, order_id)
        code = self.db.get_payment_qr_code(code_id)
        if not code:
            raise ApiError(404, "QR-код не найден — возможно, админ его убрал.")
        if not order["pay_on_delivery"]:
            raise ApiError(409, "Этот заказ оплачивается наличными.")
        # Курьер может показать ЛЮБОЙ QR («❗ Изменить валюту ❗»), если покупатель
        # ошибся при оформлении; сумма — по курсу на момент оформления заказа.
        total = int(order["total"])
        result: dict[str, Any] = {"order_id": order_id, "qr": self.qr_json(code), "total_rub": total,
                                  "amount": None, "rate": None, "rate_note": None}
        if code["kind"] == "crypto":
            cur = code["currency"]
            quotes = crypto.loads(order["crypto_quotes"])
            if cur not in quotes:
                # Кошелёк добавлен после оформления заказа или курс тогда был недоступен —
                # считаем по текущему курсу и запоминаем в заказе.
                try:
                    rates = await self.bridge.get_crypto_rates([cur])
                except Exception:
                    logger.exception("Mini App: курс %s недоступен", cur)
                    rates = {}
                fresh = crypto.build_quotes(total, rates)
                if cur in fresh:
                    quotes[cur] = fresh[cur]
                    self.db.set_order_crypto_quotes(order_id, crypto.dumps(quotes))
                    result["rate_note"] = "курс на момент открытия QR (при оформлении курса этой монеты не было)"
            if cur in quotes:
                result["amount"] = quotes[cur]["amount"]
                result["rate"] = quotes[cur]["rate"]
            else:
                result["rate_note"] = "курс сейчас недоступен — возьмите оплату по рублёвой сумме или выберите другой кошелёк"
        return result

    async def courier_payment_confirm(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Фото экрана покупателя с переводом → в Telegram-канал (лог), заказ помечается оплаченным."""
        order = self._courier_owned_active(user.id, order_id)
        self._need_prepay(order)
        method = order["payment_method"]
        # В группу дублируются только фото оплат криптой и по СБП; наличные — никогда.
        if not order["pay_on_delivery"] or method not in PHOTO_TO_GROUP_METHODS:
            raise ApiError(409, "Этот заказ оплачивается наличными.")
        raw, mime = decode_data_url(body.get("photo") or "", MAX_PROOF_BYTES)
        quotes = crypto.loads(order["crypto_quotes"])
        lines = [
            f"💳 Оплата по заказу №{order_id} подтверждена курьером {order['courier_name'] or ''}".strip(),
            f"Способ: {PAYMENT_METHOD_LABELS.get(method, method)}",
            f"Сумма: {money(int(order['total']))}",
        ]
        used = None
        if body.get("qr_id") not in (None, ""):
            try:
                used = self.db.get_payment_qr_code_any(int(body["qr_id"]))
            except (TypeError, ValueError):
                raise ApiError(400, "Некорректный QR.")
            if not used:
                raise ApiError(404, "QR-код не найден.")
        chosen = self.db.get_payment_qr_code_any(int(order["payment_qr_id"])) if order["payment_qr_id"] else None
        if used is not None:
            lines.append(f"QR: {self.qr_label(used)}")
            if chosen is not None and int(chosen["id"]) != int(used["id"]):
                lines.append(f"❗ Курьер изменил валюту: покупатель выбрал {self.qr_label(chosen)}")
        else:
            code_title = _optional_text(body.get("qr_title"), 100)
            if code_title:
                lines.append(f"QR: {code_title}")
        cur = (used["currency"] if used is not None and used["kind"] == "crypto" else None) or _optional_text(body.get("currency"), 10)
        if cur and cur in quotes:
            lines.append(f"В криптовалюте: {quotes[cur]['amount']} {cur} (курс 1 {cur} = {quotes[cur]['rate']} ₽)")
        lines.append(f"Покупатель: {order['customer_name']}, {order['customer_phone']}")
        try:
            file_id = await self.bridge.send_payment_photo(order_id, raw, mime, "\n".join(lines))
        except Exception:
            logger.exception("Mini App: фото оплаты по заказу %s не отправлено", order_id)
            raise ApiError(502, "Не удалось отправить фото. Попробуйте ещё раз.")
        self.db.confirm_order_payment(order_id, file_id, int(used["id"]) if used is not None else None)
        return {"order": self.order_json(self.db.get_order(order_id), "courier")}

    async def courier_customer_note(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Комментарий курьера о покупателе. Видят только админ и курьеры.
        Оставить можно по своему заказу — в работе или уже завершённому."""
        order = self.db.get_order(order_id)
        if not order or order["courier_telegram_id"] != user.id:
            raise ApiError(404, "Заказ не найден среди ваших.")
        text = _text(body.get("text"), "Комментарий", 2, MAX_CUSTOMER_NOTE)
        self.db.add_customer_review(
            order_id, int(order["user_id"]), order["courier_id"], order["courier_name"], None, text
        )
        await self._safe(self.bridge.customer_review(order_id, None, text), f"комментарий о покупателе {order_id}", order_id)
        return {"ok": True, "customer_reviews": self.db.get_customer_reviews(int(order["user_id"]))}

    # ------------------------------------------------------------ admin: orders
    async def admin_orders(self, user, body, query) -> dict[str, Any]:
        scope = query.get("scope", "open")
        rows = self.db.get_admin_open_orders() if scope == "open" else self.db.get_all_orders()
        if scope == "open" and any(not _row(o).get("region") for o in rows):
            self._backfill_regions(rows)
            rows = self.db.get_admin_open_orders()
        couriers = []
        for c in self.db.get_couriers():
            shift = self.db.courier_shift(c)
            couriers.append({
                "id": c["id"], "name": c["name"], "on_shift": shift["active"], "status": shift["status"],
                "stock": self.db.courier_stock(c),
            })
        return {"orders": self.orders_json(rows, "admin"), "couriers": couriers,
                "regions": [{"id": k, "label": v} for k, v in REGIONS]}

    async def admin_map(self, user, body, query) -> dict[str, Any]:
        """Интерактивная карта курьеров: последняя точка, цвет метки, смена, трансляция, заказы в работе."""
        active = {}
        for o in self.db.get_admin_open_orders():
            o = _row(o)
            if o.get("courier_id") and o["status"] in ("accepted", "in_transit"):
                active.setdefault(int(o["courier_id"]), []).append(
                    {"id": int(o["id"]), "status": o["status"], "status_label": STATUS_LABELS.get(o["status"], o["status"]),
                     "address": (o.get("delivery_address") or "").split("\n")[0], "region": REGION_LABELS.get(o.get("region") or "")})
        couriers = []
        for c in self.db.get_couriers():
            shift = self.db.courier_shift(c)
            live = self.db.courier_live(c)
            couriers.append({
                "id": int(c["id"]), "name": c["name"], "phone": c["phone"], "username": c["username"],
                "color": int(c["marker_idx"] or 0) % self.db.MARKER_COLORS,
                "status": shift["status"], "started_at": shift["started_at"], "ended_at": shift["ended_at"],
                "live": live, "orders": active.get(int(c["id"]), []),
            })
        return {"couriers": couriers, "now": self.db.now()}

    SILENT_TEXT = "Курьер перестал отвечать на опрос «Вы ещё на смене?» — заказы ему назначить нельзя."

    def _check_not_silent(self, courier_id: int, order_ids: list[int] | None = None) -> None:
        """28.10.2026: кому можно отдать заказы. Нельзя: курьер не отвечает на опрос, не на смене /
        закончил смену (нет учёта товаров — «никаких старых смен»), не хватает товаров (order_ids —
        сумма по всем этим заказам; заказы, уже у него в работе, учтены в остатке)."""
        c = self.db.get_courier(courier_id)
        if c is None:
            raise ApiError(404, "Курьер не найден.")
        status = self.db.courier_shift(c)["status"]
        if status == "silent":
            raise ApiError(409, self.SILENT_TEXT)
        if status in ("offline", "ending"):
            raise ApiError(409, f"{c['name']} не на смене — заказы можно отдать только курьеру на смене (после «В путь!»).")
        if order_ids is not None:
            shortage = self.db.assign_shortage(c, order_ids)
            if shortage is None:
                raise ApiError(409, f"У {c['name']} нет учёта товаров — пусть закончит смену и снова нажмёт «В путь!».")
            if shortage:
                raise ApiError(409, f"{c['name']}: " + self._shortage_text(shortage))

    async def admin_assign_many(self, user, body, query) -> dict[str, Any]:
        """Группа заказов одному курьеру: {"order_ids": [1, 2], "courier_id": 5}."""
        courier_id = _int(body.get("courier_id"), "курьер", 1)
        self._check_not_silent(courier_id)
        ids = body.get("order_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            raise ApiError(400, "Выберите заказы.")
        done, failed, short = [], [], []
        for raw in ids:
            order_id = _int(raw, "заказ", 1)
            current = self.db.get_order(order_id)
            if not current or current["status"] != "new" or current["courier_id"] is not None:
                failed.append(order_id)
                continue
            try:  # по порядку: на что хватает товаров — назначается, остальное — «не хватает»
                self._check_not_silent(courier_id, [order_id])
            except ApiError as e:
                short.append({"id": order_id, "reason": e.message})
                continue
            self.db.release_individual(order_id)
            if not self.db.assign_courier(order_id, courier_id):
                failed.append(order_id)
                continue
            done.append(order_id)
            await self._safe(self.bridge.admin_assigned(order_id, _row(current)), f"назначение {order_id}", order_id)
        if not done:
            if short:
                raise ApiError(409, short[0]["reason"] if len(short) == 1 else
                               "Не хватает товаров: " + "; ".join(f"№{x['id']} — {x['reason']}" for x in short))
            raise ApiError(409, "Не удалось назначить — заказы уже взяты или курьер недоступен.")
        return {"assigned": done, "failed": failed + [x["id"] for x in short], "short": short}

    async def admin_history_delete(self, user, body, query) -> dict[str, Any]:
        """Удалить закрытые заказы из истории везде (покупатель, курьер, Excel). Нужно подтверждение confirm=true."""
        raw = body.get("order_ids")
        if not isinstance(raw, list) or not raw or len(raw) > 200:
            raise ApiError(400, "Выберите от 1 до 200 заказов.")
        try:
            ids = [int(i) for i in raw]
        except (TypeError, ValueError):
            raise ApiError(400, "Неверные номера заказов.")
        if body.get("confirm") is not True:
            raise ApiError(400, "Нужно подтверждение удаления.")
        self._check_dev_password(user, str(body.get("password") or ""))
        res = self.db.delete_history_orders(ids)
        if res["deleted"]:
            self._log_admin(user, "Удалил заказы из истории", details="№" + ", №".join(str(i) for i in res["deleted"]))
        return res

    async def admin_history(self, user, body, query) -> dict[str, Any]:
        """История всех заказов: кто отвёз, куда и что. Фильтры: status, courier."""
        limit, offset = self._page(query)
        status = query.get("status", "delivered")
        if status not in HISTORY_FILTERS:
            raise ApiError(400, "Неизвестный фильтр статуса.")
        courier_id = None
        raw = query.get("courier")
        if raw not in (None, "", "all"):
            try:
                courier_id = int(raw)
            except ValueError:
                raise ApiError(400, "Неверный курьер.")
        search = _optional_text(query.get("q"), 100)
        rows, total, total_sum = self.db.get_orders_history(
            statuses=HISTORY_FILTERS[status], courier_id=courier_id, limit=limit, offset=offset, search=search,
        )
        orders = self.orders_json(rows, "admin")
        extras = self.db.order_extras([int(o["id"]) for o in orders])
        chats = self.db.order_chat_counts([int(o["id"]) for o in orders])
        for o in orders:
            o["extra"] = extras.get(int(o["id"]), {})
            o["chat_count"] = chats.get(int(o["id"]), 0)
        return {
            "orders": orders,
            "total": total,
            "delivered_sum": total_sum,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "couriers": [
                {"id": c["id"], "name": c["name"], "active": bool(c["is_active"])}
                for c in self.db.get_couriers(include_inactive=True)
            ],
        }

    async def admin_status(self, user, body, query, order_id: int) -> dict[str, Any]:
        status = body.get("status")
        if status not in ADMIN_STATUS_TARGETS:
            raise ApiError(400, "Недопустимый статус.")
        current = self.db.get_order(order_id)
        if not current:
            raise ApiError(404, "Заказ не найден.")
        cur = _row(current)
        if status == "cancelled" and int(cur.get("prepay_amount") or 0) > 0 and cur.get("prepay_taken_at") \
                and not body.get("confirm_prepay"):
            raise ApiError(409, "Вы отменяете заказ, по которому внесена предоплата.", {
                "code": "prepay_cancel", "prepay": int(cur["prepay_amount"]),
                "customer": {"name": cur.get("customer_name"), "phone": cur.get("customer_phone"),
                             "username": (cur.get("username") or None), "user_id": cur.get("user_id")}})
        order = self.db.update_order_status(order_id, status)
        if not order:
            raise ApiError(
                409,
                "Статус нельзя изменить: заказ уже завершён/отменён, или у него ещё нет курьера.",
            )
        if status == "cancelled" and int(cur.get("prepay_amount") or 0) > 0 and cur.get("prepay_taken_at"):
            self._log_admin(user, "Отмена заказа с предоплатой (списана у курьера)", target_user_id=cur.get("user_id"),
                            target_name=cur.get("customer_name"), amount=int(cur["prepay_amount"]), order_id=order_id)
        await self._safe(
            self.bridge.admin_status(order_id, _row(current), status), f"смена статуса {order_id}", order_id
        )
        return {"order": self.order_json(order, "admin")}

    async def admin_assign(self, user, body, query, order_id: int) -> dict[str, Any]:
        courier_id = _int(body.get("courier_id"), "курьер", 1)
        self._check_not_silent(courier_id, [order_id])
        current = self.db.get_order(order_id)
        self.db.release_individual(order_id)  # индивидуальный: админ сам выбрал курьера
        order = self.db.assign_courier(order_id, courier_id)
        if not order:
            raise ApiError(409, "Курьер недоступен или заказ уже кому-то назначен.")
        await self._safe(self.bridge.admin_assigned(order_id, _row(current)), f"назначение {order_id}", order_id)
        return {"order": self.order_json(order, "admin")}

    async def admin_delivery_fee(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Индивидуальный заказ (не Москва): админ вписывает стоимость доставки — она
        прибавляется к сумме заказа. {"fee": 1500, "release": true} — и сразу передать курьерам."""
        fee = _int(body.get("fee"), "стоимость доставки", 0, 1_000_000)
        current = self.db.get_order(order_id)
        if not current or not int(current["individual"] or 0):
            raise ApiError(409, "Стоимость доставки назначается только индивидуальному заказу (не Москва).")
        order = self.db.set_delivery_fee(order_id, fee)
        if not order:
            raise ApiError(409, "Заказ уже в пути или закрыт — стоимость доставки менять нельзя.")
        if order["pay_on_delivery"] and order["payment_method"] == "crypto":
            # Сумма в монете — от новой суммы заказа, по текущему курсу.
            quotes = await self._crypto_quotes(int(order["total"]))
            if quotes:
                self.db.set_order_pay_on_delivery(order_id, crypto.dumps(quotes), order["payment_qr_id"])
        self._log_admin(user, "Доставка за пределы Москвы: стоимость", target_user_id=order["user_id"],
                        target_name=order["customer_name"], amount=fee, order_id=order_id)
        await self._safe(self.bridge.bonus_event(
            f"🚚 Заказ №{order_id}: стоимость доставки за пределы Москвы — {money(fee)} — админ {user.display_name}"),
            "стоимость доставки")
        sent = None
        if body.get("release") and order["status"] == "new" and order["courier_id"] is None:
            self.db.release_individual(order_id)
            sent = None  # заказы раздаёт администратор — «передать курьерам» больше нет (28.10.2026)
        return {"order": self.order_json(self.db.get_order(order_id), "admin"), "sent": sent}

    async def admin_delivery_toll(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Платная дорога на маршруте: {"toll": 400} — доплата к доставке за пределы Москвы (0 — убрать)."""
        toll = _int(body.get("toll"), "платная дорога", 0, 100_000)
        current = self.db.get_order(order_id)
        if not current or not int(current["individual"] or 0):
            raise ApiError(409, "Платная дорога — только для доставки за пределы Москвы.")
        order = self.db.set_delivery_toll(order_id, toll)
        if not order:
            raise ApiError(409, "Заказ уже закрыт — доставку менять нельзя.")
        if order["pay_on_delivery"] and order["payment_method"] == "crypto":
            quotes = await self._crypto_quotes(int(order["total"]))
            if quotes:
                self.db.set_order_pay_on_delivery(order_id, crypto.dumps(quotes), order["payment_qr_id"])
        self._log_admin(user, "Доставка за пределы Москвы: платная дорога", target_user_id=order["user_id"],
                        target_name=order["customer_name"], amount=toll, order_id=order_id)
        await self._safe(self.bridge.bonus_event(
            f"🛣 Заказ №{order_id}: платная дорога {money(toll)}, доставка теперь {money(int(order['delivery_fee']))} — админ {user.display_name}"),
            "платная дорога")
        return {"order": self.order_json(order, "admin")}

    async def admin_order_edit_options(self, user, body, query, order_id: int) -> dict[str, Any]:
        """Данные для окна «✏️ Изменить» заказа: что в заказе сейчас и что можно добавить."""
        o = self.db.get_order(order_id)
        if not o:
            raise ApiError(404, "Заказ не найден.")
        is_return = (o["kind"] or "order") == "return"
        returns = [{"product_id": int(r["product_id"]), "name": r["product_name"], "quantity": int(r["quantity"]),
                    "unit_price": int(r["unit_price"])} for r in self.db.get_order_returns(order_id) if r["product_id"] is not None]
        return_choices = [{"id": int(p["id"]), "name": p["name"], "cash": int(p["return_price"]), "points": int(p["return_points"] or 0)}
                          for p in self.db.return_products()]
        items = [{"product_id": int(i["product_id"]), "name": i["product_name"], "variant": i["variant"],
                  "quantity": int(i["quantity"]), "unit_price": int(i["unit_price"])}
                 for i in self.db.get_order_items(order_id) if not i["is_gift"] and i["product_id"] is not None]
        if is_return:
            mode = o["return_mode"] or "cash"
            products = [{"id": int(p["id"]), "name": p["name"],
                         "price": int(p["return_points"] if mode == "points" else p["return_price"]), "price_exchange": None}
                        for p in self.db.return_products() if mode != "points" or int(p["return_points"] or 0) > 0]
        else:
            products = [{"id": int(p["id"]), "name": p["name"], "price": int(p["price"]),
                         "price_exchange": None if p["price_exchange"] is None else int(p["price_exchange"])}
                        for p in self.db.get_products()]
        row = self.db.get_order(order_id)
        return {"order_id": order_id, "is_return": is_return, "status": o["status"], "return_mode": o["return_mode"] or "cash",
                "returns": returns, "return_choices": return_choices,
                "items": items, "products": products,
                "lift_floors": int(o["floor_lift_floors"] or 0),
                "lift_price": int(o["floor_lift_price"] or 0) or self.db.get_floor_lift_price(),
                "descent_count": int(o["descent_count"] or 0),
                "descent_price": int(o["descent_price"] or 0) or self.db.courier_rates()["descent"],
                "total": int(row["total"]), "return_payout": int(o["return_payout"] or 0), "return_points": int(o["return_points"] or 0)}

    async def admin_order_edit(self, user, body, query, order_id: int) -> dict[str, Any]:
        """✏️ Изменить заказ (31.10.2026, только админ, пока заказ активен): товары, подъёмы; в возврате — состав и спуски."""
        raw = body.get("items")
        if not isinstance(raw, list):
            raise ApiError(400, "Нет списка товаров.")
        items = []
        for it in raw:
            if not isinstance(it, dict):
                raise ApiError(400, "Неверный список товаров.")
            price = it.get("unit_price")
            price = None if price in (None, "") else _int(price, "сумма выкупа", 0, 1_000_000)
            items.append({"product_id": _int(it.get("product_id"), "товар", 1), "variant": it.get("variant"), "unit_price": price,
                          "quantity": _int(it.get("quantity") if it.get("quantity") is not None else 0, "количество", 0, 99)})
        lift = None if body.get("lift_floors") is None else _int(body.get("lift_floors"), "подъёмы", 0, 99)
        descents = None if body.get("descent_count") is None else _int(body.get("descent_count"), "спуски", 0, 99)
        returns = None
        if body.get("returns") is not None:
            if not isinstance(body["returns"], list):
                raise ApiError(400, "Неверный список возврата.")
            returns = [{"product_id": _int(r.get("product_id"), "баллон", 1), "quantity": _int(r.get("quantity") or 0, "количество", 0, 20),
                        "unit_price": None if r.get("unit_price") in (None, "") else _int(r.get("unit_price"), "сумма выкупа", 0, 1_000_000)}
                       for r in body["returns"] if isinstance(r, dict)]
        before_row = self.db.get_order(order_id)
        if not before_row:
            raise ApiError(404, "Заказ не найден.")
        def _brief(oid: int) -> str:
            row = self.db.get_order(oid)
            is_ret = (row["kind"] or "order") == "return"
            txt = ", ".join(f"{i['product_name']} × {i['quantity']}" + (f" (по {int(i['unit_price'])})" if is_ret else "")
                            for i in self.db.get_order_items(oid) if not i["is_gift"])
            ret = ", ".join(f"{r['product_name']} × {r['quantity']} (по {int(r['unit_price'])})" for r in self.db.get_order_returns(oid))
            return txt + (f"; ♻️ возврат: {ret}" if ret else "")
        before_items = _brief(order_id)
        try:
            res = self.db.admin_edit_order(order_id, items, lift, descents, returns,
                                           "points" if body.get("return_mode") == "points" else "cash")
        except ValueError as exc:
            raise ApiError(409, str(exc))
        order = self.db.get_order(order_id)
        after_items = _brief(order_id)
        if res["is_return"]:
            money_txt = (f"начислят {res['after']['points']} баллов" if (order["return_mode"] or "cash") == "points"
                         else f"отдать покупателю {money(res['after']['payout'])}")
        else:
            money_txt = f"сумма {money(res['after']['total'])} (было {money(res['before']['total'])})"
        text = f"✏️ Состав заказа №{order_id} изменён админом.\nБыло: {before_items}\nСтало: {after_items}\n{money_txt[:1].upper() + money_txt[1:]}."
        self._log_admin(user, "Изменён состав заказа", target_user_id=order["user_id"], target_name=order["customer_name"],
                        order_id=order_id, details=f"{before_items} → {after_items}; {money_txt}")
        await self._safe(self.bridge.order_changed(order_id, text), "изменение состава заказа", order_id)
        warnings = []
        if order["courier_id"]:
            short = self.db.stock_shortage(self.db.get_courier(int(order["courier_id"])), order_id) if hasattr(self.db, "get_courier") else {}
            if short:
                warnings.append("У курьера не хватает: " + ", ".join(f"{n} — нужно {a}, есть {b}" for n, (a, b) in short.items()))
        if not res["is_return"] and order["payment_method"] != "cash_on_delivery" and res["after"]["total"] != res["before"]["total"]:
            warnings.append("Оплата не наличными: сумма заказа изменилась — договоритесь с клиентом о доплате/возврате.")
        return {"order": self.order_json(order, "admin"), "warnings": warnings}

    # ------------------------------------------------------------ admin: products
    async def admin_products(self, user, body, query) -> dict[str, Any]:
        return {
            "products": [self.product_json(p, admin=True) for p in self.db.get_products()],
            "archived": [self.product_json(p, admin=True) for p in self.db.get_archived_products()],
        }

    @staticmethod
    def _photo_data_url(value: Any) -> str:
        text = str(value or "")
        if len(text) > MAX_PRODUCT_PHOTO_CHARS:
            raise ApiError(413, "Фото слишком большое — уменьшите его.")
        decode_data_url(text, MAX_PRODUCT_PHOTO_CHARS)
        return text

    def _save_bonus(self, product_id: int, body: dict, price_exchange) -> None:
        """Бонус товара (28.10.2026): товар-подарок из каталога (bonus_product_id), bonus_qty штук на каждую
        штуку товара, описание, к «Новому» / к «Замене». Пусто — бонуса нет. Без товара бонус не задаётся."""
        raw = body.get("bonus_product_id")
        if raw in (None, "", 0, "0"):
            self.db.set_product_bonus(product_id, None, 1, None, True, True)
            return
        gift_id = _int(raw, "Товар-подарок", 1)
        if gift_id == product_id:
            raise ApiError(400, "Товар не может быть подарком сам к себе.")
        gift = self.db.get_product(gift_id)
        if not gift or not gift["is_active"]:
            raise ApiError(400, "Товар-подарок не найден в каталоге.")
        qty = _int(body.get("bonus_qty", 1), "Количество подарков", 1, 100)
        desc = _optional_text(body.get("bonus_desc"), 1000)
        for_new = bool(body.get("bonus_new", True))
        for_ex = bool(body.get("bonus_exchange", True))
        if price_exchange is None:
            for_new, for_ex = True, True
        if not for_new and not for_ex:
            raise ApiError(400, "Отметьте, к чему применяется бонус: к «Новому», к «Замене» или к обоим.")
        self.db.set_product_bonus(product_id, gift_id, qty, desc, for_new, for_ex)

    async def admin_product_create(self, user, body, query) -> dict[str, Any]:
        name = _text(body.get("name"), "Название", 2, 200)
        description = _text(body.get("description"), "Описание", 0, 2000)
        price = _int(body.get("price"), "Цена «Новый»", 0)
        price_exchange = body.get("price_exchange")
        price_exchange = None if price_exchange in (None, "") else _int(price_exchange, "Цена «Замена»", 0)
        product_id = self.db.add_product(name, description, price, price_exchange, None, None)
        if body.get("bonus_product_id") not in (None, "", 0, "0"):
            self._save_bonus(product_id, body, price_exchange)
        if body.get("partner_reward") not in (None, ""):
            self.db.set_product_partner_reward(product_id, _int(body["partner_reward"], "Баллы партнёру", 0, 1_000_000))
        if body.get("return_price") not in (None, ""):
            self.db.set_product_return_price(product_id, _int(body["return_price"], "Цена возврата", 0, 1_000_000))
        if body.get("return_points") not in (None, ""):
            self.db.set_product_return_points(product_id, _int(body["return_points"], "Баллы за возврат", 0, 10_000_000))
        if body.get("photo"):
            self.db.update_product_webapp_photo(product_id, self._photo_data_url(body["photo"]))
        return {"product": self.product_json(self.db.get_product(product_id), admin=True)}

    async def admin_product_update(self, user, body, query, product_id: int) -> dict[str, Any]:
        product = self.db.get_product(product_id)
        if not product:
            raise ApiError(404, "Товар не найден.")
        removed = 0
        if "name" in body:
            self.db.update_product_name(product_id, _text(body["name"], "Название", 2, 200))
        if "description" in body:
            self.db.update_product_description(product_id, _text(body["description"], "Описание", 2, 2000))
        if "price" in body or "price_exchange" in body:
            price = _int(body.get("price", product["price"]), "Цена «Новый»", 0)
            raw_ex = body["price_exchange"] if "price_exchange" in body else product["price_exchange"]
            price_exchange = None if raw_ex in (None, "") else _int(raw_ex, "Цена «Замена»", 0)
            removed = self.db.update_product_prices(product_id, price, price_exchange)
        if "bonus_product_id" in body:
            self._save_bonus(product_id, body, self.db.get_product(product_id)["price_exchange"])
        if "return_price" in body:  # пусто или 0 — баллон на возврат не принимается
            raw = body["return_price"]
            self.db.set_product_return_price(product_id, 0 if raw in (None, "") else _int(raw, "Цена возврата", 0, 1_000_000))
        if "return_points" in body:  # пусто или 0 — баллами этот баллон не принимается
            raw = body["return_points"]
            self.db.set_product_return_points(product_id, 0 if raw in (None, "") else _int(raw, "Баллы за возврат", 0, 10_000_000))
        if "partner_reward" in body:  # пусто — по умолчанию по объёму баллона
            raw = body["partner_reward"]
            self.db.set_product_partner_reward(
                product_id, None if raw in (None, "") else _int(raw, "Баллы партнёру", 0, 1_000_000))
        if "photo" in body:
            self.db.update_product_webapp_photo(
                product_id, self._photo_data_url(body["photo"]) if body["photo"] else None
            )
        return {
            "product": self.product_json(self.db.get_product(product_id), admin=True),
            "removed_cart_rows": removed,
        }

    async def admin_products_order(self, user, body, query) -> dict[str, Any]:
        """Порядок товаров в каталоге (перетаскивание): {"ids": [id, …]} — все товары в продаже, сверху вниз."""
        ids = body.get("ids")
        active = [int(p["id"]) for p in self.db.get_products()]
        try:
            ids = [int(i) for i in ids] if isinstance(ids, list) else None
        except (TypeError, ValueError):
            ids = None
        if ids is None or len(ids) != len(set(ids)) or set(ids) != set(active):
            raise ApiError(409, "Список товаров изменился — обновите страницу.")
        self.db.reorder_products(ids)
        if ids != active:
            names = self.db.product_names(ids)
            self._log_admin(user, "Товары: новый порядок в каталоге", details=", ".join(names.get(i, str(i)) for i in ids))
        return await self.admin_products(user, {}, {})

    async def admin_product_hide(self, user, body, query, product_id: int) -> dict[str, Any]:
        if not self.db.delete_product(product_id):
            raise ApiError(409, "Товар уже скрыт или не найден.")
        return {"ok": True}

    async def admin_product_restore(self, user, body, query, product_id: int) -> dict[str, Any]:
        if not self.db.restore_product(product_id):
            raise ApiError(409, "Товар уже в каталоге или не найден.")
        return {"ok": True}

    # ------------------------------------------------------------ admin: couriers
    WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")

    async def admin_courier_stats(self, user, body, query) -> dict[str, Any]:
        """Статистика курьеров (28.10.2026): неделя пн–вс или календарный месяц (МСК), по дням.
        ?period=week|month&offset=0 (−1 — прошлая неделя / прошлый месяц). Деньги — из журнала курьеров:
        собрано наличными за заказы, отдано за пустые баллоны, сдать в кассу (= собрано − отдано), заработал."""
        period = query.get("period", "week")
        if period not in ("week", "month"):
            raise ApiError(400, "Период: неделя или месяц.")
        offset = _int(query.get("offset") or 0, "период", -120, 0)
        today = datetime.now(MSK).date()
        if period == "week":
            start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            end = start + timedelta(days=7)
            label = f"{start:%d.%m} — {end - timedelta(days=1):%d.%m.%Y}"
        else:
            y, m = today.year, today.month + offset
            while m < 1:
                y, m = y - 1, m + 12
            start = today.replace(year=y, month=m, day=1)
            end = start.replace(year=y + (m == 12), month=1 if m == 12 else m + 1)
            label = f"{self.MONTHS[m - 1]} {y}"
        to_utc = lambda d: datetime(d.year, d.month, d.day, tzinfo=MSK).astimezone(timezone.utc).isoformat(timespec="seconds")
        days = []
        d = start
        while d < end:
            days.append({"date": d.isoformat(), "label": f"{self.WEEKDAYS[d.weekday()]} {d:%d.%m}"})
            d += timedelta(days=1)
        keys = ("orders", "pickups", "balloons", "cash", "paid", "to_hand", "earned")
        couriers: dict[int, dict[str, Any]] = {}
        for c in self.db.get_couriers(include_inactive=True):
            couriers[int(c["id"])] = {"id": int(c["id"]), "name": c["name"], "active": bool(c["is_active"]),
                                      "color": int(c["marker_idx"] or 0) % self.db.MARKER_COLORS,
                                      **{k: 0 for k in keys}, "days": {}}
        for e in self.db.courier_money_events(to_utc(start), to_utc(end)):
            c = couriers.get(int(e["courier_id"]))
            if c is None:
                continue
            day = datetime.fromisoformat(e["created_at"]).astimezone(MSK).date().isoformat()
            row = c["days"].setdefault(day, {k: 0 for k in keys})
            add = {"orders": int(e["kind"] == "delivered"), "pickups": int(e["kind"] == "return_picked"),
                   "balloons": int(e["balloons"]), "cash": int(e["cash"]), "paid": -int(e["returns"]),
                   "to_hand": int(e["cash"]) + int(e["returns"]) - int(e.get("keep") or 0), "earned": int(e["earned"])}
            for k, v in add.items():
                row[k] += v
                c[k] += v
        rows = [c for c in couriers.values() if c["active"] or c["days"]]
        rows.sort(key=lambda c: (-c["earned"], c["name"]))
        return {"period": period, "offset": offset, "label": label, "from": start.isoformat(),
                "to": (end - timedelta(days=1)).isoformat(), "days": days, "couriers": rows,
                "totals": {k: sum(c[k] for c in rows) for k in keys}}

    async def admin_couriers(self, user, body, query) -> dict[str, Any]:
        result = []
        for c in self.db.get_couriers(include_inactive=True):
            count = int(c["rating_count"] or 0)
            result.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "phone": c["phone"],
                    "telegram_id": c["telegram_id"],
                    "username": c["username"],
                    "is_active": bool(c["is_active"]),
                    "rating": round(int(c["rating_sum"]) / count, 1) if count else None,
                    "rating_count": count,
                    "shift": {**self.db.courier_shift(c), "stock": self.db.courier_stock(c)},
                    "counters": self.db.courier_counters(int(c["id"])),
                    "empties": self.db.courier_empties(int(c["id"])),
                    "reload_pending": self._named_stock(c["reload_pending"]) if c["reload_pending"] else None,
                    "shift_request": self.db.shift_request(c),
                    "reload_pending_at": c["reload_pending_at"],
                    "color": int(c["marker_idx"] or 0) % self.db.MARKER_COLORS,
                    "live": self.db.courier_live(c),
                }
            )
        return {"couriers": result, "courier_rates": self.db.courier_rates()}

    # ------------------------------------------------------------ деньги курьера и журнал смен (25.10.2026)
    COUNTER_LABELS = {"earned": "Заработано", "cash": "Наличные за товары", "returns": "Возвраты",
                      "keep": "Оставил себе из наличных"}  # keep — 28.10.2026, обнуляется вместе с «Сдал»

    def _counter_name(self, body: dict[str, Any]) -> str:
        name = body.get("name")
        if name not in self.COUNTER_LABELS:
            raise ApiError(400, "Неизвестный счётчик.")
        return name

    def _reset_request_text(self, courier, names: list[str]) -> str:
        """«🏦 Пётр сдал в кассу 15 000 ₽» / «🧮 Пётр просит обнулить «Заработано»: 4 000 ₽» — по текущим суммам."""
        c = self.db.courier_counters(int(courier["id"]))
        rub = lambda v: f"{int(v):,} ₽".replace(",", " ")  # noqa: E731
        if set(names) >= {"cash", "returns"} and set(names) <= {"cash", "returns", "keep"}:
            # 28.10.2026: курьер сдаёт после вычета своего заработка (keep).
            return (f"🏦 {courier['name']} сдал в кассу {rub(c['cash'] + c['returns'] - c.get('keep', 0))} — "
                    "подтвердите, что деньги получены")
        return "🧮 " + courier["name"] + " просит обнулить " + ", ".join(
            f"«{self.COUNTER_LABELS[n]}»: {rub(abs(c[n]))}" for n in names)

    async def courier_counter_reset(self, user, body, query) -> dict[str, Any]:
        """28.10.2026 (владелец): курьер не может сам обнулять себе деньги — «Обнулить» / «Сдал» отправляют
        запрос администратору; обнуляет админ (alerts → «✅ Подтвердить»). {"cancel": true} — отменить запрос."""
        courier = self.courier_of(user.id)
        if not courier:
            raise ApiError(403, "Вы не курьер.")
        if body.get("cancel"):
            self.db.request_counter_reset(int(courier["id"]), None)
            return self._shift_json(user.id)
        raw = body.get("names") if isinstance(body.get("names"), list) else [body.get("name")]
        names = []
        for n in raw:
            names.append(self._counter_name({"name": n}))
        names = sorted(set(names))
        if self.db.counter_reset_request(courier):
            raise ApiError(409, "Запрос уже отправлен — ждём подтверждения администратора.")
        self.db.request_counter_reset(int(courier["id"]), names)
        await self._safe(self.bridge.admin_alert(self._reset_request_text(courier, names) + "\nПодтвердить — в приложении, на главной админа."),
                         "запрос обнуления счётчиков")
        return self._shift_json(user.id)

    async def admin_counter_reset_request(self, user, body, query, courier_id: int) -> dict[str, Any]:
        """Админ подтверждает или отклоняет запрос курьера обнулить счётчики."""
        c = self.db.get_courier(courier_id)
        req = self.db.counter_reset_request(c)
        if not c or not req:
            raise ApiError(409, "Запроса уже нет.")
        if body.get("approve"):
            done = []
            for name in req["names"]:
                if name in self.COUNTER_LABELS:
                    before = self.db.reset_courier_counter(courier_id, name, "admin", user.display_name)
                    done.append(f"«{self.COUNTER_LABELS[name]}»: было {before} ₽")
            self._log_admin(user, "Курьер: обнулены счётчики по запросу курьера", target_user_id=c["telegram_id"],
                            target_name=c["name"], details="; ".join(done))
            if c["telegram_id"]:
                await self._safe(self.bridge.courier_message(int(c["telegram_id"]), "✅ Администратор подтвердил: счётчики обнулены."),
                                 "ответ курьеру об обнулении")
        else:
            self._log_admin(user, "Курьер: отклонён запрос обнулить счётчики", target_user_id=c["telegram_id"], target_name=c["name"])
            if c["telegram_id"]:
                await self._safe(self.bridge.courier_message(int(c["telegram_id"]), "✖️ Администратор отклонил запрос обнулить счётчики."),
                                 "ответ курьеру об обнулении")
        self.db.request_counter_reset(courier_id, None)
        return {"alerts": self._admin_alerts()}

    async def courier_empties_set(self, user, body, query) -> dict[str, Any]:
        """Курьер сам указывает, сколько пустых баллонов у него в машине."""
        courier = self.courier_of(user.id)
        if not courier:
            raise ApiError(403, "Вы не курьер.")
        raw = body.get("empty")
        if not isinstance(raw, dict):
            raise ApiError(400, "Укажите пустые баллоны.")
        empty = {}
        for size in self.db.empty_sizes():
            empty[size] = _int(raw.get(size, 0) or 0, f"Пустые {size} л", 0, 999)
        self.db.set_courier_empties(int(courier["id"]), empty)
        return self._shift_json(user.id)

    async def admin_counter_reset(self, user, body, query, courier_id: int) -> dict[str, Any]:
        c = self.db.get_courier(courier_id)
        name = self._counter_name(body)
        before = self.db.reset_courier_counter(courier_id, name, "admin", user.display_name)
        cname = c["name"] if c else f"№{courier_id}"
        self._log_admin(user, f"Курьер: обнулён счётчик «{self.COUNTER_LABELS[name]}»",
                        target_user_id=c["telegram_id"] if c else None, target_name=cname,
                        details=f"было {before} ₽")
        return await self.admin_couriers(user, {}, {})

    async def admin_courier_rate(self, user, body, query) -> dict[str, Any]:
        rate = self.db.set_courier_delivery_rate(_int(body.get("rate"), "Ставка за доставку", 0, 1_000_000))
        self._log_admin(user, "Ставка курьеру за доставку", details=f"{rate} ₽")
        return {"courier_delivery_rate": rate}

    async def admin_courier_rates(self, user, body, query) -> dict[str, Any]:
        """Ставки курьеру (28.10.2026): {"balloon": ₽ за отвезённый баллон, "return": ₽ за забранный пустой (возврат)}."""
        rates = self.db.set_courier_rates(
            _int(body.get("balloon"), "Ставка курьеру за каждый баллон в доставке", 0, 1_000_000),
            _int(body.get("return"), "Ставка курьеру за поездку на возврат пустых баллонов", 0, 1_000_000),
            _int(body.get("descent"), "Ставка курьеру за спуск пустого баллона", 0, 1_000_000) if body.get("descent") is not None else None,
            _int(body.get("balloon_next"), "Ставка курьеру за каждый следующий баллон в заказе", 0, 1_000_000) if body.get("balloon_next") is not None else None)
        self._log_admin(user, "Ставки курьеру", details=f"за первый баллон в заказе {rates['balloon']} ₽, за каждый следующий {rates['balloon_next']} ₽, "
                        f"за поездку на возврат {rates['return']} ₽, за спуск пустого баллона {rates['descent']} ₽")
        return {"courier_rates": rates}

    def _shift_row_json(self, sh: dict[str, Any]) -> dict[str, Any]:
        def load(v):
            try:
                return json.loads(v) if v else None
            except (ValueError, TypeError):
                return None
        regions = load(sh.get("regions")) or []
        return {
            "id": sh["id"], "started_at": sh["started_at"], "ended_at": sh.get("ended_at"),
            "end_reason": sh.get("end_reason"), "open": not sh.get("ended_at"),
            "region_labels": [REGION_LABELS[r] for r in regions if r in REGION_LABELS],
            # на начало — {название: штук} (старые смены — литры баллонов)
            "stock_start": (self._named_stock(sh.get("stock_start")) if '"v": 2' in (sh.get("stock_start") or "")
                            else load(sh.get("stock_start"))),
            "stock_end": load(sh.get("stock_end")),
            "empty_end": load(sh.get("empty_end")),
            **{k: sh.get(k, 0) for k in ("earned", "cash", "returns", "keep", "deliveries", "pickups") if k in sh},
        }

    async def admin_courier_shifts(self, user, body, query, courier_id: int) -> dict[str, Any]:
        return {"shifts": [self._shift_row_json(sh) for sh in self.db.courier_shifts(courier_id)]}

    async def admin_shift_detail(self, user, body, query, shift_id: int) -> dict[str, Any]:
        found = self.db.courier_shift_detail(shift_id)
        if not found:
            raise ApiError(404, "Смена не найдена.")
        events = []
        totals = {"earned": 0, "cash": 0, "returns": 0, "keep": 0, "deliveries": 0, "pickups": 0}
        for e in found["events"]:
            try:
                data = json.loads(e["data"]) if e["data"] else {}
            except (ValueError, TypeError):
                data = {}
            totals["earned"] += int(e["earned"]); totals["cash"] += int(e["cash"]); totals["returns"] += int(e["returns"])
            totals["keep"] += int(e.get("keep") or 0)
            totals["deliveries"] += e["kind"] == "delivered"; totals["pickups"] += e["kind"] == "return_picked"
            events.append({"id": e["id"], "kind": e["kind"], "order_id": e["order_id"], "earned": int(e["earned"]),
                           "cash": int(e["cash"]), "returns": int(e["returns"]), "data": data, "at": e["created_at"]})
        return {"shift": {**self._shift_row_json(found["shift"]), **totals}, "events": events}

    async def admin_shift_request(self, user, body, query, courier_id: int) -> dict[str, Any]:
        """«В путь!» курьера: {"approve": true} — подтвердить; {"approve": true, "stock": {...}} — с
        исправленными количествами; {"approve": false} — отклонить (курьер правит и отправляет снова)."""
        approve = bool(body.get("approve"))
        stock = self._shift_stock(body["stock"]) if approve and body.get("stock") is not None else None
        c = self.db.get_courier(courier_id)
        res = self.db.resolve_shift_request(courier_id, approve, stock)
        if not res or not c:
            raise ApiError(409, "Заявки на смену уже нет.")
        final = {int(k): v for k, v in res["final"].items()}
        changed = approve and stock is not None and {k: v for k, v in final.items() if v} != {
            int(k): v for k, v in res["stock"].items() if v}
        self._log_admin(user, "Выход курьера на смену: " + ("подтверждён" if approve else "отклонён")
                        + (" (количества исправлены)" if changed else ""),
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
