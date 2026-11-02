Warning: truncated output (original token count: 86109)
Total output lines: 5668

from __future__ import annotations

import functools
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------- баллоны и партнёрские баллы
# Баллон узнаём по названию товара: отдельное слово «баллон» (не «баллона», не «баллоны»);
# ставка по умолчанию — по объёму в названии («5 л», «10л», «2 литра»).
# Баллы партнёру — за каждый баллон в доставленном заказе его клиента, одинаково за новый и
# за замену. Админ может задать свои баллы каждому баллону (products.partner_reward);
# пусто — по умолчанию по объёму.
BALLOON_DEFAULT_REWARDS = {2: 100, 5: 150, 10: 200}
_LITERS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:литр|л\b|л\.|l\b)", re.IGNORECASE)


@functools.lru_cache(maxsize=1024)
def balloon_liters(name: str | None) -> float | None:
    m = _LITERS_RE.search(name or "")
    return float(m.group(1).replace(",", ".")) if m else None


_BALLOON_WORD_RE = re.compile(r"(?<![а-яё])баллон(?![а-яё])", re.IGNORECASE)


@functools.lru_cache(maxsize=1024)
def is_balloon(name: str | None) -> bool:
    """Баллон — только товар со словом «баллон» (именно так, в единственном числе) в названии.
    «Подставка для баллона», «Баллоны …» — не баллоны."""
    return bool(_BALLOON_WORD_RE.search(name or ""))


def default_partner_reward(name: str | None) -> int:
    if not is_balloon(name):
        return 0
    liters = balloon_liters(name)
    if liters is None or not float(liters).is_integer():
        return 0
    return BALLOON_DEFAULT_REWARDS.get(int(liters), 0)


def partner_reward_for(name: str | None, custom: Any) -> int:
    """Баллы партнёру за 1 баллон этого товара (0 — не баллон)."""
    if not is_balloon(name):
        return 0
    return int(custom) if custom is not None else default_partner_reward(name)


DEFAULT_THEME: dict[str, Any] = {
    "bg_color": "#0f172a",
    "bg_gradient_to": "#1e293b",
    "accent_color": "#f97316",
    "button_style": "rounded",
    "button_fill": "lux",
    "menu_order": ["catalog", "cart", "orders_active", "orders_history", "call_admin"],
    # Optional background image (data: URL, uploaded by the admin on the
    # "Разработчик" screen) drawn over the bg_color/bg_gradient_to gradient.
    # None means "no image, gradient only".
    "bg_image_data_url": None,
    # Manual positioning of the background image, as percentages (0-100)
    # fed straight into CSS `background-position: X% Y%`, so the admin can
    # drag the focal point of an uploaded photo instead of always getting
    # a plain centered crop. 50/50 is centered (the old default behaviour).
    "bg_image_pos_x": 50,
    "bg_image_pos_y": 50,
    # "button_animation": which press animation style the app's CSS applies
    # (see style.css / the Mini App's own stylesheet for the keyframes).
    "button_animation": "premium",
}


class OutOfStockError(Exception):
    """No longer raised anywhere (stock tracking was removed) — kept only
    so older imports of it don't break."""


class CourierBusyError(Exception):
    """Raised when a courier can't be deleted because an order is in their hands."""


class ProductInUseError(Exception):
    """Raised when a product cannot be permanently deleted because it still
    appears in someone's order history."""


class BonusError(Exception):
    """Нельзя списать столько бонусов: reason = balance (больше, чем есть) |
    too_much (больше суммы товаров минус 1 ₽); limit — сколько можно."""

    def __init__(self, reason: str, limit: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.limit = max(0, int(limit))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Одно соединение на поток, открытое один раз (раньше — новое на каждый запрос:
        # открытие + 4 PRAGMA стоили ~0,3 мс, а за один экран их бывали сотни).
        self._local = threading.local()
        self.initialize()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        # WAL + NORMAL: запись без fsync на каждый коммит (рекомендованная пара
        # в документации SQLite; база не повреждается, при сбое питания могут
        # потеряться лишь последние доли секунды записей).
        conn.execute("PRAGMA synchronous = NORMAL")
        # LOWER() в SQLite меняет регистр только у латиницы — для поиска по
        # кириллице («борис» → «Борис») нужен Python-вариант.
        conn.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = local.conn = self._open()
            local.depth = 0
        local.depth += 1
        try:
            yield conn
        finally:
            local.depth -= 1
            # Внешний вызов закончился, а транзакция осталась открытой (ошибка до
            # COMMIT/ROLLBACK) — откатываем, чтобы не держать блокировку записи.
            if local.depth == 0 and conn.in_transaction:
                conn.rollback()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    admin_rating INTEGER CHECK (admin_rating BETWEEN 1 AND 5),
                    admin_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT 'Газовые баллоны',
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price INTEGER NOT NULL CHECK (price >= 0),
                    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
                    photo_file_id TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS carts (
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
                    variant TEXT NOT NULL DEFAULT 'new' CHECK (variant IN ('new', 'exchange')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, product_id, variant)
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
                    status TEXT NOT NULL DEFAULT 'new',
                    customer_name TEXT NOT NULL,
                    customer_phone TEXT NOT NULL,
                    delivery_address TEXT NOT NULL,
                    payment_method TEXT NOT NULL DEFAULT 'cash_on_delivery',
                    total INTEGER NOT NULL CHECK (total >= 0),
                    inventory_released INTEGER NOT NULL DEFAULT 0,
                    courier_id INTEGER REFERENCES couriers(id) ON DELETE SET NULL,
                    location_lat REAL,
                    location_lon REAL,
                    courier_last_lat REAL,
                    courier_last_lon REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                    product_name TEXT NOT NULL,
                    unit_price INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    variant TEXT NOT NULL DEFAULT 'new' CHECK (variant IN ('new', 'exchange'))
                );

                CREATE TABLE IF NOT EXISTS couriers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT,
                    telegram_id INTEGER UNIQUE,
                    username TEXT,
                    rating_sum INTEGER NOT NULL DEFAULT 0,
                    rating_count INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS courier_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    courier_id INTEGER REFERENCES couriers(id) ON DELETE CASCADE,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    channel TEXT NOT NULL CHECK (channel IN ('private', 'group')),
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'won', 'closed')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS courier_ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
                    courier_id INTEGER NOT NULL REFERENCES couriers(id) ON DELETE RESTRICT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
                    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                    created_at TEXT NOT NULL
                );

                -- Оценки и комментарии курьеров о покупателях. Видят ТОЛЬКО
                -- админ и курьеры (в Mini App покупателю не отдаются).
                -- rating NULL — просто комментарий; оценка — одна на заказ.
                -- order_id ON DELETE SET NULL: при очистке истории заказов
                -- отзывы о покупателе сохраняются.
                CREATE TABLE IF NOT EXISTS customer_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    courier_id INTEGER REFERENCES couriers(id) ON DELETE SET NULL,
                    courier_name TEXT,
                    rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
                    comment TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_customer_reviews_user
                    ON customer_reviews (user_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_reviews_order_rating
                    ON customer_reviews (order_id) WHERE rating IS NOT NULL;

                -- Архив продаж для выгрузки в Excel. Перед «Очистить всю историю
                -- заказов» строки всех заказов копируются сюда (товары и услуги),
                -- чтобы общая выгрузка считала с первого заказа и помечала
                -- сброшенные строки. order_no — номер заказа на момент сброса
                -- (после сброса нумерация заказов начинается с 1 заново).
                CREATE TABLE IF NOT EXISTS sales_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wipe_id INTEGER NOT NULL,
                    order_no INTEGER NOT NULL,
                    order_status TEXT NOT NULL,
                    order_created_at TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    line_total INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sales_archive_time ON sales_archive (order_created_at);
                -- Журнал сбросов истории заказов (из бота и из приложения).
                CREATE TABLE IF NOT EXISTS history_wipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wiped_at TEXT NOT NULL,
                    orders_deleted INTEGER NOT NULL
                );
                -- «⬇️🗑 Скачать и обнулить» (владелец 28.10.2026): таблица №2 начинается с чистого листа
                -- с момента сброса; в приложении ничего не удаляется, таблица №1 ведётся постоянно и
                -- отмечает периоды. Период N+1 начинается в момент at.
                CREATE TABLE IF NOT EXISTS export_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    admin_id INTEGER,
                    admin_name TEXT
                );
                -- Баллы партнёров: + начисление за заказ приглашённого клиента,
                -- − списание админом. Без внешних ключей — переживает сброс истории.
                CREATE TABLE IF NOT EXISTS partner_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id INTEGER NOT NULL,
                    order_id INTEGER,
                    referee_id INTEGER,
                    points INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'writeoff', 'spend', 'refund')),
                    reason TEXT,
                    order_total INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_partner_points_partner ON partner_points (partner_id, id);
                -- Бонусы покупателей (1 бонус = 1 ₽): + начисление за доставленный
                -- заказ, − списание при оформлении, + возврат при отмене, ± вручную
                -- админом. Баланс = сумма amount. Без внешних ключей — переживает
                -- сброс истории заказов.
                CREATE TABLE IF NOT EXISTS customer_bonuses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    amount INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'spend', 'refund', 'manual', 'transfer_out', 'transfer_in')),
                    reason TEXT,
                    order_total INTEGER,
                    percent REAL,
                    created_at TEXT NOT NULL,
                    counterpart_id INTEGER,
                    fee INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_customer_bonuses_user ON customer_bonuses (user_id, id);
                -- Журнал действий администраторов (кто начислил/списал бонусы, баллы и т. п.).
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    admin_name TEXT,
                    action TEXT NOT NULL,
                    target_user_id INTEGER,
                    target_name TEXT,
                    amount INTEGER,
                    balance_after INTEGER,
                    order_id INTEGER,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_actions_admin ON admin_actions (admin_id, id);
                -- «📞 Позвать администратора»: висит в уведомлениях админа, пока не «Решено».
                -- Чат покупателя с администрацией (25.10.2026): от имени «Администрация»,
                -- admin_id — кто из админов ответил (покупателю не показывается).
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    from_admin INTEGER NOT NULL DEFAULT 0,
                    admin_id INTEGER,
                    text TEXT,
                    has_photo INTEGER NOT NULL DEFAULT 0,
                    order_id INTEGER,
                    created_at TEXT NOT NULL,
                    read_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_support_user ON support_messages (user_id, id);
                -- Фото из чата хранятся здесь же (в Telegram не уходят).
                CREATE TABLE IF NOT EXISTS support_photos (
                    message_id INTEGER PRIMARY KEY,
                    mime TEXT NOT NULL,
                    data BLOB NOT NULL
                );
                -- Чат курьера и покупателя внутри заказа (25.10.2026): без раскрытия Telegram.
                CREATE TABLE IF NOT EXISTS order_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    from_courier INTEGER NOT NULL DEFAULT 0,
                    courier_id INTEGER,
                    text TEXT,
                    has_photo INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    customer_id INTEGER,
                    customer_name TEXT,
                    courier_name TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_order_messages ON order_messages (order_id, id);
                CREATE TABLE IF NOT EXISTS order_message_photos (
                    message_id INTEGER PRIMARY KEY,
                    mime TEXT NOT NULL,
                    data BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by INTEGER
                );
                -- Личная программа партнёра: свои баллы за баллон (поверх общей ставки товара).
                CREATE TABLE IF NOT EXISTS partner_rewards (
                    partner_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    points INTEGER NOT NULL CHECK (points >= 0),
                    PRIMARY KEY (partner_id, product_id)
                );
                -- Начисление, списание и возврат — не больше одного на заказ.
                CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_bonuses_order
                    ON customer_bonuses (order_id, kind)
                    WHERE order_id IS NOT NULL AND kind IN ('accrual', 'spend', 'refund');
                -- Заявки «🤝 Реферальная программа»: kind = partner (партнёрская
                -- программа — крупные объёмы и поставки) или loyalty (система
                -- лояльности для покупателя и его друзей). Одна заявка каждого вида
                -- на человека. Условия владелец продумает позже — пока сбор заявок.
                CREATE TABLE IF NOT EXISTS referral_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL DEFAULT 'loyalty' CHECK (kind IN ('partner', 'loyalty')),
                    created_at TEXT NOT NULL,
                    UNIQUE (user_id, kind)
                );

                -- QR-коды для оплаты ПРИ КУРЬЕРЕ (СБП и криптокошельки). Их
                -- добавляет админ, видят курьеры: показывают покупателю на месте.
                -- kind: sbp | crypto; currency — код монеты (USDT, BTC…) для crypto.
                CREATE TABLE IF NOT EXISTS payment_qr_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('sbp', 'crypto')),
                    title TEXT NOT NULL,
                    currency TEXT,
                    network TEXT,
                    file_id TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                -- Пароль на раздел «Разработчик» (01.11.2026): хранится только хэш.
                CREATE TABLE IF NOT EXISTS dev_access (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    salt TEXT NOT NULL,
                    pw_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shop_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    catalog_photo_file_id TEXT,
                    crypto_qr_file_id TEXT,
                    sbp_qr_file_id TEXT,
                    floor_lift_price INTEGER NOT NULL DEFAULT 0
                );

                -- Visual theme for the Mini App ("Разработчик" screen), set by
                -- the admin. One row, one JSON blob: background, button style,
                -- menu order. The bot/webapp code never hardcodes a theme —
                -- this table is the single source of truth, with a baked-in
                -- default applied only if the row is missing (first run).
                CREATE TABLE IF NOT EXISTS app_theme (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    theme_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Журнал смен курьера (25.10.2026): смена от «В путь!» до «Закончить смену»
                -- (или автоматического ухода офлайн). Перерыв смену не закрывает.
                CREATE TABLE IF NOT EXISTS courier_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    courier_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    end_reason TEXT,
                    regions TEXT,
                    stock_start TEXT,
                    stock_end TEXT,
                    empty_end TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_courier_shifts_courier ON courier_shifts (courier_id, id);

                -- Всё, что курьер делал, и движение денег (25.10.2026). Деньги — в earned
                -- (его заработок), cash (наличные за товары), returns (отдано покупателям за
                -- возвраты, со знаком минус). Счётчики = сумма после последнего обнуления
                -- (kind reset_earned / reset_cash / reset_returns). order_id — без внешнего
                -- ключа: история остаётся и после очистки заказов; data — снимок заказа (JSON).
                CREATE TABLE IF NOT EXISTS courier_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    courier_id INTEGER NOT NULL,
                    shift_id INTEGER,
                    kind TEXT NOT NULL,
                    order_id INTEGER,
                    earned INTEGER NOT NULL DEFAULT 0,
                    cash INTEGER NOT NULL DEFAULT 0,
                    returns INTEGER NOT NULL DEFAULT 0,
                    data TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_courier_events_courier ON courier_events (courier_id, id);
                CREATE INDEX IF NOT EXISTS idx_courier_events_shift ON courier_events (shift_id, id);

                -- «🔄 Обмен» (28.10.2026): курьер А отдаёт курьеру Б полные товары и пустые баллоны;
                -- 30 секунд можно отменить, потом заявка уходит админу; остатки меняются после одобрения.
                CREATE TABLE IF NOT EXISTS courier_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_id INTEGER NOT NULL,
                    to_id INTEGER NOT NULL,
                    items TEXT NOT NULL,
                    empties TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    admin_name TEXT,
                    alerted INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_courier_transfers_status ON courier_transfers (status, id);

                CREATE TABLE IF NOT EXISTS courier_location_broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    courier_source_chat_id INTEGER NOT NULL,
                    courier_source_message_id INTEGER NOT NULL,
                    customer_chat_id INTEGER NOT NULL,
                    customer_message_id INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_courier_loc_source
                    ON courier_location_broadcasts
                        (courier_source_chat_id, courier_source_message_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_courier_loc_order
                    ON courier_location_broadcasts (order_id, is_active);
                """
            )
            shop_settings_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(shop_settings)").fetchall()
            }
            if "crypto_qr_file_id" not in shop_settings_columns:
                conn.execute("ALTER TABLE shop_settings ADD COLUMN crypto_qr_file_id TEXT")
            if "sbp_qr_file_id" not in shop_settings_columns:
                conn.execute("ALTER TABLE shop_settings ADD COLUMN sbp_qr_file_id TEXT")
            if "floor_lift_price" not in shop_settings_columns:
                # Услуга «Подъём на этаж»: фиксированная цена в рублях,
                # задаётся админом. 0 — услуга выключена.
                conn.execute(
                    "ALTER TABLE shop_settings ADD COLUMN floor_lift_price INTEGER NOT NULL DEFAULT 0"
                )
            for column, definition in (
                # Быстрые переключатели оплаты для всех покупателей (1 — включено).
                ("sbp_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("crypto_enabled", "INTEGER NOT NULL DEFAULT 1"),
                # Курьеры работают строго по регионам (1) или видят все заказы (0).
                ("regions_enabled", "INTEGER NOT NULL DEFAULT 0"),
                # Объявление на главном экране магазина (NULL / пусто — плашки нет).
                ("announcement", "TEXT"),
                ("announcement_updated_at", "TEXT"),
                # Приветствие под «Привет, …!» на главном экране (NULL — стандартное).
                ("home_greeting", "TEXT"),
                # Версия партнёрской ссылки: админ нажимает «Выслать новую ссылку» — +1 (30.10.2026).
                ("partner_link_version", "INTEGER NOT NULL DEFAULT 0"),
                # Доставка за пределы Москвы (25.10.2026): база + ₽ за км по дорогам за МКАД.
                # По умолчанию 500 ₽ + 50 ₽/км (владелец, 25.10.2026). ₽ за км = 0 — авторасчёт выключен.
                ("outside_base_price", "INTEGER NOT NULL DEFAULT 500"),
                ("outside_km_price", "INTEGER NOT NULL DEFAULT 50"),
                # Заработок курьера за одну доставку или возврат баллона, ₽ (25.10.2026).
                ("courier_delivery_rate", "INTEGER NOT NULL DEFAULT 1000"),
                # 28.10.2026 (вместо ставки за доставку): курьеру за каждый отвезённый полный баллон
                # («Новый» и «Замена» — одинаково) и за каждый забранный пустой в заказе «Возврат баллона».
                # По умолчанию 0 — цены вписывает владелец; действует с момента изменения.
                ("courier_rate_balloon", "INTEGER NOT NULL DEFAULT 0"),
                # 30.10.2026: ставка за каждый СЛЕДУЮЩИЙ баллон в том же заказе (первый — courier_rate_balloon).
                # NULL — не задана: тогда все баллоны по одной ставке (как раньше).
                ("courier_rate_balloon_next", "INTEGER"),
                ("courier_rate_return", "INTEGER NOT NULL DEFAULT 1500"),  # за поездку на возврат (28.10.2026)
                # «Ставка курьеру за спуск пустого баллона» (возврат), 28.10.2026: платит покупатель (из выплаты).
                ("courier_rate_descent", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in shop_settings_columns:
                    conn.execute(f"ALTER TABLE shop_settings ADD COLUMN {column} {definition}")
            if "courier_rate_return_trip" not in shop_settings_columns:
                # 28.10.2026 (владелец): за возврат пустых баллонов курьер получает 1 500 ₽ за поездку
                # (раньше — ставка за каждый забранный баллон). Один раз ставим 1 500 ₽, дальше — в админке.
                conn.execute("ALTER TABLE shop_settings ADD COLUMN courier_rate_return_trip INTEGER NOT NULL DEFAULT 1")
                conn.execute("UPDATE shop_settings SET courier_rate_return = 1500 WHERE id = 1")
            if "courier_cash_lift_fix" not in shop_settings_columns:
                # 28.10.2026: прежние доставки — «Собрано за заказы» без подъёма; вернуть подъём в наличные.
                conn.execute("ALTER TABLE shop_settings ADD COLUMN courier_cash_lift_fix INTEGER NOT NULL DEFAULT 1")
                for ev in conn.execute("SELECT id, cash, data FROM courier_events WHERE kind = 'delivered' AND data IS NOT NULL").fetchall():
                    try:
                        d = json.loads(ev["data"])
                    except (TypeError, ValueError):
                        continue
                    if d.get("payment") == "cash_on_delivery" and int(d.get("lift") or 0) > 0:
                        conn.execute("UPDATE courier_events SET cash = ? WHERE id = ?", (int(d.get("total") or 0), ev["id"]))
            if "outside_km_default" not in shop_settings_columns:
                # Цена владельца 25.10.2026: 500 ₽ + 50 ₽ за км — один раз; дальше меняется в админке.
                conn.execute("ALTER TABLE shop_settings ADD COLUMN outside_km_default INTEGER NOT NULL DEFAULT 1")
                conn.execute("UPDATE shop_settings SET outside_km_price = 50 WHERE outside_km_price = 0")
            conn.execute("INSERT OR IGNORE INTO shop_settings (id) VALUES (1)")
            conn.execute(
                "INSERT OR IGNORE INTO app_theme (id, theme_json, updated_at) VALUES (1, ?, ?)",
                (json.dumps(DEFAULT_THEME, ensure_ascii=False), self.now()),
            )
            # Разово включаем премиум-кнопки («Премиум», button_fill = "lux") у уже
            # сохранённой темы. Метка lux_buttons_applied не даёт повторить это,
            # если админ потом сам выберет другую заливку на экране «Разработчик».
            theme_row = conn.execute("SELECT theme_json FROM app_theme WHERE id = 1").fetchone()
            try:
                saved_theme = json.loads(theme_row["theme_json"]) if theme_row else None
            except (json.JSONDecodeError, TypeError):
                saved_theme = None
            if isinstance(saved_theme, dict) and not saved_theme.get("lux_buttons_applied"):
                saved_theme["button_fill"] = "lux"
                saved_theme["lux_buttons_applied"] = True
                conn.execute(
                    "UPDATE app_theme SET theme_json = ? WHERE id = 1",
                    (json.dumps(saved_theme, ensure_ascii=False),),
                )
            order_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()
            }
            if "courier_id" not in order_columns:
                conn.execute(
                    """
                    ALTER TABLE orders
                    ADD COLUMN courier_id INTEGER REFERENCES couriers(id) ON DELETE SET NULL
                    """
                )
            if "points_spent" not in order_columns:
                # Сколько партнёрских баллов списано при оформлении (1 балл = 1 ₽, уже вычтено из total).
                conn.execute("ALTER TABLE orders ADD COLUMN points_spent INTEGER NOT NULL DEFAULT 0")
            if "assign_ack" not in order_columns:
                # Админ назначил заказ курьеру (0) — пока курьер не нажал «Понятно» в окне
                # «Вам назначили заказ». 1 — подтверждено / курьер взял сам.
                conn.execute("ALTER TABLE orders ADD COLUMN assign_ack INTEGER NOT NULL DEFAULT 1")
            if "kind" not in order_columns:
                # 'order' — обычный заказ; 'return' — возврат баллона: курьер забирает пустые
                # баллоны и отдаёт покупателю return_payout ₽ (25.10.2026).
                conn.execute("ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'order'")
                conn.execute("ALTER TABLE orders ADD COLUMN return_payout INTEGER NOT NULL DEFAULT 0")
            if "return_mode" not in order_columns:
                # Возврат: 'cash' — курьер отдаёт return_payout ₽; 'points' — клиенту начисляется return_points баллов.
                conn.execute("ALTER TABLE orders ADD COLUMN return_mode TEXT NOT NULL DEFAULT 'cash'")
                conn.execute("ALTER TABLE orders ADD COLUMN return_points INTEGER NOT NULL DEFAULT 0")
            if "descent_count" not in order_columns:
                # «⬇️ Спуск пустого баллона» в возврате (28.10.2026): сколько баллонов спустить и цена
                # за 1 спуск (= ставка курьеру). Сумма вычитается из выплаты покупателю, идёт курьеру в заработок.
                conn.execute("ALTER TABLE orders ADD COLUMN descent_count INTEGER NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE orders ADD COLUMN descent_price INTEGER NOT NULL DEFAULT 0")
            if "delivered_at" not in order_columns:
                # Когда заказ стал «Доставлен» — для учёта баллонов и денег курьера за смену.
                # Старые доставленные заказы получают время последнего изменения.
                conn.execute("ALTER TABLE orders ADD COLUMN delivered_at TEXT")
                conn.execute("UPDATE orders SET delivered_at = updated_at WHERE status = 'delivered'")
            if "courier_taken_at" not in order_columns:
                # Когда курьер взял заказ (или админ назначил) — для учёта баллонов за смену.
                conn.execute("ALTER TABLE orders ADD COLUMN courier_taken_at TEXT")
            if "region" not in order_columns:
                # Регион курьеров (center/north/east/south/west), NULL — не определился.
                conn.execute("ALTER TABLE orders ADD COLUMN region TEXT")
            if "delivery_fee" not in order_columns:
                # Стоимость доставки за пределы Москвы — назначает админ у индивидуального
                # заказа; уже входит в total (к оплате курьеру).
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_fee INTEGER NOT NULL DEFAULT 0")
            if "individual" not in order_columns:
                # Индивидуальный заказ (доставка не в Москву): 1 — ждёт администратора,
                # курьерам не показывается; 2 — админ передал курьерам / назначил курьера.
                conn.execute("ALTER TABLE orders ADD COLUMN individual INTEGER NOT NULL DEFAULT 0")
            if "bonus_spent" not in order_columns:
                # Сколько бонусов покупатель списал при оформлении (уже вычтено из total).
                conn.execute("ALTER TABLE orders ADD COLUMN bonus_spent INTEGER NOT NULL DEFAULT 0")
            if "location_lat" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN location_lat REAL")
            if "location_lon" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN location_lon REAL")
            # Комментарий покупателя к заказу и «доставка ко времени» (UTC ISO), 25.10.2026.
            if "customer_comment" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN customer_comment TEXT")
            if "deliver_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN deliver_at TEXT")
            # Чат курьера и покупателя: снимок участников (для Excel) и метка «до сброса истории».
            om_columns = {r["name"] for r in conn.execute("PRAGMA table_info(order_messages)").fetchall()}
            for column, definition in (("customer_id", "INTEGER"), ("customer_name", "TEXT"),
                                       ("courier_name", "TEXT"), ("archived", "INTEGER NOT NULL DEFAULT 0")):
                if om_columns and column not in om_columns:
                    conn.execute(f"ALTER TABLE order_messages ADD COLUMN {column} {definition}")
            # Километры по дорогам за границей Москвы (автоматический расчёт доставки).
            if "delivery_km" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_km INTEGER")
            # Платная дорога: сколько из delivery_fee — доплата админа (уже входит в delivery_fee и total).
            if "delivery_toll" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_toll INTEGER NOT NULL DEFAULT 0")
            # 30.10.2026: предоплата за доставку за пределы Москвы — покупатель отдаёт её курьеру сам;
            # в total не входит. prepay_taken_at — курьер нажал «Взял предоплату».
            if "prepay_amount" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN prepay_amount INTEGER NOT NULL DEFAULT 0")
            if "prepay_taken_at" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN prepay_taken_at TEXT")
            # 28.10.2026: цена за км на момент расчёта доставки — км × цена идёт курьеру в заработок.
            if "delivery_km_price" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN delivery_km_price INTEGER")
            # 28.10.2026: «keep» — сколько курьер оставляет себе из наличных (заработок + платная дорога
            # по наличным заказам и возвратам); «Сдать в кассу» = собрано − отдано за пустые − keep.
            ev_columns = {r[1] for r in conn.execute("PRAGMA table_info(courier_events)")}
            if ev_columns and "keep" not in ev_columns:
                conn.execute("ALTER TABLE courier_events ADD COLUMN keep INTEGER NOT NULL DEFAULT 0")
            # Кеш маршрутов: точка дома (4 знака) → км за Москвой; не тратить лимит сервиса.
            conn.execute("""CREATE TABLE IF NOT EXISTS route_cache (
                                key TEXT PRIMARY KEY, km REAL NOT NULL, created_at TEXT NOT NULL)""")
            if "courier_last_lat" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN courier_last_lat REAL")
            if "courier_last_lon" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN courier_last_lon REAL")
            if "payment_proof_file_id" not in order_columns:
                # Telegram file_id of the screenshot a customer sends after
                # paying by crypto/SBP QR code, for the admin to verify
                # manually — nothing here confirms the payment automatically.
                conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_file_id TEXT")
            if "usdt_amount" not in order_columns:
                # Crypto orders: the USDT amount and the RUB-per-USDT rate
                # fixed at the moment the customer confirmed the order.
                conn.execute("ALTER TABLE orders ADD COLUMN usdt_amount REAL")
            if "usdt_rate" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN usdt_rate REAL")
            if "rating_prompted" not in order_columns:
                # 1 — приложение уже один раз напомнило покупателю оценить
                # курьера по этому заказу; больше не напоминаем.
                conn.execute("ALTER TABLE orders ADD COLUMN rating_prompted INTEGER NOT NULL DEFAULT 0")
            for column, definition in (
                # 1 — СБП/крипта оплачиваются при курьере (новая схема, без
                # предоплаты и скриншота): заказ сразу доступен курьерам.
                ("pay_on_delivery", "INTEGER NOT NULL DEFAULT 0"),
                # Суммы во всех нужных криптовалютах на момент оформления:
                # JSON {"USDT": {"amount": "68.43", "rate": 95.0}, ...}
                ("crypto_quotes", "TEXT"),
                # Курьер подтвердил оплату фото экрана покупателя.
                ("payment_confirmed_at", "TEXT"),
                ("payment_confirm_file_id", "TEXT"),
                # QR (монета + сеть / СБП), который покупатель выбрал при
                # оформлении; курьер показывает именно его.
                ("payment_qr_id", "INTEGER"),
                # QR, по которому оплату реально приняли (курьер мог «❗ Изменить валюту»).
                ("paid_qr_id", "INTEGER"),
                # Когда курьер нажал «📵 Покупатель не выходит на связь» (статус disputed).
                ("disputed_at", "TEXT"),
            ):
                if column not in order_columns:
                    conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
            if "rating_required" not in order_columns:
                # 1 — заказ закрыт курьером (или админом) после 17.10.2026:
                # покупатель обязан оценить курьера, приложение не пускает
                # дальше без оценки. Старые заказы остаются 0 — их не требуем.
                conn.execute("ALTER TABLE orders ADD COLUMN rating_required INTEGER NOT NULL DEFAULT 0")
            if "courier_arrived_at" not in order_columns:
                # Когда курьер нажал «Я на месте» (последний раз).
                conn.execute("ALTER TABLE orders ADD COLUMN courier_arrived_at TEXT")
            if "floor_lift_floors" not in order_columns:
                # Подъём на этаж: 1 — нужен, 0 — «заберу сам» (до 17.10.2026
                # здесь хранилось число этажей), и цена услуги на момент
                # заказа (смена цены админом не меняет старые заказы).
                # Стоимость = floor_lift_floors × floor_lift_price, уже в orders.total.
                conn.execute(
                    "ALTER TABLE orders ADD COLUMN floor_lift_floors INTEGER NOT NULL DEFAULT 0"
                )
            if "floor_lift_price" not in order_columns:
                conn.execute(
                    "ALTER TABLE orders ADD COLUMN floor_lift_price INTEGER NOT NULL DEFAULT 0"
                )
            user_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            for column, definition in (
                ("admin_rating", "INTEGER CHECK (admin_rating BETWEEN 1 AND 5)"),
                ("admin_note", "TEXT"),
                ("last_customer_name", "TEXT"),
                ("last_phone", "TEXT"),
                ("last_address_city", "TEXT"),
                ("last_address_street", "TEXT"),
                ("last_address_house", "TEXT"),
                ("last_address_building", "TEXT"),
                ("last_address_apartment", "TEXT"),
                ("last_address", "TEXT"),
                # Чёрный список: 1 — покупатель не может оформлять заказы.
                ("is_blocked", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_reason", "TEXT"),
                ("blocked_at", "TEXT"),
                # Последний выбранный вход в приложение: admin | courier | customer.
                ("app_mode", "TEXT"),
                # Пришёл по ссылке партнёра (Telegram ID партнёра) — навсегда.
                ("referred_by", "INTEGER"),
                ("referred_at", "TEXT"),
                # Индивидуальный процент бонусов (NULL — по умолчанию, BONUS_DEFAULT_PERCENT).
                ("bonus_percent", "REAL"),
                # Последние адреса доставки (JSON, до 3) — выбор в одно касание при оформлении.
                ("saved_addresses", "TEXT"),
            ):
                if column not in user_columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
            courier_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(couriers)").fetchall()
            }
            for column, definition in (
                ("username", "TEXT"),
                ("rating_sum", "INTEGER NOT NULL DEFAULT 0"),
                ("rating_count", "INTEGER NOT NULL DEFAULT 0"),
                # Смена курьера: с какого момента и какие регионы выбрал (JSON).
                ("shift_started_at", "TEXT"),
                ("shift_regions", "TEXT"),
                # Смена региона посреди смены: NULL / 'requested' / 'allowed'.
                ("region_change", "TEXT"),
                ("region_change_at", "TEXT"),
                # Баллоны с собой на начало смены: {"2": n, "5": n, "10": n} (JSON).
                ("shift_stock", "TEXT"),
                # «Заехать на склад»: фактические баллоны после загрузки (JSON) и когда.
                ("shift_reload", "TEXT"),
                ("shift_reload_at", "TEXT"),
                # Загрузка на складе ждёт подтверждения администратора (JSON) и когда отправлена.
                ("reload_pending", "TEXT"),
                ("reload_pending_at", "TEXT"),
                # Статус смены (23.10.2026, вместо 12 часов): 'on' / 'break' / 'off';
                # когда курьер последний раз ответил «Да, работаем!»; когда и почему закончил
                # ('self' — «Нет, закончил работу», 'auto' — не ответил, 'break' — перерыв).
                ("shift_state", "TEXT"),
                ("shift_confirmed_at", "TEXT"),
                ("shift_ended_at", "TEXT"),
                ("shift_end_reason", "TEXT"),
                # Цвет метки курьера на карте админа (0–14, свой у каждого).
                ("marker_idx", "INTEGER"),
                # Последняя точка курьера (трансляция в боте или приложение) и когда пришла.
                ("live_lat", "REAL"),
                ("live_lon", "REAL"),
                ("live_at", "TEXT"),
                # Трансляция геопозиции в чате с ботом: какое сообщение, до какого времени, остановлена ли.
                ("live_src_chat", "INTEGER"),
                ("live_src_msg", "INTEGER"),
                ("live_until", "TEXT"),
                ("live_stopped", "INTEGER NOT NULL DEFAULT 0"),
                # «В путь!» ждёт администратора (26.10.2026): {"regions", "stock", "at"}; отказ — когда.
                ("shift_request", "TEXT"),
                ("shift_request_declined_at", "TEXT"),
                # Журнал смен (25.10.2026): открытая смена курьера (courier_shifts.id).
                ("current_shift_id", "INTEGER"),
                # 28.10.2026: остаток полных товаров после закрытой смены (с ним начинается следующая);
                # «Завершить смену» ждёт администратора (когда нажал); админ видел перерыв («ОК»);
                # пустые баллоны, которые курьер обещал сдать на склад (JSON, на момент заявки склада).
                ("stock_left", "TEXT"),
                ("shift_end_requested_at", "TEXT"),
                ("break_seen_at", "TEXT"),
                ("reload_empties", "TEXT"),
                # 28.10.2026: курьер сам деньги не обнуляет — запрос {"names": [...], "at": ...} ждёт админа.
                ("counter_reset_request", "TEXT"),
            ):
                if column not in courier_columns:
                    conn.execute(f"ALTER TABLE couriers ADD COLUMN {column} {definition}")
            # 28.10.2026: работа по регионам убрана — запросы смены региона и регионы смен больше не нужны.
            conn.execute("UPDATE couriers SET region_change = NULL, region_change_at = NULL WHERE region_change IS NOT NULL")
            if "marker_idx" not in courier_columns:
                for i, row in enumerate(conn.execute("SELECT id FROM couriers ORDER BY id").fetchall()):
                    conn.execute("UPDATE couriers SET marker_idx = ? WHERE id = ?", (i % self.MARKER_COLORS, row["id"]))
            if "shift_state" not in courier_columns:
                # Переход с «12 часов»: кто сейчас на смене — остаётся на смене, вопрос через час.
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE couriers SET shift_state = 'on', shift_confirmed_at = ? "
                    "WHERE shift_started_at IS NOT NULL AND shift_started_at > ?",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), cutoff))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_courier_id ON orders(courier_id)")
            # Оплата в архиве сброшенной истории — для колонок «Оплата»/крипта в Excel.
            # Заявки на реферал первой версии (одна на человека, без вида) — в
            # новую таблицу с видом заявки; прежние заявки считаем «лояльностью».
            ref_columns = {r["name"] for r in conn.execute("PRAGMA table_info(referral_requests)").fetchall()}
            if "kind" not in ref_columns:
                conn.executescript(
                    """
                    BEGIN;
                    ALTER TABLE referral_requests RENAME TO referral_requests_v1;
                    CREATE TABLE referral_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL DEFAULT 'loyalty' CHECK (kind IN ('partner', 'loyalty')),
                        created_at TEXT NOT NULL,
                        UNIQUE (user_id, kind)
                    );
                    INSERT INTO referral_requests (user_id, kind, created_at)
                        SELECT user_id, 'loyalty', created_at FROM referral_requests_v1 ORDER BY id;
                    DROP TABLE referral_requests_v1;
                    COMMIT;
                    """
                )
            # Решение админа по заявке: pending — ждёт; declined_temp — отклонена
            # до decline_until (потом можно подать снова); declined_forever — навсегда.
            ref_columns = {r["name"] for r in conn.execute("PRAGMA table_info(referral_requests)").fetchall()}
            for column, definition in (
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("declined_at", "TEXT"),
                ("decline_until", "TEXT"),
                # Одобренная заявка (status = 'approved'): уникальный код ссылки
                # и сколько баллов начислять за каждый подходящий заказ.
                ("partner_code", "TEXT"),
                # Версия ссылки, о которой партнёр подтвердил «разослал» (сравнивается с shop_settings).
                ("link_ack_version", "INTEGER NOT NULL DEFAULT 0"),
                ("reward_points", "INTEGER"),
                ("approved_at", "TEXT"),
                # Баллы партнёру за КАЖДЫЙ баллон в доставленном заказе
                # приглашённого клиента: отдельно за новый и за баллон на замену.
   …56109 tokens truncated…     status = r["status"]
            if status == "dismissed" or (status == "declined_temp" and (not r["decline_until"] or r["decline_until"] <= now)):
                status = "can_reapply"  # «просто отклонена» или 7 дней прошли — можно подать снова
            result[r["kind"]] = {"status": status, "created_at": r["created_at"], "decline_until": r["decline_until"]}
        return result

    def get_referral_requests(self) -> list[sqlite3.Row]:
        """Заявки для админа: кто подал, курьер ли он (по Telegram ID), контакты."""
        with self.connection() as conn:
            return list(conn.execute(
                """
                SELECT r.id, r.user_id, r.kind, r.created_at, r.comment,
                       u.first_name, u.last_name, u.username, u.last_phone,
                       c.id AS courier_id, c.name AS courier_name, c.phone AS courier_phone,
                       c.is_active AS courier_active,
                       (SELECT COUNT(*) FROM orders o WHERE o.user_id = r.user_id AND o.status = 'delivered') AS purchases
                FROM referral_requests r
                JOIN users u ON u.telegram_id = r.user_id
                LEFT JOIN couriers c ON c.telegram_id = r.user_id
                WHERE r.status = 'pending'
                ORDER BY r.created_at DESC, r.id DESC
                """
            ).fetchall())

    def get_couriers(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        with self.connection() as conn:
            query = "SELECT * FROM couriers"
            if not include_inactive:
                query += " WHERE is_active = 1"
            return list(
                conn.execute(query + " ORDER BY is_active DESC, name COLLATE NOCASE, id").fetchall()
            )

    def get_courier(self, courier_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM couriers WHERE id = ?", (courier_id,)).fetchone()

    def add_courier(
        self,
        name: str,
        phone: str | None,
        telegram_id: int | None,
        username: str | None = None,
    ) -> int:
        with self.connection() as conn:
            used = [int(r[0]) for r in conn.execute("SELECT marker_idx FROM couriers WHERE marker_idx IS NOT NULL")]
            free = [i for i in range(self.MARKER_COLORS) if i not in used]
            # Свободный цвет; если все 15 заняты — тот, что встречается реже всего.
            idx = free[0] if free else min(range(self.MARKER_COLORS), key=lambda i: (used.count(i), i))
            cursor = conn.execute(
                """
                INSERT INTO couriers
                    (name, phone, telegram_id, username, is_active, created_at, marker_idx)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (name, phone, telegram_id, username, self.now(), idx),
            )
            return int(cursor.lastrowid)

    def get_users(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT telegram_id, username, first_name, last_name,
                           admin_rating, admin_note, is_blocked, blocked_reason, blocked_at, bonus_percent,
                           created_at, updated_at
                    FROM users
                    ORDER BY updated_at DESC, telegram_id
                    """
                ).fetchall()
            )

    def set_user_admin_rating(
        self, telegram_id: int, rating: int, note: str | None
    ) -> sqlite3.Row | None:
        if rating < 1 or rating > 5:
            raise ValueError("User rating must be between 1 and 5")
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET admin_rating = ?, admin_note = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (rating, note, self.now(), telegram_id),
            )
            return conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()

    def get_blocked_user_ids(self, user_ids: list[int]) -> set[int]:
        ids = sorted({int(u) for u in user_ids})
        found: set[int] = set()
        with self.connection() as conn:
            for chunk in self._chunks(ids):
                marks = ",".join("?" * len(chunk))
                found.update(int(r[0]) for r in conn.execute(
                    f"SELECT telegram_id FROM users WHERE is_blocked = 1 AND telegram_id IN ({marks})", chunk
                ))
        return found

    def set_user_app_mode(self, telegram_id: int, mode: str | None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE users SET app_mode = ? WHERE telegram_id = ?", (mode, telegram_id))

    def is_user_blocked(self, telegram_id: int) -> bool:
        """Покупатель в чёрном списке — заказы оформлять нельзя (приложение и бот)."""
        with self.connection() as conn:
            row = conn.execute("SELECT is_blocked FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return bool(row and row["is_blocked"])

    def set_user_blocked(self, telegram_id: int, blocked: bool, reason: str | None = None) -> sqlite3.Row | None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE users SET is_blocked = ?, blocked_reason = ?, blocked_at = ? WHERE telegram_id = ?",
                (1 if blocked else 0, reason if blocked else None, self.now() if blocked else None, telegram_id),
            )
            return conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()

    def get_active_courier_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM couriers
                WHERE telegram_id = ? AND is_active = 1
                """,
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def _order_search_condition(search: str | None) -> tuple[str | None, list[Any]]:
        """Поиск заказа: «23» — номер заказа (или Telegram ID / цифры телефона),
        «622221060_23» — ID покупателя + номер, иначе — имя, @логин или телефон."""
        import re as _re

        q = (search or "").strip()
        if not q:
            return None, []
        phone_col = ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(o.customer_phone, ' ', ''), '-', ''), '(', ''), ')', ''), '+', '')")
        m = _re.fullmatch(r"#?№?\s*(\d+)_(\d+)", q)
        if m:
            return "(o.user_id = ? AND o.id = ?)", [int(m.group(1)), int(m.group(2))]
        m = _re.fullmatch(r"[#№]?\s*(\d+)", q)
        if m:
            n = m.group(1)
            return f"(o.id = ? OR o.user_id = ? OR {phone_col} LIKE ?)", [int(n), int(n), f"%{n}%"]
        digits = _re.sub(r"\D", "", q)
        text = q.lstrip("@").lower()
        parts = ["ulower(o.customer_name) LIKE ?", "ulower(COALESCE(u.username, '')) LIKE ?",
                 "ulower(COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')) LIKE ?"]
        values: list[Any] = [f"%{text}%"] * 3
        if len(digits) >= 4:
            parts.append(f"{phone_col} LIKE ?")
            values.append(f"%{digits}%")
            if digits[0] in "78" and len(digits) >= 5:  # «+7 900…» и «8 900…» — один и тот же номер
                parts.append(f"{phone_col} LIKE ?")
                values.append(f"%{'8' if digits[0] == '7' else '7'}{digits[1:]}%")
        return "(" + " OR ".join(parts) + ")", values

    def get_orders_history(
        self,
        statuses: tuple[str, ...] | None = None,
        courier_id: int | None = None,
        courier_telegram_id: int | None = None,
        limit: int = 30,
        offset: int = 0,
        search: str | None = None,
    ) -> tuple[list[sqlite3.Row], int, int]:
        """История заказов с фильтрами: статусы, курьер (по id в таблице
        couriers или по его Telegram ID). Возвращает (страница заказов,
        сколько всего подходит под фильтр, сумма доставленных из них)."""
        where, args = [], []
        if statuses:
            where.append(f"o.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if courier_id is not None:
            where.append("o.courier_id = ?")
            args.append(courier_id)
        if courier_telegram_id is not None:
            where.append("c.telegram_id = ?")
            args.append(courier_telegram_id)
        cond_search, search_args = self._order_search_condition(search)
        if cond_search:
            where.append(cond_search)
            args.extend(search_args)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        base = """FROM orders o JOIN users u ON u.telegram_id = o.user_id
                  LEFT JOIN couriers c ON c.id = o.courier_id """ + cond
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT o.*, u.telegram_id, u.username,
                           c.name AS courier_name, c.phone AS courier_phone,
                           c.telegram_id AS courier_telegram_id,
                           c.username AS courier_username,
                           c.rating_sum AS courier_rating_sum,
                           c.rating_count AS courier_rating_count
                    {base} ORDER BY o.id DESC LIMIT ? OFFSET ?""",
                (*args, int(limit), int(offset)),
            ).fetchall()
            agg = conn.execute(
                f"""SELECT COUNT(*) AS cnt,
                           COALESCE(SUM(CASE WHEN o.status = 'delivered' THEN o.total END), 0) AS delivered_sum
                    {base}""",
                args,
            ).fetchone()
        return list(rows), int(agg["cnt"]), int(agg["delivered_sum"])

    def get_courier_completed_count(self, courier_telegram_id: int) -> int:
        """How many orders this courier has delivered (status='delivered')."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM orders o
                JOIN couriers c ON c.id = o.courier_id
                WHERE c.telegram_id = ? AND o.status = 'delivered'
                """,
                (courier_telegram_id,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_admin_open_orders(self) -> list[sqlite3.Row]:
        """Every order that is not finished yet (new, accepted, in transit),
        including unpaid prepaid ones — the admin needs to see and manage
        all of them, not only the ones still waiting for a courier."""
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT o.*, u.telegram_id, u.username,
                           c.name AS courier_name, c.phone AS courier_phone,
                           c.telegram_id AS courier_telegram_id,
                           c.username AS courier_username,
                           c.rating_sum AS courier_rating_sum,
                           c.rating_count AS courier_rating_count
                    FROM orders o JOIN users u ON u.telegram_id = o.user_id
                    LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.status IN ('new', 'accepted', 'in_transit', 'not_delivered')
                    ORDER BY o.id DESC LIMIT 50
                    """
                ).fetchall()
            )

    def get_disputed_orders(self) -> list[sqlite3.Row]:
        """«⚠️ Спорные заказы»: курьер нажал «📵 Покупатель не выходит на связь»."""
        with self.connection() as conn:
            return list(conn.execute(
                """
                SELECT o.*, u.telegram_id, u.username,
                       c.name AS courier_name, c.phone AS courier_phone,
                       c.telegram_id AS courier_telegram_id,
                       c.username AS courier_username,
                       c.rating_sum AS courier_rating_sum,
                       c.rating_count AS courier_rating_count
                FROM orders o JOIN users u ON u.telegram_id = o.user_id
                LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'disputed'
                ORDER BY o.disputed_at DESC, o.id DESC
                """
            ).fetchall())

    def courier_has_open_orders(self, courier_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE courier_id = ? AND status IN ('accepted', 'in_transit') LIMIT 1",
                (courier_id,),
            ).fetchone()
            return row is not None

    def get_courier_active_orders_full(self, courier_telegram_id: int) -> list[sqlite3.Row]:
        """Как get_courier_active_orders, но с теми же полями, что get_order — одним запросом
        (раньше экран «В работе» делал по запросу на каждый заказ)."""
        with self.connection() as conn:
            return list(conn.execute(
                """
                SELECT o.*, u.telegram_id, u.username,
                       c.name AS courier_name, c.phone AS courier_phone,
                       c.telegram_id AS courier_telegram_id,
                       c.username AS courier_username,
                       c.rating_sum AS courier_rating_sum,
                       c.rating_count AS courier_rating_count
                FROM orders o JOIN users u ON u.telegram_id = o.user_id
                JOIN couriers c ON c.id = o.courier_id
                WHERE c.telegram_id = ? AND o.status IN ('accepted', 'in_transit')
                ORDER BY o.id
                """,
                (courier_telegram_id,),
            ).fetchall())

    def get_courier_active_orders(self, courier_telegram_id: int) -> list[sqlite3.Row]:
        """Orders currently assigned to this courier and still in progress
        (accepted or in transit) — used to figure out which order(s) a
        courier's live-location share belongs to."""
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT o.* FROM orders o
                    JOIN couriers c ON c.id = o.courier_id
                    WHERE c.telegram_id = ? AND o.status IN ('accepted', 'in_transit')
                    """,
                    (courier_telegram_id,),
                ).fetchall()
            )

    def set_order_extras(self, order_id: int, comment: str | None, deliver_at: str | None) -> None:
        """Комментарий покупателя и время «доставка ко времени» (видят курьер и админ)."""
        with self.connection() as conn:
            conn.execute("UPDATE orders SET customer_comment = ?, deliver_at = ? WHERE id = ?",
                         (comment, deliver_at, order_id))

    def set_order_courier_location(self, order_id: int, lat: float, lon: float) -> None:
        """Remember the courier's latest known position for this order, so
        the customer's "📍 Где курьер" button can send a fresh static pin
        on demand without needing a live broadcast still in flight."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET courier_last_lat = ?, courier_last_lon = ? WHERE id = ?",
                (lat, lon, order_id),
            )

    def create_location_broadcast(
        self,
        order_id: int,
        courier_source_chat_id: int,
        courier_source_message_id: int,
        customer_chat_id: int,
        customer_message_id: int,
    ) -> None:
        """Record that a courier's live-location message (identified by its
        own chat_id+message_id) is being mirrored into a live-location
        message the bot sent to one specific order's customer."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO courier_location_broadcasts
                    (order_id, courier_source_chat_id, courier_source_message_id,
                     customer_chat_id, customer_message_id, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    order_id,
                    courier_source_chat_id,
                    courier_source_message_id,
                    customer_chat_id,
                    customer_message_id,
                    self.now(),
                ),
            )

    def get_location_broadcasts_by_source(
        self, courier_source_chat_id: int, courier_source_message_id: int
    ) -> list[sqlite3.Row]:
        """Active broadcasts mirroring a given courier live-location message —
        there can be more than one if the courier has several active orders
        and shared one live location for all of them."""
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM courier_location_broadcasts
                    WHERE courier_source_chat_id = ? AND courier_source_message_id = ?
                      AND is_active = 1
                    """,
                    (courier_source_chat_id, courier_source_message_id),
                ).fetchall()
            )

    def deactivate_location_broadcasts_by_order(self, order_id: int) -> list[sqlite3.Row]:
        """Mark every active broadcast for this order inactive and return the
        rows that were active, so the caller can stop each live-location
        message on Telegram's side too."""
        with self.connection() as conn:
            rows = list(
                conn.execute(
                    "SELECT * FROM courier_location_broadcasts WHERE order_id = ? AND is_active = 1",
                    (order_id,),
                ).fetchall()
            )
            if rows:
                conn.execute(
                    "UPDATE courier_location_broadcasts SET is_active = 0 WHERE order_id = ?",
                    (order_id,),
                )
            return rows

    def deactivate_location_broadcasts_by_source(
        self, courier_source_chat_id: int, courier_source_message_id: int
    ) -> list[sqlite3.Row]:
        """Like deactivate_location_broadcasts_by_order, but by the courier's
        own live-location message — used when the courier manually stops
        sharing before the order is marked delivered/cancelled."""
        with self.connection() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM courier_location_broadcasts
                    WHERE courier_source_chat_id = ? AND courier_source_message_id = ?
                      AND is_active = 1
                    """,
                    (courier_source_chat_id, courier_source_message_id),
                ).fetchall()
            )
            if rows:
                conn.execute(
                    """
                    UPDATE courier_location_broadcasts
                    SET is_active = 0
                    WHERE courier_source_chat_id = ? AND courier_source_message_id = ?
                    """,
                    (courier_source_chat_id, courier_source_message_id),
                )
            return rows

    def set_courier_active(self, courier_id: int, is_active: bool) -> sqlite3.Row | None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE couriers SET is_active = ? WHERE id = ?",
                (int(is_active), courier_id),
            )
            return conn.execute("SELECT * FROM couriers WHERE id = ?", (courier_id,)).fetchone()

    def delete_courier(self, courier_id: int) -> bool:
        """Permanently delete a courier, along with their rating history
        (courier_ratings.courier_id is ON DELETE RESTRICT, so it would
        otherwise block this). Past orders are left alone — orders.courier_id
        is ON DELETE SET NULL, so the order stays in history, just with an
        empty "Курьер" field. courier_offers rows cascade automatically.

        Returns True if a courier was deleted, False if courier_id didn't exist.
        Raises CourierBusyError if the courier still has an order in work —
        deleting them then would leave that order with no courier and no
        way for anyone to finish it.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                busy = conn.execute(
                    "SELECT 1 FROM orders WHERE courier_id = ? "
                    "AND status IN ('accepted', 'in_transit') LIMIT 1",
                    (courier_id,),
                ).fetchone()
                if busy:
                    conn.execute("ROLLBACK")
                    raise CourierBusyError()
                conn.execute(
                    "DELETE FROM courier_ratings WHERE courier_id = ?", (courier_id,)
                )
                cursor = conn.execute("DELETE FROM couriers WHERE id = ?", (courier_id,))
                conn.execute("COMMIT")
                return cursor.rowcount > 0
            except CourierBusyError:
                raise
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def assign_courier(self, order_id: int, courier_id: int) -> sqlite3.Row | None:
        """Manually assign an active courier to a freshly placed (new) order.

        This is the admin override path: it works on orders that no courier
        has claimed yet. On success the order moves straight to 'accepted',
        the same state a courier reaches by tapping "Принять заказ".
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order = conn.execute(
                    "SELECT status, courier_id FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                courier = conn.execute(
                    "SELECT id FROM couriers WHERE id = ? AND is_active = 1", (courier_id,)
                ).fetchone()
                if (
                    not order
                    or order["status"] != "new"
                    or order["courier_id"] is not None
                    or not courier
                ):
                    conn.execute("ROLLBACK")
                    return None

                updated = conn.execute(
                    """
                    UPDATE orders
                    SET courier_id = ?, status = 'accepted', updated_at = ?, courier_taken_at = ?, assign_ack = 0
                    WHERE id = ? AND status = 'new' AND courier_id IS NULL
                    """,
                    (courier_id, self.now(), self.now(), order_id),
                )
                if updated.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    """UPDATE orders SET
                         courier_last_lat = (SELECT live_lat FROM couriers WHERE id = orders.courier_id),
                         courier_last_lon = (SELECT live_lon FROM couriers WHERE id = orders.courier_id)
                       WHERE id = ? AND (SELECT live_lat FROM couriers WHERE id = orders.courier_id) IS NOT NULL""",
                    (order_id,))
                conn.execute(
                    """
                    UPDATE courier_offers
                    SET status = CASE
                        WHEN courier_id = ? THEN 'won' ELSE 'closed' END
                    WHERE order_id = ? AND status = 'open'
                    """,
                    (courier_id, order_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._journal_safe(lambda: self.courier_event(int(courier_id), "take", order_id,
                                                      data={**self._order_snapshot(order_id), "by": "admin"}))
        return self.get_order(order_id)

    def create_courier_offer(
        self,
        order_id: int,
        courier_id: int | None,
        chat_id: int,
        message_id: int,
        channel: str,
    ) -> None:
        if channel not in {"private", "group"}:
            raise ValueError("Unknown courier offer channel")
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO courier_offers
                    (order_id, courier_id, chat_id, message_id, channel, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (order_id, courier_id, chat_id, message_id, channel, self.now()),
            )

    def get_courier_offers(self, order_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT co.*, c.telegram_id AS courier_telegram_id
                    FROM courier_offers co
                    LEFT JOIN couriers c ON c.id = co.courier_id
                    WHERE co.order_id = ?
                    ORDER BY co.id
                    """,
                    (order_id,),
                ).fetchall()
            )

    def close_courier_offers(self, order_id: int, winner_courier_id: int | None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE courier_offers
                SET status = CASE
                    WHEN courier_id = ? THEN 'won' ELSE 'closed' END
                WHERE order_id = ? AND status = 'open'
                """,
                (winner_courier_id, order_id),
            )

    def confirm_delivery(self, order_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order = conn.execute(
                    """
                    SELECT * FROM orders
                    WHERE id = ? AND user_id = ?
                      AND status IN ('accepted', 'in_transit')
                      AND courier_id IS NOT NULL
                    """,
                    (order_id, user_id),
                ).fetchone()
                if not order:
                    conn.execute("ROLLBACK")
                    return None
                updated = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'delivered', updated_at = ?, delivered_at = ?
                    WHERE id = ? AND user_id = ?
                      AND status IN ('accepted', 'in_transit')
                      AND courier_id IS NOT NULL
                    """,
                    (self.now(), self.now(), order_id, user_id),
                )
                if updated.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._journal_safe(lambda: self._courier_order_event(order, "delivered"))
        return self.get_order(order_id)

    def rate_courier(self, order_id: int, user_id: int, rating: int) -> sqlite3.Row | None:
        if rating < 1 or rating > 5:
            raise ValueError("Courier rating must be between 1 and 5")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order = conn.execute(
                    """
                    SELECT * FROM orders
                    WHERE id = ? AND user_id = ? AND status = 'delivered'
                      AND courier_id IS NOT NULL
                    """,
                    (order_id, user_id),
                ).fetchone()
                if not order:
                    conn.execute("ROLLBACK")
                    return None
                existing = conn.execute(
                    "SELECT 1 FROM courier_ratings WHERE order_id = ?", (order_id,)
                ).fetchone()
                if existing:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    """
                    INSERT INTO courier_ratings
                        (order_id, courier_id, user_id, rating, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_id, int(order["courier_id"]), user_id, rating, self.now()),
                )
                conn.execute(
                    """
                    UPDATE couriers
                    SET rating_sum = rating_sum + ?, rating_count = rating_count + 1
                    WHERE id = ?
                    """,
                    (rating, int(order["courier_id"])),
                )
                conn.execute("COMMIT")
                return self.get_order(order_id)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _restore_order_inventory(self, conn: sqlite3.Connection, order_id: int) -> None:
        """No-op: stock/inventory tracking was removed, so cancelling an
        order no longer needs to give anything back. Kept as a call site
        (rather than deleted) so cancel_order/update_order_status don't
        need to change their transaction structure."""
        return

    def cancel_order(self, order_id: int, user_id: int | None = None) -> sqlite3.Row | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                query = "SELECT * FROM orders WHERE id = ?"
                params: list[Any] = [order_id]
                if user_id is not None:
                    query += " AND user_id = ?"
                    params.append(user_id)
                order = conn.execute(query, params).fetchone()
                if (
                    not order
                    or order["status"] != "new"
                    or order["inventory_released"]
                ):
                    conn.execute("ROLLBACK")
                    return None
                cancelled = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'cancelled', inventory_released = 1, updated_at = ?
                    WHERE id = ? AND status = 'new' AND inventory_released = 0
                    """,
                    (self.now(), order_id),
                )
                if cancelled.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                self._restore_order_inventory(conn, order_id)
                self._refund_bonus(conn, order)
                conn.execute("COMMIT")
                return self.get_order(order_id)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # Конечные статусы: заказ закрыт, менять нельзя. disputed_closed — «Спорный —
    # закрыт» (админ нажал «🗑 Удалить заказ» в «Спорных»): как отмена, но с пометкой.
    FINAL_STATUSES = ("cancelled", "delivered", "disputed_closed")

    def update_order_status(self, order_id: int, status: str) -> sqlite3.Row | None:
        allowed = {"new", "accepted", "in_transit", "delivered", "cancelled", "not_delivered",
                   "disputed", "disputed_closed"}
        if status not in allowed:
            raise ValueError("Unknown order status")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if not order or order["status"] in self.FINAL_STATUSES:
                    conn.execute("ROLLBACK")
                    return None
                if status == order["status"]:
                    conn.execute("ROLLBACK")
                    return self.get_order(order_id)

                if status in ("cancelled", "disputed_closed"):
                    # Отмена и «Спорный — закрыт»: товар возвращается на склад.
                    cancelled = conn.execute(
                        """
                        UPDATE orders
                        SET status = ?, inventory_released = 1, updated_at = ?
                        WHERE id = ? AND status NOT IN ('cancelled', 'delivered', 'disputed_closed')
                          AND inventory_released = 0
                        """,
                        (status, self.now(), order_id),
                    )
                    if cancelled.rowcount != 1:
                        conn.execute("ROLLBACK")
                        return None
                    self._restore_order_inventory(conn, order_id)
                    if status == "cancelled":
                        # Отмена — списанные бонусы возвращаются. «Спорный — закрыт»
                        # (покупатель не вышел к курьеру) — бонусы сгорают.
                        self._refund_bonus(conn, order)
                elif status == "new":
                    # Back to "waiting for a courier": the courier is taken
                    # off the order, otherwise it would sit in 'new' with a
                    # courier attached and nobody could ever take it again.
                    # Следы прошлого курьера (его точка на карте, «на месте»,
                    # спор) сбрасываются — у нового курьера всё с чистого листа.
                    conn.execute(
                        """
                        UPDATE orders
                        SET status = 'new', courier_id = NULL, courier_arrived_at = NULL,
                            courier_last_lat = NULL, courier_last_lon = NULL, disputed_at = NULL,
                            updated_at = ?
                        WHERE id = ? AND status NOT IN ('cancelled', 'delivered', 'disputed_closed')
                        """,
                        (self.now(), order_id),
                    )
                else:
                    if order["courier_id"] is None:
                        # "В пути"/"Доставлен" only make sense once a courier has the order.
                        conn.execute("ROLLBACK")
                        return None
                    conn.execute(
                        """
                        UPDATE orders
                        SET status = ?, updated_at = ?,
                            delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END,
                            rating_required = CASE WHEN ? = 'delivered' THEN 1 ELSE rating_required END,
                            disputed_at = CASE WHEN ? = 'disputed' THEN ? ELSE disputed_at END
                        WHERE id = ? AND status NOT IN ('cancelled', 'delivered', 'disputed_closed')
                        """,
                        (status, self.now(), status, self.now(), status, status, self.now(), order_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._journal_safe(lambda: self._courier_order_event(order, status))  # журнал смены и деньги курьера
        if status == "delivered":
            self.accrue_partner_points(order_id)  # баллы партнёру, если заказ подходит
            self.accrue_customer_bonus(order_id)  # бонусы покупателю, если заказ подходит
        return self.get_order(order_id)

    # ------------------------------------------------------------ бонусы покупателей
    BONUS_DEFAULT_PERCENT = 2.0
    BONUS_MIN_ITEMS_TOTAL = 6000  # бонусы — за заказ, где товаров на 6 000 ₽ и больше (без подъёма)

    @staticmethod
    def _bonus_balance(conn: sqlite3.Connection, user_id: int) -> int:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM customer_bonuses WHERE user_id = ?", (user_id,)
        ).fetchone()[0])

    def _refund_bonus(self, conn: sqlite3.Connection, order: sqlite3.Row) -> None:
        """Отмена заказа: списанные на него бонусы — обратно покупателю (один раз).
        Исключение — спорный заказ (курьер отметил «покупатель не выходит на
        связь»): бонусы сгорают как компенсация издержек."""
        spent = int(order["bonus_spent"] or 0)
        if order["status"] == "disputed" or order["disputed_at"]:
            return
        if spent > 0:
            conn.execute(
                """INSERT OR IGNORE INTO customer_bonuses (user_id, order_id, amount, kind, created_at)
                   VALUES (?, ?, ?, 'refund', ?)""",
                (int(order["user_id"]), int(order["id"]), spent, self.now()),
            )
        points = int(order["points_spent"] or 0) if "points_spent" in order.keys() else 0
        if points > 0:  # партнёрские баллы, списанные на заказ, — тоже обратно
            conn.execute(
                """INSERT OR IGNORE INTO partner_points (partner_id, order_id, points, kind, reason, created_at)
                   VALUES (?, ?, ?, 'refund', 'возврат: заказ отменён', ?)""",
                (int(order["user_id"]), int(order["id"]), points, self.now()),
            )

    def bonus_percent_for(self, user_id: int) -> float:
        with self.connection() as conn:
            row = conn.execute("SELECT bonus_percent FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        if row is None or row["bonus_percent"] is None:
            return self.BONUS_DEFAULT_PERCENT
        return float(row["bonus_percent"])

    @staticmethod
    def bonus_for(items_total: int, percent: float) -> int:
        """Бонусы за заказ: процент от суммы товаров по исходным ценам, вниз до целого."""
        from decimal import ROUND_FLOOR, Decimal

        value = Decimal(int(items_total)) * Decimal(str(percent)) / Decimal(100)
        return int(value.to_integral_value(rounding=ROUND_FLOOR))

    def accrue_customer_bonus(self, order_id: int) -> int:
        """Заказ доставлен: если товаров по исходным ценам (без подъёма) на
        6 000 ₽ и больше — покупателю начисляется его процент (по умолчанию 2 %)
        от суммы товаров ЗА ВЫЧЕТОМ списанных бонусов; если после вычета
        меньше 6 000 ₽ — от 6 000 ₽. Вниз до целого. Один раз на заказ."""
        with self.connection() as conn:
            o = conn.execute("SELECT id, user_id, status, kind, bonus_spent, points_spent FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not o or o["status"] != "delivered" or o["kind"] == "return":  # возврат баллона — не покупка
                return 0
            items_total = int(conn.execute(
                "SELECT COALESCE(SUM(quantity * unit_price), 0) FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchone()[0])
            if items_total < self.BONUS_MIN_ITEMS_TOTAL:
                return 0
            row = conn.execute("SELECT bonus_percent FROM users WHERE telegram_id = ?", (o["user_id"],)).fetchone()
            percent = self.BONUS_DEFAULT_PERCENT if row is None or row["bonus_percent"] is None else float(row["bonus_percent"])
            # Оплаченное бонусами и партнёрскими баллами — не деньги, в базу не входит.
            base = max(items_total - int(o["bonus_spent"] or 0) - int(o["points_spent"] or 0), self.BONUS_MIN_ITEMS_TOTAL)
            points = self.bonus_for(base, percent)
            if points <= 0:
                return 0
            cur = conn.execute(
                """INSERT OR IGNORE INTO customer_bonuses
                       (user_id, order_id, amount, kind, order_total, percent, created_at)
                   VALUES (?, ?, ?, 'accrual', ?, ?, ?)""",
                (int(o["user_id"]), order_id, points, base, percent, self.now()),
            )
            return points if cur.rowcount == 1 else 0

    def bonus_balance(self, user_id: int) -> int:
        with self.connection() as conn:
            return self._bonus_balance(conn, user_id)

    BONUS_TRANSFER_FEE_PERCENT = 10
    BONUS_TRANSFER_MIN = 10

    @classmethod
    def bonus_transfer_fee(cls, amount: int) -> int:
        """Комиссия 10 %, округление вверх: 15 → 2, 100 → 10."""
        return -(-int(amount) * cls.BONUS_TRANSFER_FEE_PERCENT // 100)

    def find_user_by_username(self, username: str) -> sqlite3.Row | None:
        name = (username or "").strip().lstrip("@").strip()
        if not name:
            return None
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE ltrim(username, '@') = ? COLLATE NOCASE ORDER BY updated_at DESC LIMIT 1",
                (name,),
            ).fetchone()

    def transfer_bonus(self, sender_id: int, recipient_id: int, amount: int) -> dict[str, int]:
        """Перевод бонусов другу: у отправителя −amount, другу amount − 10 % (вверх).
        Возвращает {"sent", "fee", "received", "balance"}. ValueError — мало бонусов/неверная сумма."""
        amount = int(amount)
        if amount < self.BONUS_TRANSFER_MIN or sender_id == recipient_id:
            raise ValueError("bad amount")
        fee = self.bonus_transfer_fee(amount)
        received = amount - fee
        now = self.now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                balance = self._bonus_balance(conn, sender_id)
                if amount > balance:
                    raise ValueError("not enough")
                conn.execute(
                    """INSERT INTO customer_bonuses (user_id, amount, kind, counterpart_id, fee, created_at)
                       VALUES (?, ?, 'transfer_out', ?, ?, ?)""", (sender_id, -amount, recipient_id, fee, now))
                conn.execute(
                    """INSERT INTO customer_bonuses (user_id, amount, kind, counterpart_id, fee, created_at)
                       VALUES (?, ?, 'transfer_in', ?, ?, ?)""", (recipient_id, received, sender_id, fee, now))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"sent": amount, "fee": fee, "received": received, "balance": balance - amount}

    def bonus_transfers(self) -> list[dict[str, Any]]:
        """Для выгрузки: все переводы (строки отправителя)."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customer_bonuses WHERE kind = 'transfer_out' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def bonus_balances(self) -> dict[int, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT user_id, SUM(amount) AS b FROM customer_bonuses GROUP BY user_id"
            ).fetchall()
        return {int(r["user_id"]): int(r["b"] or 0) for r in rows}

    def bonus_history(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT b.*, u.first_name AS cp_first_name, u.last_name AS cp_last_name, u.username AS cp_username
                   FROM customer_bonuses b LEFT JOIN users u ON u.telegram_id = b.counterpart_id
                   WHERE b.user_id = ? ORDER BY b.id DESC LIMIT ?""", (user_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_admin_action(self, admin_id: int, admin_name: str | None, action: str, *,
                         target_user_id: int | None = None, target_name: str | None = None,
                         amount: int | None = None, balance_after: int | None = None,
                         order_id: int | None = None, details: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO admin_actions (admin_id, admin_name, action, target_user_id, target_name, amount,
                                              balance_after, order_id, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(admin_id), admin_name, action, target_user_id, target_name, amount, balance_after,
                 order_id, details, self.now()),
            )

    def get_admin_actions(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_actions ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
            ).fetchall()
        return [dict(r) for r in rows]

    def adjust_customer_bonus(self, user_id: int, amount: int, reason: str | None) -> int:
        """Админ вручную: + начислить / − списать (не больше остатка). Возвращает новый остаток."""
        amount = int(amount)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                balance = self._bonus_balance(conn, user_id)
                if amount == 0 or balance + amount < 0:
                    raise ValueError("bad amount")
                conn.execute(
                    """INSERT INTO customer_bonuses (user_id, amount, kind, reason, created_at)
                       VALUES (?, ?, 'manual', ?, ?)""",
                    (user_id, amount, reason, self.now()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return balance + amount

    def set_user_bonus_percent(self, user_id: int, percent: float | None) -> bool:
        """Индивидуальный процент бонусов покупателя; None — вернуть «по умолчанию»."""
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE users SET bonus_percent = ? WHERE telegram_id = ?",
                (None if percent is None else float(percent), user_id),
            )
        return cur.rowcount == 1

    def order_extras(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Для «Истории заказов» админа: пришёл ли покупатель по ссылке партнёра
        (и сколько баллов партнёру дал заказ), сколько бонусов начислено,
        оценки сторон и комментарии курьера."""
        if not order_ids:
            return {}
        marks = ",".join("?" * len(order_ids))
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT o.id, u.referred_by,
                       p.first_name AS partner_first, p.last_name AS partner_last, p.username AS partner_username,
                       (SELECT points FROM partner_points WHERE order_id = o.id AND kind = 'accrual') AS partner_points,
                       (SELECT amount FROM customer_bonuses WHERE order_id = o.id AND kind = 'accrual') AS bonus_accrued,
                       (SELECT amount FROM customer_bonuses WHERE order_id = o.id AND kind = 'refund') AS bonus_refunded,
                       (SELECT rating FROM courier_ratings WHERE order_id = o.id) AS courier_rating,
                       (SELECT rating FROM customer_reviews WHERE order_id = o.id AND rating IS NOT NULL) AS customer_rating,
                       (SELECT group_concat(comment, ' | ') FROM customer_reviews
                         WHERE order_id = o.id AND comment IS NOT NULL) AS courier_comment
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id
                LEFT JOIN users p ON p.telegram_id = u.referred_by
                WHERE o.id IN ({marks})
                """,
                [int(i) for i in order_ids],
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for r in rows:
            partner = None
            if r["referred_by"]:
                name = " ".join(x for x in (r["partner_first"], r["partner_last"]) if x) or f"ID {r['referred_by']}"
                partner = {"id": int(r["referred_by"]), "name": name,
                           "username": (r["partner_username"] or "").lstrip("@") or None,
                           "points": int(r["partner_points"] or 0)}
            result[int(r["id"])] = {
                "referral": partner,
                "bonus_accrued": int(r["bonus_accrued"] or 0),
                "bonus_refunded": int(r["bonus_refunded"] or 0),
                "courier_rating": r["courier_rating"],
                "customer_rating": r["customer_rating"],
                "courier_comment": r["courier_comment"],
            }
        return result

    SAVED_ADDRESSES = 3

    def set_prepaid(self, order_id: int) -> None:
        """Покупатель согласился на предоплату: доставка в total не входит (он платит её курьеру сам).
        delivery_fee остаётся полной (км × цена идёт курьеру в заработок)."""
        with self.connection() as conn:
            conn.execute(
                """UPDATE orders SET prepay_amount = delivery_fee - delivery_toll,
                       total = total - (delivery_fee - delivery_toll), updated_at = ?
                   WHERE id = ? AND individual = 2 AND status = 'new' AND prepay_amount = 0 AND delivery_fee > delivery_toll""",
                (self.now(), order_id))

    def take_prepay(self, order_id: int, courier_id: int) -> sqlite3.Row | None:
        """Курьер нажал «Взял предоплату»: сумма идёт ему в «Наличные за товары» (сдать в кассу
        вместе с остальными наличными; своё за км он оставляет при доставке). Один раз на заказ."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if (not o or int(o["prepay_amount"] or 0) <= 0 or o["prepay_taken_at"]
                        or o["courier_id"] is None or int(o["courier_id"]) != int(courier_id)
                        or o["status"] not in ("accepted", "in_transit")):
                    conn.execute("ROLLBACK")
                    return None
                conn.execute("UPDATE orders SET prepay_taken_at = ?, updated_at = ? WHERE id = ?",
                             (self.now(), self.now(), order_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.courier_event(int(courier_id), "prepaid", order_id, cash=int(o["prepay_amount"]),
                           data={"prepay": int(o["prepay_amount"]), "address": o["delivery_address"]})
        return self.get_order(order_id)

    def set_delivery_fee(self, order_id: int, fee: int) -> sqlite3.Row | None:
        """Стоимость доставки индивидуального заказа: прежняя убирается из total, новая добавляется.
        Только пока заказ не закрыт и курьер не взял его в работу."""
        fee = max(0, int(fee))
        with self.connection() as conn:
            cur = conn.execute(
                """UPDATE orders SET total = total - delivery_fee + ? + delivery_toll,
                       delivery_fee = ? + delivery_toll, updated_at = ?
                   WHERE id = ? AND individual > 0 AND status IN ('new', 'accepted') AND prepay_amount = 0""",
                (fee, fee, self.now(), order_id),
            )
        return self.get_order(order_id) if cur.rowcount == 1 else None

    def set_delivery_toll(self, order_id: int, toll: int) -> sqlite3.Row | None:
        """Платная дорога: доплата к доставке за пределы Москвы (входит в delivery_fee и total).
        Пока заказ не доставлен и не отменён."""
        toll = max(0, int(toll))
        with self.connection() as conn:
            cur = conn.execute(
                """UPDATE orders SET total = total - delivery_toll + ?, delivery_fee = delivery_fee - delivery_toll + ?,
                       delivery_toll = ?, updated_at = ?
                   WHERE id = ? AND individual > 0 AND status IN ('new', 'accepted', 'in_transit')""",
                (toll, toll, toll, self.now(), order_id),
            )
        return self.get_order(order_id) if cur.rowcount == 1 else None

    def release_individual(self, order_id: int) -> None:
        """Админ договорился с покупателем: индивидуальный заказ можно отдавать курьерам."""
        with self.connection() as conn:
            conn.execute("UPDATE orders SET individual = 2 WHERE id = ? AND individual = 1", (order_id,))

    def saved_addresses(self, user_id: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute("SELECT saved_addresses FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        try:
            items = json.loads(row["saved_addresses"]) if row and row["saved_addresses"] else []
        except (TypeError, ValueError):
            items = []
        return [i for i in items if isinstance(i, dict) and i.get("line")][: self.SAVED_ADDRESSES]

    def remember_address(self, user_id: int, parts: dict[str, Any]) -> None:
        """Адрес последнего заказа — первым в списке; одинаковые не повторяются."""
        keys = ("line", "house", "building", "entrance", "apartment", "floor", "intercom")
        same = lambda a: tuple(str(a.get(k) or "").strip().lower() for k in keys)  # noqa: E731
        items = [parts] + [a for a in self.saved_addresses(user_id) if same(a) != same(parts)]
        with self.connection() as conn:
            conn.execute(
                "UPDATE users SET saved_addresses = ? WHERE telegram_id = ?",
                (json.dumps(items[: self.SAVED_ADDRESSES], ensure_ascii=False), user_id),
            )

    def forget_address(self, user_id: int, index: int) -> None:
        items = self.saved_addresses(user_id)
        if 0 <= index < len(items):
            del items[index]
            with self.connection() as conn:
                conn.execute("UPDATE users SET saved_addresses = ? WHERE telegram_id = ?",
                             (json.dumps(items, ensure_ascii=False), user_id))

    def customer_phones(self) -> dict[int, list[str]]:
        """Все телефоны покупателя: последний сохранённый и из его заказов (для поиска в админке)."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT telegram_id AS uid, last_phone AS phone FROM users WHERE last_phone IS NOT NULL
                   UNION SELECT user_id, customer_phone FROM orders WHERE customer_phone IS NOT NULL"""
            ).fetchall()
        result: dict[int, list[str]] = {}
        for r in rows:
            if r["phone"] and r["phone"] not in result.setdefault(int(r["uid"]), []):
                result[int(r["uid"])].append(r["phone"])
        return result

    def get_shop_texts(self) -> dict[str, str | None]:
        with self.connection() as conn:
            row = conn.execute("SELECT home_greeting FROM shop_settings WHERE id = 1").fetchone()
        return {"home_greeting": (row["home_greeting"] or "").strip() or None if row else None}

    def set_home_greeting(self, text: str | None) -> str | None:
        """Текст под «Привет, …!» на главном экране магазина; пусто — стандартный."""
        text = (text or "").strip() or None
        with self.connection() as conn:
            conn.execute("UPDATE shop_settings SET home_greeting = ? WHERE id = 1", (text,))
        return text

    def bonus_totals(self) -> dict[int, dict[str, int]]:
        """По покупателям: всего начислено (за заказы и вручную) и всего потрачено на заказы."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT user_id,
                          COALESCE(SUM(CASE WHEN amount > 0 AND kind IN ('accrual', 'manual') THEN amount ELSE 0 END), 0) AS earned,
                          COALESCE(-SUM(CASE WHEN kind = 'spend' THEN amount ELSE 0 END), 0)
                            - COALESCE(SUM(CASE WHEN kind = 'refund' THEN amount ELSE 0 END), 0) AS spent,
                          COALESCE(-SUM(CASE WHEN kind = 'transfer_out' THEN amount ELSE 0 END), 0) AS gifted,
                          COALESCE(SUM(CASE WHEN kind = 'transfer_in' THEN amount ELSE 0 END), 0) AS gifts
                   FROM customer_bonuses GROUP BY user_id"""
            ).fetchall()
        return {int(r["user_id"]): {"earned": int(r["earned"]), "spent": int(r["spent"]),
                                    "gifted": int(r["gifted"]), "gifts": int(r["gifts"])} for r in rows}

    def bonus_by_orders(self) -> dict[int, dict[str, int]]:
        """Для выгрузки: по текущим заказам — начислено и возвращено бонусов."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT order_id, kind, amount FROM customer_bonuses WHERE order_id IS NOT NULL AND kind IN ('accrual', 'refund')"
            ).fetchall()
        result: dict[int, dict[str, int]] = {}
        for r in rows:
            result.setdefault(int(r["order_id"]), {})[r["kind"]] = int(r["amount"])
        return result


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None
