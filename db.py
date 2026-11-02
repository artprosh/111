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
                ("reward_new", "INTEGER"),
                ("reward_exchange", "INTEGER"),
                # Комментарий к заявке (обязателен с 25.10.2026) — по нему админ отсекает случайных людей.
                ("comment", "TEXT"),
            ):
                if column not in ref_columns:
                    conn.execute(f"ALTER TABLE referral_requests ADD COLUMN {column} {definition}")
            # Партнёры, одобренные по старым условиям («N баллов за заказ»):
            # их ставка становится ставкой за каждый баллон — и новый, и на замену.
            conn.execute(
                """UPDATE referral_requests SET reward_new = COALESCE(reward_points, 0),
                       reward_exchange = COALESCE(reward_points, 0)
                   WHERE status = 'approved' AND reward_new IS NULL AND reward_exchange IS NULL"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_partner_code ON referral_requests (partner_code) "
                "WHERE partner_code IS NOT NULL"
            )
            # Одно начисление на заказ (после сброса истории номера заказов
            # начинаются заново — старые начисления отвязываются от номера).
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_points_order ON partner_points (order_id) "
                "WHERE kind = 'accrual' AND order_id IS NOT NULL"
            )
            # Оплата баллами и её возврат — не больше одного раза на заказ.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_points_spend ON partner_points (order_id, kind) "
                "WHERE kind IN ('spend', 'refund') AND order_id IS NOT NULL"
            )
            archive_columns = {r["name"] for r in conn.execute("PRAGMA table_info(sales_archive)").fetchall()}
            for column, definition in (
                ("payment_method", "TEXT"),
                ("crypto_currency", "TEXT"),
                ("crypto_network", "TEXT"),
                ("crypto_rate", "REAL"),
                # Кто, куда, кто вёз и оценки — чтобы полная выгрузка не теряла
                # клиентов и курьеров из сброшенной истории (с 20.10.2026).
                ("user_id", "INTEGER"),
                ("customer_name", "TEXT"),
                ("customer_phone", "TEXT"),
                ("customer_username", "TEXT"),
                ("delivery_address", "TEXT"),
                ("courier_id", "INTEGER"),
                ("courier_name", "TEXT"),
                ("courier_rating", "INTEGER"),
                ("customer_rating", "INTEGER"),
                # Для детальной выгрузки (с 20.10.2026): этажи подъёма, когда
                # заказ закрыт (доставлен / отменён).
                ("floor_lift_floors", "INTEGER"),
                ("order_closed_at", "TEXT"),
                # Бонусы, начисленные покупателю за этот заказ (одно число на все строки заказа).
                ("bonus_accrued", "INTEGER"),
            ):
                if column not in archive_columns:
                    conn.execute(f"ALTER TABLE sales_archive ADD COLUMN {column} {definition}")
            # Частые выборки: заказы покупателя, заказы по статусу, товары заказа.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)")

            # --- migration: products.price_exchange (price for a cylinder exchange,
            # as opposed to price = price for buying a brand-new cylinder) ---
            # Переводы бонусов другу (23.10.2026): у старой таблицы CHECK без
            # transfer_out/transfer_in — SQLite не меняет CHECK, поэтому пересобираем таблицу.
            bonus_sql = (conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'customer_bonuses'").fetchone() or [""])[0]
            if bonus_sql and "transfer_out" not in bonus_sql:
                conn.executescript("""
                    BEGIN;
                    CREATE TABLE customer_bonuses_new (
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
                    INSERT INTO customer_bonuses_new (id, user_id, order_id, amount, kind, reason, order_total, percent, created_at)
                        SELECT id, user_id, order_id, amount, kind, reason, order_total, percent, created_at FROM customer_bonuses;
                    DROP TABLE customer_bonuses;
                    ALTER TABLE customer_bonuses_new RENAME TO customer_bonuses;
                    CREATE INDEX IF NOT EXISTS idx_customer_bonuses_user ON customer_bonuses (user_id, id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_bonuses_order
                        ON customer_bonuses (order_id, kind)
                        WHERE order_id IS NOT NULL AND kind IN ('accrual', 'spend', 'refund');
                    COMMIT;
                """)
            # Оплата товаров партнёрскими баллами (23.10.2026): у старой таблицы CHECK без
            # spend/refund — пересобираем (SQLite не меняет CHECK).
            pp_sql = (conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'partner_points'").fetchone() or [""])[0]
            if pp_sql and "'spend'" not in pp_sql:
                conn.executescript("""
                    BEGIN;
                    CREATE TABLE partner_points_new (
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
                    INSERT INTO partner_points_new (id, partner_id, order_id, referee_id, points, kind, reason, order_total, created_at)
                        SELECT id, partner_id, order_id, referee_id, points, kind, reason, order_total, created_at FROM partner_points;
                    DROP TABLE partner_points;
                    ALTER TABLE partner_points_new RENAME TO partner_points;
                    CREATE INDEX IF NOT EXISTS idx_partner_points_partner ON partner_points (partner_id, id);
                    COMMIT;
                """)
            product_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()
            }
            if "price_exchange" not in product_columns:
                conn.execute("ALTER TABLE products ADD COLUMN price_exchange INTEGER")
                conn.execute(
                    "UPDATE products SET price_exchange = price WHERE price_exchange IS NULL"
                )
            if "bonus_text" not in product_columns:
                conn.execute("ALTER TABLE products ADD COLUMN bonus_text TEXT")
            if "bonus_desc" not in product_columns:
                # 28.10.2026: один бонус на товар — название (bonus_text), описание и к каким
                # вариантам применяется («Новый» / «Замена»). По умолчанию к обоим, как было.
                conn.execute("ALTER TABLE products ADD COLUMN bonus_desc TEXT")
            if "bonus_new" not in product_columns:
                conn.execute("ALTER TABLE products ADD COLUMN bonus_new INTEGER NOT NULL DEFAULT 1")
            if "bonus_exchange" not in product_columns:
                conn.execute("ALTER TABLE products ADD COLUMN bonus_exchange INTEGER NOT NULL DEFAULT 1")
            if "bonus_product_id" not in product_columns:
                # 28.10.2026: бонус — это товар каталога (подарок), bonus_qty штук на каждую штуку товара.
                # Подарок кладётся в заказ строкой за 0 ₽ и списывается с остатка курьера.
                # Старые текстовые бонусы без товара больше не действуют (владелец: «только товар»).
                conn.execute("ALTER TABLE products ADD COLUMN bonus_product_id INTEGER")
            if "bonus_qty" not in product_columns:
                conn.execute("ALTER TABLE products ADD COLUMN bonus_qty INTEGER NOT NULL DEFAULT 1")
            if "return_price" not in product_columns:
                # «Возврат баллона» (25.10.2026): сколько курьер платит покупателю за пустой
                # такой баллон, ₽. 0 — этот баллон на возврат не принимается.
                conn.execute("ALTER TABLE products ADD COLUMN return_price INTEGER NOT NULL DEFAULT 0")
            if "return_points" not in product_columns:
                # 30.10.2026: сколько баллов (бонусов покупателя) получает клиент за такой пустой баллон,
                # если выбрал возврат баллами вместо наличных. 0 — баллами не принимается.
                conn.execute("ALTER TABLE products ADD COLUMN return_points INTEGER NOT NULL DEFAULT 0")
            if "partner_reward" not in product_columns:
                # Баллы партнёру за 1 такой баллон; NULL — по умолчанию по объёму (2 л — 100, 5 л — 150, 10 л — 200).
                conn.execute("ALTER TABLE products ADD COLUMN partner_reward INTEGER")
            if "sort_order" not in product_columns:
                # Порядок товаров в каталоге (26.10.2026): админ перетаскивает товары во вкладке
                # «📦 Товары». Начальный порядок — как было (категория, номер).
                conn.execute("ALTER TABLE products ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
                for i, row in enumerate(conn.execute("SELECT id FROM products ORDER BY category, id").fetchall()):
                    conn.execute("UPDATE products SET sort_order = ? WHERE id = ?", (i + 1, row["id"]))
            if "webapp_photo_data_url" not in product_columns:
                # Mini App-only product photo, stored inline as a data: URL
                # (uploaded through the admin's "Разработчик"/product-editor
                # screen). Deliberately a SEPARATE column from photo_file_id:
                # photo_file_id is a Telegram file reference the classic bot
                # passes straight to bot.send_photo, which only accepts a
                # real file_id or an http(s) URL — not a data: URI — so
                # reusing that column here would break photo sending in the
                # classic chat flow. The Mini App's catalog renders this
                # column directly as an <img src>; the bot ignores it.
                conn.execute("ALTER TABLE products ADD COLUMN webapp_photo_data_url TEXT")

            # --- migration: order_items.variant ('new' purchase vs 'exchange') ---
            order_item_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(order_items)").fetchall()
            }
            if "variant" not in order_item_columns:
                conn.execute(
                    "ALTER TABLE order_items ADD COLUMN variant TEXT NOT NULL DEFAULT 'new'"
                )
            if "bonus_text" not in order_item_columns:
                conn.execute("ALTER TABLE order_items ADD COLUMN bonus_text TEXT")

            # --- migration: order_items.product_id used to RESTRICT deleting a
            # product that had ever been ordered (to protect order history).
            # The name/price/quantity are already copied into order_items at
            # order time, so that protection wasn't actually needed — it just
            # blocked permanent deletion. Switch it to SET NULL so a product
            # can always be purged; past orders keep showing their saved
            # name and price either way. ---
            order_item_fks = conn.execute("PRAGMA foreign_key_list(order_items)").fetchall()
            needs_fk_migration = any(
                fk["table"] == "products" and fk["from"] == "product_id" and fk["on_delete"] != "SET NULL"
                for fk in order_item_fks
            )
            if needs_fk_migration:
                conn.execute(
                    """
                    CREATE TABLE order_items_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                        product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                        product_name TEXT NOT NULL,
                        unit_price INTEGER NOT NULL,
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        variant TEXT NOT NULL DEFAULT 'new' CHECK (variant IN ('new', 'exchange')),
                        bonus_text TEXT
                    )
                    """
                )
                # By this point bonus_text always exists on the live order_items
                # table — either it was already there, or the ALTER TABLE a few
                # lines up just added it (all-NULL for existing rows either way).
                conn.execute(
                    """
                    INSERT INTO order_items_new
                        (id, order_id, product_id, product_name, unit_price, quantity, variant, bonus_text)
                    SELECT id, order_id, product_id, product_name, unit_price, quantity, variant, bonus_text
                    FROM order_items
                    """
                )
                conn.execute("DROP TABLE order_items")
                conn.execute("ALTER TABLE order_items_new RENAME TO order_items")

            # --- 28.10.2026: строка-подарок (бонус-товар за 0 ₽) ---
            if "is_gift" not in {r["name"] for r in conn.execute("PRAGMA table_info(order_items)").fetchall()}:
                conn.execute("ALTER TABLE order_items ADD COLUMN is_gift INTEGER NOT NULL DEFAULT 0")

            # --- 01.11.2026: «возврат пустых баллонов в обычном заказе» (покупка + возврат в одном заказе) ---
            conn.execute("""CREATE TABLE IF NOT EXISTS order_returns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_id INTEGER,
                    product_name TEXT NOT NULL,
                    unit_price INTEGER NOT NULL,
                    quantity INTEGER NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_returns_order ON order_returns(order_id)")

            # --- migration: carts.variant — needs a new primary key, so an
            # already-deployed carts table (created before this feature) has
            # to be rebuilt rather than just getting a new column. ---
            cart_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(carts)").fetchall()
            }
            if "variant" not in cart_columns:
                conn.execute(
                    """
                    CREATE TABLE carts_new (
                        user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
                        variant TEXT NOT NULL DEFAULT 'new' CHECK (variant IN ('new', 'exchange')),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, product_id, variant)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO carts_new (user_id, product_id, variant, quantity, updated_at)
                    SELECT user_id, product_id, 'new', quantity, updated_at FROM carts
                    """
                )
                conn.execute("DROP TABLE carts")
                conn.execute("ALTER TABLE carts_new RENAME TO carts")

            # No demo products are seeded here, on purpose: the catalog
            # starts empty on every install (first run or otherwise) and
            # only ever contains what the admin added through the bot.

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def upsert_user(
        self, telegram_id: int, username: str | None, first_name: str, last_name: str | None
    ) -> None:
        now = self.now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, username, first_name or "Покупатель", last_name, now, now),
            )

    def get_user(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()

    def save_last_delivery_info(
        self,
        telegram_id: int,
        customer_name: str,
        phone: str,
        address: str | None,
    ) -> None:
        """Remember a customer's name/phone/address so the next order can
        offer to reuse it instead of asking again.

        Pass None for address when the order was placed via a shared
        location pin instead of a typed address — that leaves any
        previously saved typed address untouched rather than overwriting
        it with a blank.
        """
        with self.connection() as conn:
            if address is None:
                conn.execute(
                    """
                    UPDATE users
                    SET last_customer_name = ?, last_phone = ?, updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (customer_name, phone, self.now(), telegram_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET last_customer_name = ?, last_phone = ?, last_address = ?, updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (customer_name, phone, address, self.now(), telegram_id),
                )

    def get_catalog_photo(self) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT catalog_photo_file_id FROM shop_settings WHERE id = 1"
            ).fetchone()
            return row["catalog_photo_file_id"] if row else None

    def set_catalog_photo(self, file_id: str | None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE shop_settings SET catalog_photo_file_id = ? WHERE id = 1", (file_id,)
            )

    def get_payment_qr(self, method: str) -> str | None:
        """method is 'crypto' or 'sbp'. Returns the Telegram file_id of the
        admin-uploaded QR code photo for that payment method, or None if
        the admin hasn't set one up yet (in which case the corresponding
        payment button is hidden from customers)."""
        column = "crypto_qr_file_id" if method == "crypto" else "sbp_qr_file_id"
        with self.connection() as conn:
            row = conn.execute(f"SELECT {column} FROM shop_settings WHERE id = 1").fetchone()
            return row[column] if row else None

    def set_payment_qr(self, method: str, file_id: str | None) -> None:
        column = "crypto_qr_file_id" if method == "crypto" else "sbp_qr_file_id"
        with self.connection() as conn:
            conn.execute(f"UPDATE shop_settings SET {column} = ? WHERE id = 1", (file_id,))

    # ------------------------------------------------------------ «Я на месте»
    def mark_courier_arrived(self, order_id: int, courier_telegram_id: int) -> sqlite3.Row | None:
        """Курьер нажал «Я на месте». Только для заказа этого курьера в работе."""
        with self.connection() as conn:
            cur = conn.execute(
                """
                UPDATE orders SET courier_arrived_at = ?
                WHERE id = ? AND status IN ('accepted', 'in_transit')
                  AND courier_id IN (SELECT id FROM couriers WHERE telegram_id = ?)
                """,
                (self.now(), order_id, courier_telegram_id),
            )
        return self.get_order(order_id) if cur.rowcount == 1 else None

    # ------------------------------------------------------------ отзывы о покупателях
    def add_customer_review(
        self,
        order_id: int | None,
        user_id: int,
        courier_id: int | None,
        courier_name: str | None,
        rating: int | None = None,
        comment: str | None = None,
    ) -> bool:
        """Оценка и/или комментарий курьера о покупателе. False — оценка по
        этому заказу уже есть (повторно не ставится)."""
        if rating is not None and not 1 <= int(rating) <= 5:
            raise ValueError("Customer rating must be between 1 and 5")
        if rating is None and not (comment or "").strip():
            return False
        with self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO customer_reviews
                        (order_id, user_id, courier_id, courier_name, rating, comment, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, user_id, courier_id, courier_name,
                     int(rating) if rating is not None else None,
                     (comment or "").strip() or None, self.now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def customer_rated_by_courier(self, order_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM customer_reviews WHERE order_id = ? AND rating IS NOT NULL", (order_id,)
            ).fetchone()
        return row is not None

    def get_customer_reviews(self, user_id: int, limit: int = 3) -> dict[str, Any]:
        """Сводка для админа и курьеров: средняя оценка и последние комментарии
        (по умолчанию 3). Все отзывы хранятся — см. get_all_customer_reviews."""
        with self.connection() as conn:
            agg = conn.execute(
                "SELECT COUNT(rating) AS cnt, AVG(rating) AS avg, COUNT(*) AS total FROM customer_reviews WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            notes = conn.execute(
                """
                SELECT order_id, courier_name, rating, comment, created_at
                FROM customer_reviews
                WHERE user_id = ? AND comment IS NOT NULL
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, int(limit)),
            ).fetchall()
        return {
            "user_id": int(user_id),
            "rating": round(float(agg["avg"]), 1) if agg["cnt"] else None,
            "rating_count": int(agg["cnt"] or 0),
            "total": int(agg["total"] or 0),
            "notes": [dict(n) for n in notes],
        }

    @staticmethod
    def _chunks(values: list[int], size: int = 500) -> Iterator[list[int]]:
        for i in range(0, len(values), size):
            yield values[i:i + size]

    def get_customer_reviews_bulk(self, user_ids: list[int], limit: int = 3) -> dict[int, dict[str, Any]]:
        """То же, что get_customer_reviews, но сразу для многих покупателей —
        два запроса вместо двух на каждого (списки заказов, «Покупатели»)."""
        ids = sorted({int(u) for u in user_ids})
        result = {
            uid: {"user_id": uid, "rating": None, "rating_count": 0, "total": 0, "notes": []} for uid in ids
        }
        if not ids:
            return result
        with self.connection() as conn:
            for chunk in self._chunks(ids):
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT user_id, COUNT(rating) AS cnt, AVG(rating) AS avg, COUNT(*) AS total
                        FROM customer_reviews WHERE user_id IN ({marks}) GROUP BY user_id""",
                    chunk,
                ):
                    r = result[int(row["user_id"])]
                    r["rating"] = round(float(row["avg"]), 1) if row["cnt"] else None
                    r["rating_count"] = int(row["cnt"] or 0)
                    r["total"] = int(row["total"] or 0)
                if limit > 0:
                    for row in conn.execute(
                        f"""SELECT user_id, order_id, courier_name, rating, comment, created_at FROM (
                                SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) AS rn
                                FROM customer_reviews
                                WHERE user_id IN ({marks}) AND comment IS NOT NULL
                            ) WHERE rn <= ? ORDER BY user_id, rn""",
                        [*chunk, int(limit)],
                    ):
                        note = dict(row)
                        uid = int(note.pop("user_id"))
                        result[uid]["notes"].append(note)
        return result

    def get_order_items_bulk(self, order_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
        ids = sorted({int(o) for o in order_ids})
        result: dict[int, list[sqlite3.Row]] = {oid: [] for oid in ids}
        if not ids:
            return result
        with self.connection() as conn:
            for chunk in self._chunks(ids):
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY order_id, id", chunk
                ):
                    result[int(row["order_id"])].append(row)
        return result

    def get_payment_qr_codes_by_ids(self, code_ids: list[int]) -> dict[int, sqlite3.Row]:
        """QR по id, включая убранные админом (для подписи в заказах)."""
        ids = sorted({int(c) for c in code_ids})
        if not ids:
            return {}
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM payment_qr_codes WHERE id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
        return {int(r["id"]): r for r in rows}

    def get_all_customer_reviews(self, user_id: int) -> list[dict[str, Any]]:
        """ВСЕ отзывы всех курьеров о покупателе (оценки и комментарии), новые сверху."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT order_id, courier_name, rating, comment, created_at
                FROM customer_reviews WHERE user_id = ? ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_courier_rating_for_order(self, order_id: int) -> int | None:
        with self.connection() as conn:
            row = conn.execute("SELECT rating FROM courier_ratings WHERE order_id = ?", (order_id,)).fetchone()
        return int(row["rating"]) if row else None

    def get_pending_courier_rating(self, user_id: int) -> sqlite3.Row | None:
        """Самый ранний доставленный заказ, по которому покупатель ОБЯЗАН
        оценить курьера (rating_required = 1) и ещё не оценил. Пока он есть,
        приложение показывает окно оценки, которое нельзя закрыть."""
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT o.id, c.name AS courier_name FROM orders o
                JOIN couriers c ON c.id = o.courier_id
                LEFT JOIN courier_ratings r ON r.order_id = o.id
                WHERE o.user_id = ? AND o.status = 'delivered' AND r.id IS NULL
                  AND o.rating_required = 1
                ORDER BY o.id LIMIT 1
                """,
                (user_id,),
            ).fetchone()

    def mark_rating_prompted(self, user_id: int) -> None:
        """Напоминание показано — больше не напоминаем ни по одному из уже
        доставленных заказов этого покупателя (иначе при каждом запуске
        всплывал бы следующий неоценённый заказ)."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET rating_prompted = 1 WHERE user_id = ? AND status = 'delivered'",
                (user_id,),
            )

    # ------------------------------------------------------------ выгрузка в Excel
    @staticmethod
    def _payment_info(order: Any, qr_by_id: dict[int, Any]) -> dict[str, Any]:
        """Чем оплачен заказ: способ, монета и сеть (если крипта), курс к рублю
        на момент оформления. Монета — по QR, через который оплату реально
        приняли (paid_qr_id), иначе — выбранный покупателем (payment_qr_id);
        старые заказы с предоплатой в USDT — по usdt_rate."""
        keys = order.keys()
        method = order["payment_method"] if "payment_method" in keys else None
        info = {"payment_method": method, "crypto_currency": None, "crypto_network": None, "crypto_rate": None}
        if method != "crypto":
            return info
        qr = None
        for col in ("paid_qr_id", "payment_qr_id"):
            if col in keys and order[col]:
                qr = qr_by_id.get(int(order[col]))
                if qr is not None:
                    break
        quotes: dict[str, Any] = {}
        if "crypto_quotes" in keys and order["crypto_quotes"]:
            try:
                quotes = json.loads(order["crypto_quotes"]) or {}
            except (TypeError, ValueError):
                quotes = {}
        if qr is not None and qr["currency"]:
            info["crypto_currency"], info["crypto_network"] = qr["currency"], qr["network"]
            rate = (quotes.get(qr["currency"]) or {}).get("rate")
            info["crypto_rate"] = float(rate) if rate else None
        elif "usdt_rate" in keys and order["usdt_rate"]:
            info["crypto_currency"], info["crypto_rate"] = "USDT", float(order["usdt_rate"])
        return info

    def _qr_map(self, conn: sqlite3.Connection) -> dict[int, Any]:
        return {int(r["id"]): r for r in conn.execute("SELECT id, currency, network FROM payment_qr_codes")}

    def get_sales_lines(self, start: str | None, end: str | None,
                        period: tuple[str | None, str | None] | None = None) -> dict[str, Any]:
        """Строки продаж для выгрузки: товары и услуги доставленных заказов,
        размещённых в [start, end) (ISO-время UTC, None — без границы), из
        текущих заказов и из архива сброшенной истории. Плюс сбросы истории в
        этом интервале и число ещё не завершённых заказов (они не попадают в
        выгрузку, пока не доставлены)."""
        cond, args = [], []
        if start:
            cond.append("{t} >= ?")
            args.append(start)
        if end:
            cond.append("{t} < ?")
            args.append(end)

        def where(col: str) -> str:
            return " AND ".join(c.format(t=col) for c in cond) or "1"

        who = (
            "o.user_id, o.customer_name, o.customer_phone, u.username AS customer_username, o.delivery_address, "
            "c.name AS courier_name, o.floor_lift_floors, o.updated_at AS order_closed_at, "
            "COALESCE(o.delivered_at, o.updated_at) AS delivered_at"
        )
        with self.connection() as conn:
            current = conn.execute(
                f"""
                SELECT o.id AS order_no, o.created_at,
                       i.product_name || CASE WHEN i.is_gift = 1 THEN ' (подарок)' WHEN i.variant = 'exchange' THEN ' (замена)' ELSE '' END AS item_name,
                       i.quantity, i.quantity * i.unit_price AS line_total, i.id AS sort_key,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'item' AS line_kind, i.product_name AS product, i.variant, i.unit_price, {who}
                FROM order_items i JOIN orders o ON o.id = i.order_id
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.kind != 'return' AND {where('o.created_at')}
                UNION ALL
                SELECT o.id, o.created_at, 'Подъём на этаж (услуга)', 1,
                       o.floor_lift_floors * o.floor_lift_price, 1000000000,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'lift', 'Подъём на этаж', NULL, o.floor_lift_price, {who}
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.floor_lift_floors > 0 AND {where('o.created_at')}
                UNION ALL
                SELECT o.id, o.created_at, 'Спуск пустого баллона (услуга)', 1,
                       o.descent_count * o.descent_price, 1000000000,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'descent', 'Спуск пустого баллона', NULL, o.descent_price, {who}
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.kind != 'return' AND o.descent_count > 0 AND {where('o.created_at')}
                UNION ALL
                SELECT o.id, o.created_at, 'Доставка за пределы Москвы', 1, o.delivery_fee, 1000000000,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'delivery', 'Доставка за пределы Москвы', NULL, o.delivery_fee, {who}
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.delivery_fee > 0 AND {where('o.created_at')}
                UNION ALL
                SELECT o.id, o.created_at, 'Оплата бонусами', 1, -o.bonus_spent, 1000000001,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'bonus', 'Оплата бонусами', NULL, NULL, {who}
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.bonus_spent > 0 AND {where('o.created_at')}
                UNION ALL
                SELECT o.id, o.created_at, 'Оплата баллами', 1, -o.points_spent, 1000000002,
                       o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id,
                       'points', 'Оплата баллами', NULL, NULL, {who}
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.status = 'delivered' AND o.points_spent > 0 AND {where('o.created_at')}
                """,
                args * 6,
            ).fetchall()
            archived = conn.execute(
                f"""
                SELECT a.order_no, a.order_created_at AS created_at, a.item_name, a.quantity,
                       a.line_total, a.id AS sort_key, w.wiped_at,
                       a.payment_method, a.crypto_currency, a.crypto_network, a.crypto_rate,
                       a.user_id, a.customer_name, a.customer_phone, a.customer_username, a.delivery_address,
                       a.courier_name, a.floor_lift_floors, a.order_closed_at
                FROM sales_archive a JOIN history_wipes w ON w.id = a.wipe_id
                WHERE a.order_status = 'delivered' AND {where('a.order_created_at')}
                """,
                args,
            ).fetchall()
            wipes = conn.execute(
                f"SELECT wiped_at, orders_deleted FROM history_wipes WHERE {where('wiped_at')} ORDER BY wiped_at",
                args,
            ).fetchall()
            unfinished = conn.execute(
                f"""SELECT COUNT(*) FROM orders o
                    WHERE status IN ('new', 'accepted', 'in_transit', 'not_delivered', 'disputed') AND {where('o.created_at')}""",
                args,
            ).fetchone()[0]
            first = conn.execute(
                """SELECT MIN(t) FROM (SELECT MIN(created_at) AS t FROM orders
                                       UNION ALL SELECT MIN(order_created_at) FROM sales_archive)"""
            ).fetchone()[0]
            qr_by_id = self._qr_map(conn)
        lines = []
        for r in current:
            line = {k: r[k] for k in (
                "order_no", "created_at", "item_name", "quantity", "line_total", "sort_key", "line_kind", "product",
                "variant", "unit_price", "user_id", "customer_name", "customer_phone", "customer_username",
                "delivery_address", "courier_name", "floor_lift_floors", "order_closed_at", "delivered_at",
            )}
            line.update(self._payment_info(r, qr_by_id), archived=False, wiped_at=None)
            lines.append(line)
        for r in archived:
            line = dict(r, archived=True)
            line["delivered_at"] = line.get("order_closed_at") or line["created_at"]
            # В архиве товар и вариант — одной строкой «Название (замена)».
            name = line["item_name"]
            if name.startswith("Оплата бонусами"):
                line.update(line_kind="bonus", product="Оплата бонусами", variant=None, unit_price=None)
            elif name.startswith("Оплата баллами"):
                line.update(line_kind="points", product="Оплата баллами", variant=None, unit_price=None)
            elif name.startswith("Доставка"):
                line.update(line_kind="delivery", product="Доставка за пределы Москвы", variant=None,
                            unit_price=int(line["line_total"]))
            elif name.startswith("Спуск"):
                line.update(line_kind="descent", product="Спуск пустого баллона", variant=None,
                            unit_price=int(line["line_total"]))
            elif name.startswith("Подъём"):
                floors = int(line.get("floor_lift_floors") or 0)
                line.update(line_kind="lift", product="Подъём на этаж", variant=None,
                            unit_price=int(line["line_total"]) // floors if floors else None)
            else:
                exchange = name.endswith(" (замена)")
                qty = int(line["quantity"] or 0)
                line.update(
                    line_kind="item", product=name[: -len(" (замена)")] if exchange else name,
                    variant="exchange" if exchange else "new",
                    unit_price=int(line["line_total"]) // qty if qty else None,
                )
            lines.append(line)
        lines.sort(key=lambda r: (r["created_at"], r["order_no"], r["sort_key"]))
        wipes = [dict(w) for w in wipes]
        if period is not None:
            # Таблица №2 (28.10.2026): продажа — в периоде, в котором заказ доставлен.
            lines = [x for x in lines if self._in_period(x.get("delivered_at") or x["created_at"], period)]
            wipes = [w for w in wipes if self._in_period(w["wiped_at"], period)]
        return {
            "lines": lines,
            "wipes": wipes,
            "unfinished": int(unfinished),
            "first_order_at": first,
        }

    # ---------- Периоды таблицы №2 («Скачать и обнулить», 28.10.2026)
    ACTIVE_STATUSES = ("new", "accepted", "in_transit", "not_delivered", "disputed")

    @staticmethod
    def _in_period(at: str | None, period: tuple[str | None, str | None]) -> bool:
        """at в [since, until) — сравнение по времени (ISO с поясом)."""
        if not at:
            return False
        since, until = period
        t = datetime.fromisoformat(str(at))
        return ((since is None or t >= datetime.fromisoformat(since))
                and (until is None or t < datetime.fromisoformat(until)))

    def export_resets(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM export_resets ORDER BY at, id")]

    def add_export_reset(self, at: str, admin_id: int | None, admin_name: str | None) -> int:
        with self.connection() as conn:
            return int(conn.execute("INSERT INTO export_resets (at, admin_id, admin_name) VALUES (?, ?, ?)",
                                    (at, admin_id, admin_name)).lastrowid)

    def _order_in_period(self, o: dict, period: tuple[str | None, str | None]) -> bool:
        """Заказ в таблице №2 периода: оформлен в периоде; или закрыт в периоде (доставлен — по времени
        доставки); или на конец периода ещё не закрыт. Так заказ, оформленный до сброса и доставленный
        после, не теряется."""
        if self._in_period(o.get("created_at"), period):
            return True
        if o.get("status") in self.ACTIVE_STATUSES:
            return period[1] is None or self._in_period(o.get("created_at"), (None, period[1]))
        closed = (o.get("delivered_at") if o.get("status") == "delivered" else None) or o.get("updated_at")
        return self._in_period(closed, period)

    def get_full_export(self, period: tuple[str | None, str | None] | None = None) -> dict[str, Any]:
        """Всё для полной выгрузки: заказы (текущие и из сброшенной истории),
        покупатели, курьеры, отзывы курьеров о покупателях.
        period (since, until) — таблица №2 (28.10.2026): только заказы, отзывы, переписка и действия
        админов этого периода; покупатели, партнёры, курьеры, админы и накопленные бонусы / баллы — все."""
        with self.connection() as conn:
            qr_by_id = self._qr_map(conn)
            orders = conn.execute(
                """
                SELECT o.*, u.username AS customer_username,
                       c.name AS courier_name, c.username AS courier_username, c.phone AS courier_phone,
                       (SELECT rating FROM courier_ratings WHERE order_id = o.id) AS courier_rating,
                       (SELECT rating FROM customer_reviews WHERE order_id = o.id AND rating IS NOT NULL) AS customer_rating,
                       (SELECT group_concat(comment, ' | ') FROM customer_reviews
                         WHERE order_id = o.id AND comment IS NOT NULL) AS review_comments
                FROM orders o
                LEFT JOIN users u ON u.telegram_id = o.user_id
                LEFT JOIN couriers c ON c.id = o.courier_id
                ORDER BY o.created_at, o.id
                """
            ).fetchall()
            items = conn.execute(
                "SELECT order_id, product_name, variant, quantity, unit_price FROM order_items ORDER BY order_id, id"
            ).fetchall()
            returns_by_order: dict[int, list[Any]] = {}
            for r in conn.execute("SELECT order_id, product_name, quantity FROM order_returns ORDER BY order_id, id").fetchall():
                returns_by_order.setdefault(int(r["order_id"]), []).append(dict(r))
            archive = conn.execute(
                """SELECT a.*, w.wiped_at FROM sales_archive a JOIN history_wipes w ON w.id = a.wipe_id
                   ORDER BY a.order_created_at, a.order_no, a.id"""
            ).fetchall()
            users = conn.execute("SELECT * FROM users ORDER BY telegram_id").fetchall()
            couriers = conn.execute("SELECT * FROM couriers ORDER BY id").fetchall()
            admin_actions = conn.execute("SELECT * FROM admin_actions ORDER BY id").fetchall()
            reviews = conn.execute("SELECT * FROM customer_reviews ORDER BY id").fetchall()
            order_chats = conn.execute(
                """SELECT m.*, o.status AS order_status FROM order_messages m
                   LEFT JOIN orders o ON o.id = m.order_id AND m.archived = 0
                   ORDER BY m.archived DESC, m.order_id, m.id""").fetchall()
        by_order: dict[int, list[Any]] = {}
        for i in items:
            by_order.setdefault(int(i["order_id"]), []).append(i)
        current = []
        for o in orders:
            d = dict(o)
            d.update(self._payment_info(o, qr_by_id))
            d["items"] = [dict(i) for i in by_order.get(int(o["id"]), [])]
            d["returns"] = returns_by_order.get(int(o["id"]), [])
            current.append(d)
        archive = [dict(a) for a in archive]
        reviews = [dict(r) for r in reviews]
        admin_actions = [dict(a) for a in admin_actions]
        order_chats = [dict(m) for m in order_chats]
        if period is not None:
            current = [o for o in current if self._order_in_period(o, period)]
            archive = [a for a in archive if self._in_period(a.get("order_created_at"), period)
                       or self._in_period(a.get("order_closed_at"), period)]
            reviews = [r for r in reviews if self._in_period(r.get("created_at"), period)]
            admin_actions = [a for a in admin_actions if self._in_period(a.get("created_at"), period)]
            order_chats = [m for m in order_chats if self._in_period(m.get("created_at"), period)]
        return {
            "period": period,
            "orders": current,
            "archive": archive,
            "users": [dict(u) for u in users],
            "couriers": [dict(c) for c in couriers],
            "reviews": reviews,
            "partners": self.get_partners(),
            "bonus_by_order": self.bonus_by_orders(),
            "bonus_balances": self.bonus_balances(),
            "bonus_totals": self.bonus_totals(),
            "bonus_default_percent": self.BONUS_DEFAULT_PERCENT,
            "admin_actions": admin_actions,
            "order_chats": order_chats,
        }

    # ------------------------------------------------------------ QR для оплаты при курьере
    def add_payment_qr_code(self, kind: str, title: str, currency: str | None, network: str | None, file_id: str) -> int:
        with self.connection() as conn:
            return int(conn.execute(
                """INSERT INTO payment_qr_codes (kind, title, currency, network, file_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (kind, title, currency, network, file_id, self.now()),
            ).lastrowid)

    def get_payment_qr_codes(self, kind: str | None = None) -> list[sqlite3.Row]:
        with self.connection() as conn:
            if kind:
                return list(conn.execute(
                    "SELECT * FROM payment_qr_codes WHERE is_active = 1 AND kind = ? ORDER BY id", (kind,)
                ).fetchall())
            return list(conn.execute(
                "SELECT * FROM payment_qr_codes WHERE is_active = 1 ORDER BY kind DESC, id"
            ).fetchall())

    def get_payment_qr_code(self, code_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM payment_qr_codes WHERE id = ? AND is_active = 1", (code_id,)
            ).fetchone()

    def get_payment_qr_code_any(self, code_id: int) -> sqlite3.Row | None:
        """QR по id, даже если админ его уже убрал (для подписи в заказе)."""
        with self.connection() as conn:
            return conn.execute("SELECT * FROM payment_qr_codes WHERE id = ?", (code_id,)).fetchone()

    def remove_payment_qr_code(self, code_id: int) -> bool:
        with self.connection() as conn:
            cur = conn.execute("UPDATE payment_qr_codes SET is_active = 0 WHERE id = ? AND is_active = 1", (code_id,))
        return cur.rowcount == 1

    def set_order_pay_on_delivery(
        self, order_id: int, crypto_quotes: str | None, payment_qr_id: int | None = None
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET pay_on_delivery = 1, crypto_quotes = ?, payment_qr_id = ? WHERE id = ?",
                (crypto_quotes, payment_qr_id, order_id),
            )

    def set_order_crypto_quotes(self, order_id: int, crypto_quotes: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE orders SET crypto_quotes = ? WHERE id = ?", (crypto_quotes, order_id))

    def confirm_order_payment(self, order_id: int, file_id: str, paid_qr_id: int | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE orders SET payment_confirmed_at = ?, payment_confirm_file_id = ?,
                       paid_qr_id = COALESCE(?, paid_qr_id), updated_at = ? WHERE id = ?""",
                (self.now(), file_id, paid_qr_id, self.now(), order_id),
            )

    def get_payment_switches(self) -> dict[str, bool]:
        """Переключатели «СБП» и «Криптовалюта» в админке: доступны ли способы всем покупателям."""
        with self.connection() as conn:
            row = conn.execute("SELECT sbp_enabled, crypto_enabled FROM shop_settings WHERE id = 1").fetchone()
        if not row:
            return {"sbp": True, "crypto": True}
        return {"sbp": bool(row["sbp_enabled"]), "crypto": bool(row["crypto_enabled"])}

    def set_payment_switch(self, method: str, enabled: bool) -> dict[str, bool]:
        if method not in ("sbp", "crypto"):
            raise ValueError("unknown method")
        with self.connection() as conn:
            conn.execute(f"UPDATE shop_settings SET {method}_enabled = ? WHERE id = 1", (1 if enabled else 0,))
        return self.get_payment_switches()

    # ------------------------------------------------------------ смены курьеров
    # 28.10.2026 (владелец): «Убери вообще работу по регионам… С этого момента только администратор
    # раздаёт заказы… Разбивку по регионам оставь». Регион заказа (orders.region) остаётся — для
    # фильтров админа и Excel; курьеры регион не выбирают и заказы сами не берут.
    def regions_enabled(self) -> bool:
        return False

    # Смена (23.10.2026): курьер на смене, пока отвечает «Да, работаем!» — вопрос каждый час.
    # Не ответил за 30 минут и нет заказов в работе — офлайн; есть заказы — остаётся на смене.
    SHIFT_ASK_MINUTES = 60
    SHIFT_ANSWER_MINUTES = 10  # 28.10.2026: 10 минут на ответ, потом «Не отвечает на опрос» (смену закрывает админ)

    # ---------- геопозиция курьера (24.10.2026): трансляция в чате с ботом обязательна
    MARKER_COLORS = 30  # 15 цветов × 2 формы (кружок, квадрат) — до 30 курьеров без повторов

    def courier_live(self, courier: sqlite3.Row | dict | None) -> dict[str, Any]:
        """Трансляция курьера в боте: active — идёт сейчас (начата, не остановлена, срок не вышел);
        lat/lon/at — последняя известная точка (из трансляции или из открытого приложения)."""
        c = dict(courier) if courier else {}
        active = False
        if c.get("live_src_msg") and not c.get("live_stopped") and c.get("live_until"):
            try:
                active = datetime.fromisoformat(c["live_until"]) > datetime.now(timezone.utc)
            except ValueError:
                active = False
        return {"active": active, "lat": c.get("live_lat"), "lon": c.get("live_lon"), "at": c.get("live_at"),
                "until": c.get("live_until") if active else None}

    def _courier_point(self, conn, courier_id: int, lat: float, lon: float) -> None:
        """Точка курьера — ему и всем его заказам в работе (покупатели видят его на карте)."""
        conn.execute("UPDATE couriers SET live_lat = ?, live_lon = ?, live_at = ? WHERE id = ?",
                     (lat, lon, self.now(), courier_id))
        conn.execute("""UPDATE orders SET courier_last_lat = ?, courier_last_lon = ?
                        WHERE courier_id = ? AND status IN ('accepted', 'in_transit')""", (lat, lon, courier_id))

    def start_courier_live(self, telegram_id: int, chat_id: int, message_id: int,
                           lat: float, lon: float, until: str) -> sqlite3.Row | None:
        """Курьер включил трансляцию геопозиции в чате с ботом."""
        with self.connection() as conn:
            c = conn.execute("SELECT id FROM couriers WHERE telegram_id = ? AND is_active = 1", (telegram_id,)).fetchone()
            if not c:
                return None
            conn.execute("""UPDATE couriers SET live_src_chat = ?, live_src_msg = ?, live_until = ?, live_stopped = 0
                            WHERE id = ?""", (chat_id, message_id, until, c["id"]))
            self._courier_point(conn, int(c["id"]), lat, lon)
            return conn.execute("SELECT * FROM couriers WHERE id = ?", (c["id"],)).fetchone()

    def update_courier_live(self, chat_id: int, message_id: int, lat: float | None, lon: float | None,
                            stopped: bool = False) -> sqlite3.Row | None:
        """Обновление (или остановка) трансляции — по сообщению, с которого она началась."""
        with self.connection() as conn:
            c = conn.execute("SELECT id FROM couriers WHERE live_src_chat = ? AND live_src_msg = ?",
                             (chat_id, message_id)).fetchone()
            if not c:
                return None
            if lat is not None and lon is not None:
                self._courier_point(conn, int(c["id"]), lat, lon)
            if stopped:
                conn.execute("UPDATE couriers SET live_stopped = 1 WHERE id = ?", (c["id"],))
            return conn.execute("SELECT * FROM couriers WHERE id = ?", (c["id"],)).fetchone()

    def set_courier_point(self, courier_id: int, lat: float, lon: float) -> None:
        """Точка из открытого приложения курьера (дополнительно к трансляции)."""
        with self.connection() as conn:
            self._courier_point(conn, courier_id, lat, lon)

    def courier_busy(self, courier_id: int) -> int:
        """Сколько заказов у курьера в работе (взят / в пути)."""
        with self.connection() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM orders WHERE courier_id = ? AND status IN ('accepted', 'in_transit')",
                (courier_id,)).fetchone()[0])

    def courier_shift(self, courier: sqlite3.Row | dict | None) -> dict[str, Any]:
        """Смена курьера. status: 'online' (на смене) / 'break' (перерыв) / 'offline' /
        'ending' («Завершить смену» ждёт администратора) / 'silent' (не ответил на опрос за 10 минут —
        заказы ему не выдаются и не назначаются, смену закрывает администратор; ответит «Да» — снова 'online').
        ask — пора спросить «Вы ещё на смене?» (окно, которое нельзя закрыть)."""
        c = dict(courier) if courier else {}
        state = c.get("shift_state") if c.get("shift_started_at") else None
        try:
            regions = json.loads(c.get("shift_regions") or "[]")
        except ValueError:
            regions = []
        regions = regions if isinstance(regions, list) else []
        out = {"active": False, "status": "offline", "regions": [], "change": None, "started_at": None,
               "ask": False, "busy": 0, "ended_at": c.get("shift_ended_at"), "end_reason": c.get("shift_end_reason"),
               "confirmed_at": None}
        if state == "break":
            out.update(status="break", regions=regions, started_at=c.get("shift_started_at"))
            return out
        if state == "ending":
            out.update(status="ending", regions=regions, started_at=c.get("shift_started_at"),
                       end_requested_at=c.get("shift_end_requested_at"),
                       busy=self.courier_busy(int(c["id"])) if c.get("id") is not None else 0)
            return out
        if state != "on":
            return out
        busy = self.courier_busy(int(c["id"])) if c.get("id") is not None else 0
        confirmed = c.get("shift_confirmed_at") or c["shift_started_at"]
        try:
            due = datetime.fromisoformat(confirmed) + timedelta(minutes=self.SHIFT_ASK_MINUTES)
        except ValueError:
            due = datetime.now(timezone.utc)
        deadline = due + timedelta(minutes=self.SHIFT_ANSWER_MINUTES)
        now = datetime.now(timezone.utc)
        if now >= deadline:
            # 28.10.2026: не ответил за 10 минут — смена сама не закрывается, курьер «не отвечает на опрос».
            out.update(status="silent", regions=regions, change=c.get("region_change"), started_at=c["shift_started_at"],
                       ask=True, busy=busy, confirmed_at=confirmed, silent_since=deadline.isoformat(timespec="seconds"))
            return out
        out.update(active=True, status="online", regions=regions, change=c.get("region_change"),
                   started_at=c["shift_started_at"], ask=now >= due, busy=busy, confirmed_at=confirmed)
        return out

    def _end_shift(self, courier_id: int, reason: str, at: str | None = None) -> None:
        state = "break" if reason == "break" else "off"
        at = at or self.now()
        if reason == "break":
            self.courier_event(courier_id, "break", at=at)
        else:
            self._close_shift(courier_id, reason, at)
        with self.connection() as conn:
            conn.execute(
                """UPDATE couriers SET shift_state = ?, shift_ended_at = ?, shift_end_reason = ?,
                       region_change = NULL, region_change_at = NULL, break_seen_at = NULL WHERE id = ?""",
                (state, at, reason, courier_id))

    def end_courier_shift(self, courier_id: int, reason: str = "self") -> None:
        """Закрыть смену сразу ('self', 'admin') или «Взять перерыв» ('break')."""
        self._end_shift(courier_id, reason)

    def request_shift_end(self, courier_id: int) -> None:
        """«Завершить смену» (28.10.2026): смена не закрывается, пока администратор не подтвердит."""
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET shift_state = 'ending', shift_end_requested_at = ?, region_change = NULL, "
                         "region_change_at = NULL WHERE id = ? AND shift_state IN ('on', 'break')", (self.now(), courier_id))

    def shift_end_view(self, courier: sqlite3.Row | dict) -> dict[str, Any]:
        """Что видит администратор при закрытии смены: полные товары, пустые с замен и с возвратов, деньги."""
        c = dict(courier)
        view = self._stock_view(c, c.get("shift_started_at")) if c.get("shift_stock") else None
        return {"stock": [{"id": it["id"], "name": it["name"], "qty": it["now"]} for it in (view or {}).get("items", [])],
                "empties": self.courier_empties_split(int(c["id"])),
                "counters": self.courier_counters(int(c["id"]))}

    def close_courier_shift(self, courier_id: int, reason: str) -> bool:
        """Администратор подтвердил конец смены ('self') или закрыл смену молчащего курьера ('silent')."""
        c = self.get_courier(courier_id)
        if not c or c["shift_state"] not in ("on", "ending", "break"):
            return False
        self._end_shift(courier_id, reason)
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET shift_end_requested_at = NULL WHERE id = ?", (courier_id,))
        return True

    def ask_all_couriers(self) -> int:
        """«📣 Опросить всех»: вопрос «Вы ещё на смене?» сразу всем на смене; часовой отсчёт — заново."""
        at = (datetime.now(timezone.utc) - timedelta(minutes=self.SHIFT_ASK_MINUTES)).isoformat(timespec="seconds")
        with self.connection() as conn:
            return conn.execute("UPDATE couriers SET shift_confirmed_at = ? WHERE shift_state = 'on' AND is_active = 1",
                                (at,)).rowcount

    def mark_break_seen(self, courier_id: int) -> bool:
        with self.connection() as conn:
            return conn.execute("UPDATE couriers SET break_seen_at = ? WHERE id = ? AND shift_state = 'break'",
                                (self.now(), courier_id)).rowcount == 1

    def confirm_courier_shift(self, courier_id: int) -> None:
        """«Да, работаем!» — следующий вопрос через час."""
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET shift_confirmed_at = ? WHERE id = ? AND shift_state = 'on'",
                         (self.now(), courier_id))

    def resume_courier_shift(self, courier_id: int) -> None:
        """«Вернуться на смену» после перерыва: регионы и баллоны — прежние."""
        with self.connection() as conn:
            on_break = conn.execute("SELECT 1 FROM couriers WHERE id = ? AND shift_state = 'break'", (courier_id,)).fetchone()
        if on_break:
            self.courier_event(courier_id, "resume")
        with self.connection() as conn:
            conn.execute(
                """UPDATE couriers SET shift_state = 'on', shift_confirmed_at = ?, shift_ended_at = NULL,
                       shift_end_reason = NULL WHERE id = ? AND shift_state = 'break'""",
                (self.now(), courier_id))

    def start_courier_shift(self, telegram_id: int, regions: list[str], stock: dict[int, int] | None = None) -> None:
        now = self.now()
        with self.connection() as conn:
            row = conn.execute("SELECT id, current_shift_id FROM couriers WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            # Прежняя смена почему-то не закрыта (например, обновление посреди смены) — закрываем.
            if row["current_shift_id"]:
                self._close_shift(int(row["id"]), "restart", now)
            stock_json = self.dump_stock(stock)
            with self.connection() as conn:
                shift_id = int(conn.execute(
                    "INSERT INTO courier_shifts (courier_id, started_at, regions, stock_start) VALUES (?, ?, ?, ?)",
                    (int(row["id"]), now, json.dumps(regions), stock_json)).lastrowid)
                conn.execute("UPDATE couriers SET current_shift_id = ? WHERE id = ?", (shift_id, int(row["id"])))
            names = self.product_names(stock or {})
            self.courier_event(int(row["id"]), "shift_start", data={
                "regions": regions, "stock": {names.get(k, f"товар {k}"): v for k, v in (stock or {}).items()}}, at=now)
        with self.connection() as conn:
            conn.execute(
                """UPDATE couriers SET shift_started_at = ?, shift_regions = ?, region_change = NULL,
                       region_change_at = NULL, shift_stock = ?, shift_reload = NULL, shift_reload_at = NULL,
                       shift_state = 'on', shift_confirmed_at = ?, shift_ended_at = NULL, shift_end_reason = NULL
                   WHERE telegram_id = ?""",
                (now, json.dumps(regions), self.dump_stock(stock), now, telegram_id))

    # ---------- журнал смен и деньги курьера (25.10.2026)
    COUNTERS = ("earned", "cash", "returns", "keep")

    def courier_delivery_rate(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT courier_delivery_rate FROM shop_settings WHERE id = 1").fetchone()
        return int(row["courier_delivery_rate"]) if row and row["courier_delivery_rate"] is not None else 1000

    def courier_rates(self) -> dict[str, int]:
        with self.connection() as conn:
            row = conn.execute("SELECT courier_rate_balloon, courier_rate_balloon_next, courier_rate_return, "
                               "courier_rate_descent FROM shop_settings WHERE id = 1").fetchone()
        first = int(row["courier_rate_balloon"] or 0) if row else 0
        nxt = row["courier_rate_balloon_next"] if row else None
        return {"balloon": first, "balloon_next": first if nxt is None else int(nxt),
                "return": int(row["courier_rate_return"] or 0) if row else 0,
                "descent": int(row["courier_rate_descent"] or 0) if row else 0}

    @staticmethod
    def balloons_pay(rates: dict[str, int], balloons: int) -> int:
        """Оплата курьеру за баллоны одного заказа: первый — «balloon», каждый следующий — «balloon_next»."""
        n = max(0, int(balloons))
        return rates["balloon"] + rates["balloon_next"] * (n - 1) if n else 0

    def set_courier_rates(self, balloon: int, return_: int, descent: int | None = None,
                          balloon_next: int | None = None) -> dict[str, int]:
        with self.connection() as conn:
            if balloon_next is not None:
                conn.execute("UPDATE shop_settings SET courier_rate_balloon_next = ? WHERE id = 1", (int(balloon_next),))
            conn.execute("UPDATE shop_settings SET courier_rate_balloon = ?, courier_rate_return = ? WHERE id = 1",
                         (int(balloon), int(return_)))
            if descent is not None:
                conn.execute("UPDATE shop_settings SET courier_rate_descent = ? WHERE id = 1", (int(descent),))
        return self.courier_rates()

    def set_courier_delivery_rate(self, rate: int) -> int:
        with self.connection() as conn:
            conn.execute("UPDATE shop_settings SET courier_delivery_rate = ? WHERE id = 1", (int(rate),))
        return self.courier_delivery_rate()

    @staticmethod
    def _journal_safe(fn) -> None:
        """Журнал курьера не должен ломать основное действие (заказ уже сохранён)."""
        try:
            fn()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("Журнал курьера: запись не удалась")

    def courier_event(self, courier_id: int, kind: str, order_id: int | None = None, earned: int = 0,
                      cash: int = 0, returns: int = 0, data: dict[str, Any] | None = None,
                      at: str | None = None, keep: int = 0) -> int:
        """Запись в журнал курьера; смена — открытая сейчас (NULL — курьер не на смене)."""
        with self.connection() as conn:
            row = conn.execute("SELECT current_shift_id FROM couriers WHERE id = ?", (courier_id,)).fetchone()
            shift_id = row["current_shift_id"] if row else None
            return int(conn.execute(
                """INSERT INTO courier_events (courier_id, shift_id, kind, order_id, earned, cash, returns, data, created_at, keep)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (courier_id, shift_id, kind, order_id, int(earned), int(cash), int(returns),
                 json.dumps(data, ensure_ascii=False) if data is not None else None, at or self.now(), int(keep))).lastrowid)

    def _close_shift(self, courier_id: int, reason: str, at: str) -> None:
        """Конец смены: остатки баллонов на этот момент — в смену, курьер без открытой смены."""
        with self.connection() as conn:
            c = conn.execute("SELECT * FROM couriers WHERE id = ?", (courier_id,)).fetchone()
        if not c or not c["current_shift_id"]:
            return
        c = dict(c)
        stock = self._stock_snapshot(c)
        empty = self.courier_empties(courier_id)
        # 28.10.2026: остаток полных — следующая смена начинается с него (без ввода, если не со склада).
        view = self._stock_view(c, c.get("shift_started_at")) if c.get("shift_stock") else None
        if view is not None:
            left = {int(it["id"]): max(0, int(it["now"])) for it in view["items"]}
            with self.connection() as conn:
                conn.execute("UPDATE couriers SET stock_left = ? WHERE id = ?", (self.dump_stock(left), courier_id))
        self.courier_event(courier_id, "shift_end", data={"reason": reason, "stock": stock, "empty": empty}, at=at)
        with self.connection() as conn:
            conn.execute(
                "UPDATE courier_shifts SET ended_at = ?, end_reason = ?, stock_end = ?, empty_end = ? WHERE id = ? AND ended_at IS NULL",
                (at, reason, json.dumps(stock) if stock is not None else None, json.dumps(empty), int(c["current_shift_id"])))
            conn.execute("UPDATE couriers SET current_shift_id = NULL WHERE id = ?", (courier_id,))

    def _order_snapshot(self, order_id: int) -> dict[str, Any]:
        """Что показать в журнале смены, даже если заказ потом удалят из истории."""
        with self.connection() as conn:
            o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            items = conn.execute("SELECT product_name, quantity, variant, is_gift FROM order_items WHERE order_id = ? ORDER BY id",
                                 (order_id,)).fetchall() if o else []
            returns_rows = conn.execute("SELECT product_name, quantity FROM order_returns WHERE order_id = ?",
                                        (order_id,)).fetchall() if o else []
        if not o:
            return {}
        o = dict(o)
        is_return = (o.get("kind") or "order") == "return"
        names = {"exchange": " (замена)"}
        return {
            "kind": o.get("kind") or "order",
            "address": o.get("delivery_address"),
            "returns_text": ", ".join(f"{r['product_name']} × {r['quantity']}" for r in returns_rows),
            "items": ", ".join(f"{'🎁 ' if it['is_gift'] else ''}{it['product_name']}"
                               f"{' (возврат)' if is_return else (' (подарок)' if it['is_gift'] else names.get(it['variant'], ''))} × {it['quantity']}"
                               for it in items),
            "total": int(o.get("total") or 0),
            "payout": int(o.get("return_payout") or 0),
            "return_mode": o.get("return_mode") or "cash",
            "return_points": int(o.get("return_points") or 0),
            "payment": o.get("payment_method"),
            "lift": int(o.get("floor_lift_floors") or 0) * int(o.get("floor_lift_price") or 0),
            "descent": int(o.get("descent_count") or 0) * int(o.get("descent_price") or 0),
            # Доставка за пределы Москвы (28.10.2026): км, цена за км, платная дорога (входит в delivery_fee).
            "delivery_fee": int(o.get("delivery_fee") or 0),
            "km": int(o.get("delivery_km") or 0),
            "km_price": int(o["delivery_km_price"]) if o.get("delivery_km_price") is not None else None,
            "toll": int(o.get("delivery_toll") or 0),
            # Баллонов в заказе (статистика курьеров, 28.10.2026)
            "balloons": sum(int(it["quantity"]) for it in items if is_balloon(it["product_name"]) and not it["is_gift"]),
        }

    @staticmethod
    def litre_key(liters: float) -> str:
        return str(int(liters)) if float(liters).is_integer() else f"{liters:g}"

    def empty_sizes(self) -> list[str]:
        """Объёмы пустых баллонов (литры, ключи-строки), 28.10.2026: по баллонам каталога
        (раньше — только 2 / 5 / 10 л, и пустые от 8 л, 16 л и т. п. не учитывались)."""
        sizes = set()
        for p in self.get_products(include_inactive=True):
            if is_balloon(p["name"]):
                liters = balloon_liters(p["name"])
                if liters is not None:
                    sizes.add(float(liters))
        if not sizes:
            sizes = {float(x) for x in self.STOCK_SIZES}
        return [self.litre_key(x) for x in sorted(sizes)]

    def _empties_in_order(self, order_id: int) -> dict[str, int]:
        """Пустые баллоны, которые курьер увозит с заказа: все баллоны возврата
        или баллоны «замена» в обычном заказе. Ключ — литры («5», «8», «12.5»)."""
        out: dict[str, int] = {}
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT oi.product_name, oi.quantity FROM order_items oi JOIN orders o ON o.id = oi.order_id
                   WHERE oi.order_id = ? AND (o.kind = 'return' OR oi.variant = 'exchange')""", (order_id,)).fetchall()
            rows = list(rows) + [{"product_name": r["product_name"], "quantity": r["quantity"]} for r in
                                 conn.execute("SELECT product_name, quantity FROM order_returns WHERE order_id = ?", (order_id,)).fetchall()]
        for it in rows:
            if not is_balloon(it["product_name"]):
                continue
            liters = balloon_liters(it["product_name"])
            if liters is not None:
                key = self.litre_key(liters)
                out[key] = out.get(key, 0) + int(it["quantity"])
        return out

    def _courier_order_event(self, order: sqlite3.Row | dict | None, new_status: str) -> None:
        """Событие в журнал курьера при смене статуса его заказа (и деньги при доставке)."""
        if not order:
            return
        o = dict(order)
        courier_id = o.get("courier_id")
        if not courier_id:
            return
        courier_id, order_id = int(courier_id), int(o["id"])
        snap = self._order_snapshot(order_id)
        if new_status == "delivered":
            with self.connection() as conn:
                if conn.execute("SELECT 1 FROM courier_events WHERE order_id = ? AND kind IN ('delivered', 'return_picked')",
                                (order_id,)).fetchone():
                    return  # уже записано (один раз на заказ)
            # 28.10.2026: заработок — ставка × баллоны (отвезённые полные, «Новый» и «Замена» одинаково;
            # в возврате — забранные пустые) + подъём на этаж. Не баллоны в заработок не идут.
            rates = self.courier_rates()
            balloons = int(snap.get("balloons") or 0)
            empties = {str(k): v for k, v in self._empties_in_order(order_id).items() if v}
            if snap.get("kind") == "return":
                rate = rates["return"]
                # Ставка за поездку на возврат (28.10.2026 — за поездку, не за баллон) + «спуск пустого
                # баллона» (покупатель оплатил из выплаты — деньги курьеру).
                # 28.10.2026: заработок за возврат курьер оставляет себе из наличных (keep).
                earned = rate + snap.get("descent", 0)
                # 30.10.2026: возврат баллами — клиенту зачисляются бонусы (один раз на заказ), наличные не отдаются.
                pts = int(snap.get("return_points") or 0)
                if snap.get("return_mode") == "points" and pts > 0:
                    with self.connection() as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO customer_bonuses (user_id, order_id, amount, kind, reason, created_at)
                               VALUES (?, ?, ?, 'accrual', 'Возврат баллона', ?)""",
                            (int(o["user_id"]), order_id, pts, self.now()))
                self.courier_event(courier_id, "return_picked", order_id, earned=earned,
                                   returns=-snap.get("payout", 0), keep=earned,
                                   data={**snap, "rate": rate, "empties": empties,
                                         "pay": {"trip": rate, "descent": snap.get("descent", 0)}})
                return
            rate = rates["balloon"]
            # 01.11.2026: возврат баллами в обычном заказе — клиенту зачисляются бонусы (один раз на заказ).
            pts = int(snap.get("return_points") or 0)
            if snap.get("return_mode") == "points" and pts > 0:
                with self.connection() as conn:
                    # kind 'manual': 'accrual' занят кэшбэком за покупку (уникален на заказ).
                    if not conn.execute("SELECT 1 FROM customer_bonuses WHERE order_id = ? AND kind = 'manual' AND reason = 'Возврат баллона'",
                                        (order_id,)).fetchone():
                        conn.execute(
                            """INSERT INTO customer_bonuses (user_id, order_id, amount, kind, reason, created_at)
                               VALUES (?, ?, ?, 'manual', 'Возврат баллона', ?)""",
                            (int(o["user_id"]), order_id, pts, self.now()))
            lift = snap.get("lift", 0)
            # «Собрано за заказы» — вся сумма, которую курьер взял у покупателя наличными (28.10.2026, владелец:
            # «та же сумма, что у покупателя»): товары + подъём + доставка за МКАД − бонусы. Подъём при этом
            # входит и в «Заработал» — его курьеру платит магазин из кассы (раньше подъём вычитался из «Собрано»,
            # и 1 000 ₽ за подъём не попадали в «Сдать в кассу»).
            is_cash = snap.get("payment") == "cash_on_delivery"
            cash = int(snap.get("total", 0)) if is_cash else 0
            # Доставка за пределы Москвы (владелец 28.10.2026): база — в кассу; км × цена за км — заработок
            # курьера; платная дорога — возвращается курьеру (не заработок). Км нет (сумму вписал админ) — всё в кассу.
            toll = int(snap.get("toll") or 0)
            km, km_price = int(snap.get("km") or 0), snap.get("km_price")
            if km and km_price is None:
                km_price = self.outside_prices()["per_km"]  # заказ оформлен до 28.10.2026 — нынешняя цена
            km_earn = min(km * int(km_price or 0), max(0, int(snap.get("delivery_fee") or 0) - toll)) if km else 0
            # + спуск пустых к «Замене» (старые заказы, до 28.10.2026, 17:07) — тоже курьеру.
            # 30.10.2026: первый баллон в заказе — одна ставка, каждый следующий — другая (обе задаёт админ).
            balloons_pay = self.balloons_pay(rates, balloons)
            earned = balloons_pay + lift + snap.get("descent", 0) + km_earn
            # Курьер сдаёт в кассу после вычета своего заработка (владелец 28.10.2026); заказ по СБП / криптой —
            # деньги у магазина, заработок курьеру выплачивает магазин (keep = 0).
            keep = earned + toll if is_cash else 0
            self.courier_event(courier_id, "delivered", order_id, earned=earned, cash=cash, keep=keep,
                               data={**snap, "rate": rate, "empties": empties,
                                     "pay": {"balloons": balloons_pay, "lift": lift, "descent": snap.get("descent", 0),
                                             "km": km, "km_price": int(km_price or 0), "km_earn": km_earn, "toll": toll}})
            return
        kinds = {"new": "released", "cancelled": "cancelled", "disputed_closed": "cancelled",
                 "not_delivered": "not_delivered", "disputed": "disputed"}
        if new_status in kinds:
            self.courier_event(courier_id, kinds[new_status], order_id, data=snap)
            # Админ отменил заказ, по которому курьер уже взял предоплату: списать её у курьера обратно
            # (деньги админ возвращает покупателю сам). «Спорный — закрыт» и «не доставлен» — предоплата
            # остаётся у курьера в наличных (сдаётся в кассу), заработка с поездки нет.
            if new_status == "cancelled" and int(o.get("prepay_amount") or 0) > 0 and o.get("prepay_taken_at"):
                self.courier_event(courier_id, "prepaid_reverse", order_id, cash=-int(o["prepay_amount"]),
                                   data={"prepay": int(o["prepay_amount"]), "address": o.get("delivery_address")})

    def courier_counters(self, courier_id: int) -> dict[str, Any]:
        """«Заработано», «Наличные за товары», «Возвраты» — суммы после последнего обнуления каждого."""
        out: dict[str, Any] = {}
        with self.connection() as conn:
            for name in self.COUNTERS:
                last = conn.execute(
                    "SELECT id, created_at FROM courier_events WHERE courier_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                    (courier_id, f"reset_{name}")).fetchone()
                total = conn.execute(
                    f"SELECT COALESCE(SUM({name}), 0) FROM courier_events WHERE courier_id = ? AND id > ?",
                    (courier_id, int(last["id"]) if last else 0)).fetchone()[0]
                out[name] = int(total)
                out[f"{name}_since"] = last["created_at"] if last else None
        return out

    def reset_courier_counter(self, courier_id: int, name: str, by: str, by_name: str | None = None) -> int:
        """Обнулить один счётчик; возвращает сумму, которая была до обнуления."""
        if name not in self.COUNTERS:
            raise ValueError("unknown counter")
        before = self.courier_counters(courier_id)[name]
        self.courier_event(courier_id, f"reset_{name}", data={"by": by, "name": by_name, "before": before})
        return before

    def request_counter_reset(self, courier_id: int, names: list[str] | None) -> None:
        """Запрос курьера обнулить счётчики (None — отменить запрос). Обнуляет только админ (28.10.2026)."""
        value = json.dumps({"names": list(names), "at": self.now()}) if names else None
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET counter_reset_request = ? WHERE id = ?", (value, courier_id))

    def counter_reset_request(self, courier: sqlite3.Row | dict | None) -> dict[str, Any] | None:
        c = dict(courier) if courier else {}
        try:
            req = json.loads(c.get("counter_reset_request") or "null")
        except ValueError:
            return None
        return req if isinstance(req, dict) and req.get("names") else None

    def courier_money_events(self, start: str, end: str) -> list[dict[str, Any]]:
        """Доставки и возвраты всех курьеров за [start, end) (UTC, ISO) — для статистики «неделя / месяц»."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT courier_id, kind, order_id, earned, cash, returns, keep, data, created_at FROM courier_events
                   WHERE kind IN ('delivered', 'return_picked') AND created_at >= ? AND created_at < ? ORDER BY id""",
                (start, end)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                data = json.loads(d.pop("data") or "{}")
            except ValueError:
                data = {}
            balloons = data.get("balloons")
            if balloons is None:  # события до 28.10.2026 — считаем по сохранённому списку товаров
                balloons = 0
                for part in str(data.get("items") or "").split(", "):
                    name, sep, qty = part.rpartition(" × ")
                    if sep and qty.isdigit() and is_balloon(name):
                        balloons += int(qty)
            d["balloons"] = int(balloons) if d["kind"] == "delivered" else 0
            out.append(d)
        return out

    def courier_empties(self, courier_id: int) -> dict[str, int]:
        """Пустые баллоны в машине: последнее значение, которое ввёл курьер, + забранные после него."""
        base = {size: 0 for size in self.empty_sizes()}
        with self.connection() as conn:
            last = conn.execute(
                "SELECT id, data FROM courier_events WHERE courier_id = ? AND kind IN ('empties_set', 'empties_handed') "
                "ORDER BY id DESC LIMIT 1", (courier_id,)).fetchone()
            rows = conn.execute(
                """SELECT data FROM courier_events WHERE courier_id = ? AND id > ?
                   AND kind IN ('delivered', 'return_picked', 'transfer_in', 'transfer_out') AND data IS NOT NULL""",
                (courier_id, int(last["id"]) if last else 0)).fetchall()
        if last:
            try:
                base.update({str(k): int(v) for k, v in (json.loads(last["data"]).get("empty") or {}).items()})
            except (ValueError, TypeError, AttributeError):
                pass
        for r in rows:
            try:
                for k, v in (json.loads(r["data"]).get("empties") or {}).items():
                    base[str(k)] = base.get(str(k), 0) + int(v)
            except (ValueError, TypeError, AttributeError):
                continue
        return base

    def courier_empties_split(self, courier_id: int) -> dict[str, dict[str, int]]:
        """Пустые в машине по происхождению (28.10.2026): «exchange» — с замен, «return» — с возвратов,
        «manual» — что было на момент последней правки курьером / сдачи на склад,
        «transfer» — «🔄 Обмен» с другими курьерами: +принял, −отдал."""
        zero = lambda: {size: 0 for size in self.empty_sizes()}  # noqa: E731
        out = {"manual": zero(), "exchange": zero(), "return": zero(), "transfer": zero()}
        with self.connection() as conn:
            last = conn.execute(
                "SELECT id, data FROM courier_events WHERE courier_id = ? AND kind IN ('empties_set', 'empties_handed') "
                "ORDER BY id DESC LIMIT 1", (courier_id,)).fetchone()
            rows = conn.execute(
                """SELECT kind, data FROM courier_events WHERE courier_id = ? AND id > ?
                   AND kind IN ('delivered', 'return_picked', 'transfer_in', 'transfer_out') AND data IS NOT NULL""",
                (courier_id, int(last["id"]) if last else 0)).fetchall()
        if last:
            try:
                out["manual"].update({str(k): int(v) for k, v in (json.loads(last["data"]).get("empty") or {}).items()})
            except (ValueError, TypeError, AttributeError):
                pass
        for r in rows:
            part = out[{"return_picked": "return", "transfer_in": "transfer", "transfer_out": "transfer"}.get(r["kind"], "exchange")]
            try:
                for k, v in (json.loads(r["data"]).get("empties") or {}).items():
                    part[str(k)] = part.get(str(k), 0) + int(v)
            except (ValueError, TypeError, AttributeError):
                continue
        return out

    def hand_over_empties(self, courier_id: int) -> dict[str, int]:
        """«Понял, не забуду» (склад): пустые обнуляются; возвращает, сколько их было."""
        before = self.courier_empties(courier_id)
        if not any(before.values()):
            # Повторная заявка (курьер нажал «Изменить» и отправил снова) — показываем, что сдал в прошлый раз.
            with self.connection() as conn:
                last = conn.execute("SELECT kind, data FROM courier_events WHERE courier_id = ? AND kind IN "
                                    "('empties_set', 'empties_handed', 'delivered', 'return_picked', 'transfer_in', 'transfer_out') "
                                    "ORDER BY id DESC LIMIT 1",
                                    (courier_id,)).fetchone()
            if last and last["kind"] == "empties_handed":
                try:
                    return {str(k): int(v) for k, v in (json.loads(last["data"]).get("before") or {}).items()}
                except (ValueError, TypeError, AttributeError):
                    return before
            return before
        self.courier_event(courier_id, "empties_handed",
                           data={"empty": {size: 0 for size in self.empty_sizes()}, "before": before})
        return before

    def set_courier_empties(self, courier_id: int, empty: dict) -> dict[str, int]:
        self.courier_event(courier_id, "empties_set",
                           data={"empty": {size: int(empty.get(size, empty.get(int(float(size)), 0)) if float(size).is_integer()
                                                     else empty.get(size, 0)) for size in self.empty_sizes()}})
        return self.courier_empties(courier_id)

    # ---------- «🔄 Обмен» между курьерами (владелец 28.10.2026)
    TRANSFER_CANCEL_SECONDS = 30

    @staticmethod
    def _transfer_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["items"] = {int(k): int(v) for k, v in (json.loads(d["items"]) or {}).items()}
        d["empties"] = {str(k): int(v) for k, v in (json.loads(d["empties"]) or {}).items()}
        return d

    def transfer_age(self, t: dict) -> float:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(t["created_at"])).total_seconds()

    def create_transfer(self, from_id: int, to_id: int, items: dict[int, int], empties: dict[str, int]) -> int:
        with self.connection() as conn:
            return int(conn.execute(
                "INSERT INTO courier_transfers (from_id, to_id, items, empties, created_at) VALUES (?, ?, ?, ?, ?)",
                (from_id, to_id, json.dumps({str(k): int(v) for k, v in items.items() if int(v) > 0}),
                 json.dumps({str(k): int(v) for k, v in empties.items() if int(v) > 0}), self.now())).lastrowid)

    def get_transfer(self, transfer_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            return self._transfer_row(conn.execute("SELECT * FROM courier_transfers WHERE id = ?", (transfer_id,)).fetchone())

    def pending_transfer_from(self, courier_id: int) -> dict[str, Any] | None:
        """Заявка курьера, которая ждёт (отмены 30 секунд или решения админа)."""
        with self.connection() as conn:
            return self._transfer_row(conn.execute(
                "SELECT * FROM courier_transfers WHERE from_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (courier_id,)).fetchone())

    def pending_transfers(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [self._transfer_row(r) for r in conn.execute(
                "SELECT * FROM courier_transfers WHERE status = 'pending' ORDER BY id")]

    def set_transfer_status(self, transfer_id: int, status: str, admin_name: str | None = None) -> bool:
        """pending → cancelled / rejected / approved. False — заявка уже не ждёт."""
        with self.connection() as conn:
            cur = conn.execute("UPDATE courier_transfers SET status = ?, decided_at = ?, admin_name = ? "
                               "WHERE id = ? AND status = 'pending'", (status, self.now(), admin_name, transfer_id))
        return cur.rowcount == 1

    def mark_transfer_alerted(self, transfer_id: int) -> bool:
        """Уведомление админу в бот — один раз (True — отметили сейчас)."""
        with self.connection() as conn:
            cur = conn.execute("UPDATE courier_transfers SET alerted = 1 WHERE id = ? AND alerted = 0 AND status = 'pending'",
                               (transfer_id,))
        return cur.rowcount == 1

    def apply_transfer(self, t: dict, names: dict[int, str], from_name: str, to_name: str) -> None:
        """После одобрения: журнал обоих курьеров; пустые — со знаком (полные считает _transfer_delta)."""
        items = {names.get(pid, f"товар {pid}"): n for pid, n in t["items"].items()}
        self.courier_event(int(t["from_id"]), "transfer_out", data={
            "transfer_id": t["id"], "to": to_name, "items": items, "empties": {k: -v for k, v in t["empties"].items()}})
        self.courier_event(int(t["to_id"]), "transfer_in", data={
            "transfer_id": t["id"], "from": from_name, "items": items, "empties": dict(t["empties"])})

    def courier_shifts(self, courier_id: int, limit: int = 60) -> list[dict[str, Any]]:
        """Смены курьера, новые сверху, с итогами (деньги и число доставок)."""
        with self.connection() as conn:
            shifts = [dict(r) for r in conn.execute(
                "SELECT * FROM courier_shifts WHERE courier_id = ? ORDER BY id DESC LIMIT ?", (courier_id, limit))]
            for sh in shifts:
                agg = conn.execute(
                    """SELECT COALESCE(SUM(earned), 0) AS earned, COALESCE(SUM(cash), 0) AS cash,
                              COALESCE(SUM(returns), 0) AS returns, COALESCE(SUM(keep), 0) AS keep,
                              SUM(kind = 'delivered') AS deliveries, SUM(kind = 'return_picked') AS pickups
                       FROM courier_events WHERE shift_id = ?""", (sh["id"],)).fetchone()
                sh.update({k: int(agg[k] or 0) for k in ("earned", "cash", "returns", "keep", "deliveries", "pickups")})
        return shifts

    def shift_money(self, courier: sqlite3.Row | dict) -> dict[str, Any] | None:
        """«💰 Деньги» за смену (владелец 28.10.2026): что взял, что отдал за пустые, заработок по статьям,
        платная дорога, сколько сдать в кассу (на руках − заработок по наличным), что доплатит магазин (СБП / крипта).
        Смена — текущая, иначе последняя. Всё из журнала курьера (суммы, записанные при доставке)."""
        c = dict(courier)
        sid = c.get("current_shift_id")
        with self.connection() as conn:
            if not sid:
                row = conn.execute("SELECT id FROM courier_shifts WHERE courier_id = ? ORDER BY id DESC LIMIT 1", (int(c["id"]),)).fetchone()
                sid = row["id"] if row else None
            if not sid:
                return None
            sh = conn.execute("SELECT * FROM courier_shifts WHERE id = ?", (sid,)).fetchone()
            rows = conn.execute("""SELECT kind, earned, cash, returns, keep, data FROM courier_events
                                   WHERE shift_id = ? AND kind IN ('delivered', 'return_picked', 'prepaid', 'prepaid_reverse')
                                   ORDER BY id""", (sid,)).fetchall()
        z = lambda: {"cash": 0, "noncash": 0}  # noqa: E731
        earn = {k: z() for k in ("balloons", "lift", "descent", "km", "trip")}
        counts = {"balloons": 0, "lifts": 0, "km": 0, "trips": 0, "orders": 0, "noncash_orders": 0}
        toll = z()
        cash = returns = keep = 0
        for r in rows:
            try:
                d = json.loads(r["data"] or "{}")
            except ValueError:
                d = {}
            pay = d.get("pay") or {}
            cash += int(r["cash"] or 0)
            returns += -int(r["returns"] or 0)
            keep += int(r["keep"] or 0)
            if r["kind"] in ("prepaid", "prepaid_reverse"):
                continue  # предоплата за доставку: только наличные на руках (30.10.2026)
            if r["kind"] == "return_picked":
                part = "cash"
                trip = int(pay.get("trip", d.get("rate", 0)) or 0)
                earn["trip"][part] += trip
                earn["descent"][part] += int(r["earned"] or 0) - trip
                counts["trips"] += 1
                continue
            counts["orders"] += 1
            part = "cash" if d.get("payment") == "cash_on_delivery" else "noncash"
            counts["noncash_orders"] += part == "noncash"
            bal = int(pay.get("balloons", int(d.get("rate") or 0) * int(d.get("balloons") or 0)) or 0)
            lift = int(pay.get("lift", d.get("lift", 0)) or 0)
            km_earn = int(pay.get("km_earn", 0) or 0)
            earn["balloons"][part] += bal
            earn["lift"][part] += lift
            earn["km"][part] += km_earn
            earn["descent"][part] += int(r["earned"] or 0) - bal - lift - km_earn
            toll[part] += int(pay.get("toll", 0) or 0)
            counts["balloons"] += int(d.get("balloons") or 0)
            counts["lifts"] += 1 if lift else 0
            counts["km"] += int(pay.get("km", 0) or 0) if km_earn else 0
        earned_cash = sum(v["cash"] for v in earn.values())
        earned_noncash = sum(v["noncash"] for v in earn.values())
        on_hand = cash - returns
        return {
            "shift_id": sid, "started_at": sh["started_at"] if sh else None, "ended_at": sh["ended_at"] if sh else None,
            "cash": cash, "returns": returns, "on_hand": on_hand,
            "earn": earn, "counts": counts, "toll": toll,
            "earned": earned_cash + earned_noncash, "earned_cash": earned_cash, "earned_noncash": earned_noncash,
            # Оставляет себе из наличных: заработок по наличным заказам и возвратам + платная дорога по наличным.
            # До 28.10.2026 keep не записывался — тогда «сдать» считается по записанному keep (как в счётчиках).
            "keep": keep, "kassa": on_hand - keep,
            # Остальное (заработок по СБП / крипте и платная дорога по ним) магазин выплачивает сам:
            # заработок + платная дорога = оставил себе + выплатит магазин.
            "shop_owes": earned_cash + earned_noncash + toll["cash"] + toll["noncash"] - keep,
        }

    def courier_shift_detail(self, shift_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            sh = conn.execute("SELECT * FROM courier_shifts WHERE id = ?", (shift_id,)).fetchone()
            if not sh:
                return None
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM courier_events WHERE shift_id = ? ORDER BY id", (shift_id,))]
        return {"shift": dict(sh), "events": events}

    # ---------- учёт баллонов курьера (23.10.2026): 2, 5 и 10 литров
    STOCK_SIZES = (2, 5, 10)

    # ---------- учёт товаров курьера (26.10.2026): все позиции каталога, по номеру товара.
    # Хранится как {"v": 2, "items": {"<id товара>": штук}}. Старый формат (литры 2/5/10)
    # переводится в товары-баллоны с этими литрами — для смен, начатых до обновления.
    STOCK_V = 2

    @staticmethod
    def dump_stock(stock: dict[int, int] | None) -> str | None:
        if stock is None:
            return None
        return json.dumps({"v": 2, "items": {str(int(k)): int(v) for k, v in stock.items()}})

    def parse_stock(self, raw: str | None) -> dict[int, int] | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(data, dict) and data.get("v") == 2:
            try:
                return {int(k): int(v) for k, v in (data.get("items") or {}).items()}
            except (ValueError, TypeError):
                return None
        # старый формат — литры → товары-баллоны с такими литрами (первый по номеру)
        try:
            liters = {int(k): int(v) for k, v in data.items()}
        except (ValueError, TypeError, AttributeError):
            return None
        by_liters: dict[int, int] = {}
        for p in sorted(self.get_products(include_inactive=True), key=lambda r: (not r["is_active"], r["id"])):
            lit = balloon_liters(p["name"]) if is_balloon(p["name"]) else None
            if lit is not None and float(lit).is_integer():
                by_liters.setdefault(int(lit), int(p["id"]))
        return {by_liters[size]: n for size, n in liters.items() if size in by_liters}

    def stock_catalog(self) -> list[dict[str, Any]]:
        """Позиции, которые курьер вносит перед «В путь!» и на складе: весь каталог (активные товары)."""
        return [{"id": int(p["id"]), "name": p["name"]} for p in self.get_products()]

    def product_names(self, ids) -> dict[int, str]:
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        with self.connection() as conn:
            rows = conn.execute(f"SELECT id, name FROM products WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {int(r["id"]): r["name"] for r in rows}

    def order_need(self, order_id: int) -> dict[int, int]:
        """Сколько каждого товара в заказе (новый и замена — одинаково; возврат баллона не требует ничего)."""
        return self.items_in_orders([order_id])

    def items_in_orders(self, order_ids: list[int]) -> dict[int, int]:
        """Сумма товаров по нескольким заказам — {номер товара: штук}, одним запросом."""
        total: dict[int, int] = {}
        if not order_ids:
            return total
        with self.connection() as conn:
            for i in range(0, len(order_ids), 500):  # лимит параметров SQLite
                chunk = order_ids[i:i + 500]
                for it in conn.execute(
                        f"""SELECT oi.product_id, SUM(oi.quantity) AS q FROM order_items oi JOIN orders o ON o.id = oi.order_id
                            WHERE oi.order_id IN ({','.join('?' * len(chunk))}) AND o.kind != 'return'
                              AND oi.product_id IS NOT NULL GROUP BY oi.product_id""", chunk).fetchall():
                    total[int(it["product_id"])] = total.get(int(it["product_id"]), 0) + int(it["q"])
        return total

    def courier_stock(self, courier: sqlite3.Row | dict | None) -> dict[str, Any] | None:
        """Товары курьера за текущую смену: на начало и сейчас (начало − заказы в работе − доставленные
        за смену; отменённые, «взял по ошибке», снятые админом, «не доставлен», спорные — не считаются).
        start / now / reload — {"<id товара>": штук}; items — список для показа с названиями."""
        c = dict(courier) if courier else {}
        shift = self.courier_shift(c)
        if shift["status"] == "offline" or not c.get("shift_stock"):
            return None
        return self._stock_view(c, shift["started_at"])

    def _stock_view(self, c: dict, started_at: str | None) -> dict[str, Any] | None:
        start = self.parse_stock(c.get("shift_stock"))
        if start is None or not started_at:
            return None
        reload, reload_at = None, c.get("shift_reload_at")
        if c.get("shift_reload") and reload_at:
            reload = self.parse_stock(c["shift_reload"])
        now = self._stock_now(c, start, reload, reload_at, started_at)
        ids = list(dict.fromkeys([p["id"] for p in self.stock_catalog()] + list(start) + list(reload or {}) + list(now)))
        names = self.product_names(ids)
        ids = [i for i in ids if i in names]
        full = lambda d: {str(i): int(d.get(i, 0)) for i in ids}  # noqa: E731
        out = {"start": full(start), "now": full(now), "names": {str(i): names[i] for i in ids},
               "items": [{"id": i, "name": names[i], "start": start.get(i, 0), "now": now.get(i, 0),
                          **({"reload": reload.get(i, 0)} if reload is not None else {})} for i in ids]}
        if reload is not None:
            out["reload"] = full(reload)
            out["reload_at"] = reload_at
        return out

    def _stock_now(self, c: dict, start: dict[int, int], reload: dict[int, int] | None,
                   reload_at: str | None, started_at: str) -> dict[int, int]:
        now = dict(reload if reload is not None else start)
        # Из машины уходит: всё, что сейчас у курьера в работе (взято / в пути — в том числе
        # назначенное админом до начала смены), и всё, что он доставил за смену
        # (после загрузки на складе — только доставленное после неё). Отменённые,
        # снятые, «не доставлен», спорные — товары снова в машине.
        since = reload_at if reload is not None else started_at
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id FROM orders WHERE courier_id = ? AND (
                       status IN ('accepted', 'in_transit')
                       OR (status = 'delivered' AND delivered_at >= ?))""",
                (int(c["id"]), since)).fetchall()
        for pid, n in self.items_in_orders([int(r["id"]) for r in rows]).items():
            now[pid] = now.get(pid, 0) - n
        # «🔄 Обмен» (28.10.2026): одобренные админом передачи после начала смены / загрузки на складе.
        for pid, n in self._transfer_delta(int(c["id"]), since).items():
            now[pid] = now.get(pid, 0) + n
        return now

    def _transfer_delta(self, courier_id: int, since: str) -> dict[int, int]:
        """Полные товары по одобренным передачам с момента since: +принял, −отдал."""
        out: dict[int, int] = {}
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT from_id, to_id, items FROM courier_transfers WHERE status = 'approved'
                   AND decided_at >= ? AND (from_id = ? OR to_id = ?)""", (since, courier_id, courier_id)).fetchall()
        for r in rows:
            sign = -1 if int(r["from_id"]) == courier_id else 1
            try:
                items = json.loads(r["items"]) or {}
            except ValueError:
                continue
            for pid, n in items.items():
                out[int(pid)] = out.get(int(pid), 0) + sign * int(n)
        return out

    def _stock_snapshot(self, c: dict) -> dict[str, int] | None:
        """Остатки «сейчас» без проверки статуса смены (для закрытия смены): {название: штук}."""
        view = self._stock_view(c, c.get("shift_started_at"))
        return {it["name"]: it["now"] for it in view["items"]} if view else None

    # ---------- «В путь!» — после подтверждения администратора (26.10.2026)
    def request_shift(self, telegram_id: int, regions: list[str], stock: dict[int, int],
                      source: str = "entry", empties: dict[str, int] | None = None) -> None:
        """Курьер нажал «В путь!» — заявка ждёт администратора. source: 'entry' (первая смена — ввёл всё сам),
        'left' (остаток прошлой смены), 'warehouse' («Стартую со склада»: ввёл всё, пустые сдал — empties)."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE couriers SET shift_request = ?, shift_request_declined_at = NULL WHERE telegram_id = ?",
                (json.dumps({"regions": regions, "stock": {str(k): int(v) for k, v in stock.items()}, "at": self.now(),
                             "source": source, "empties": empties}), telegram_id))

    def stock_left(self, courier: sqlite3.Row | dict | None) -> dict[int, int] | None:
        """Остаток полных после прошлой смены (по всему каталогу), None — смен ещё не было."""
        c = dict(courier) if courier else {}
        left = self.parse_stock(c.get("stock_left"))
        if left is None:
            return None
        return {p["id"]: int(left.get(p["id"], 0)) for p in self.stock_catalog()} | {k: v for k, v in left.items() if v}

    def cancel_shift_request(self, telegram_id: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET shift_request = NULL WHERE telegram_id = ?", (telegram_id,))

    def shift_request(self, courier: sqlite3.Row | dict | None) -> dict[str, Any] | None:
        c = dict(courier) if courier else {}
        if not c.get("shift_request"):
            return None
        try:
            r = json.loads(c["shift_request"])
            stock = {int(k): int(v) for k, v in (r.get("stock") or {}).items()}
        except (ValueError, TypeError, AttributeError):
            return None
        # Товар, добавленный в каталог после заявки, тоже виден (0 шт.) — админ может вписать его в «Исправить».
        for p in self.stock_catalog():
            stock.setdefault(p["id"], 0)
        names = self.product_names(stock)
        return {"regions": r.get("regions") or [], "stock": {str(k): v for k, v in stock.items()}, "at": r.get("at"),
                "source": r.get("source") or "entry", "empties": r.get("empties"),
                "items": [{"id": k, "name": names.get(k, f"товар {k}"), "qty": v} for k, v in stock.items()]}

    def shift_requests(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(
                "SELECT * FROM couriers WHERE shift_request IS NOT NULL AND is_active = 1 ORDER BY id"))

    def resolve_shift_request(self, courier_id: int, approve: bool, stock: dict[int, int] | None = None) -> dict[str, Any] | None:
        """Админ: подтвердить (можно с исправленными количествами) — смена начинается; отклонить —
        курьер правит и отправляет снова."""
        c = self.get_courier(courier_id)
        req = self.shift_request(c)
        if not c or req is None:
            return None
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET shift_request = NULL, shift_request_declined_at = ? WHERE id = ?",
                         (None if approve else self.now(), courier_id))
        final = stock if stock is not None else {int(k): v for k, v in req["stock"].items()}
        if approve:
            self.start_courier_shift(int(c["telegram_id"]), req["regions"], final)
        return {**req, "approved": approve, "final": {str(k): v for k, v in final.items()}}

    def courier_reload(self, telegram_id: int, stock: dict[int, int], empties: dict[str, int] | None = None) -> None:
        """«Заехать на склад»: курьер вписал, сколько товаров у него после загрузки (и сдал пустые — empties,
        сколько их было). Остаток не меняется, пока администратор не подтвердит (resolve_reload)."""
        with self.connection() as conn:
            conn.execute("UPDATE couriers SET reload_pending = ?, reload_pending_at = ?, reload_empties = ? WHERE telegram_id = ?",
                         (self.dump_stock(stock), self.now(), json.dumps(empties) if empties is not None else None, telegram_id))

    def resolve_reload(self, courier_id: int, approve: bool) -> sqlite3.Row | None:
        """Админ: «Подтвердить» — фактические баллоны становятся остатком (на момент загрузки);
        «Отклонить» — остаток прежний."""
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM couriers WHERE id = ? AND reload_pending IS NOT NULL", (courier_id,)).fetchone()
            if not row:
                return None
            if approve:
                conn.execute(
                    """UPDATE couriers SET shift_reload = reload_pending, shift_reload_at = reload_pending_at,
                           reload_pending = NULL, reload_pending_at = NULL, reload_empties = NULL WHERE id = ?""", (courier_id,))
            else:
                conn.execute("UPDATE couriers SET reload_pending = NULL, reload_pending_at = NULL, reload_empties = NULL "
                             "WHERE id = ?", (courier_id,))
        pending = self.parse_stock(row["reload_pending"]) or {}
        names = self.product_names(pending)
        stock = {names.get(k, f"товар {k}"): v for k, v in pending.items()}
        try:
            handed = json.loads(row["reload_empties"]) if row["reload_empties"] else None
        except ValueError:
            handed = None
        self.courier_event(courier_id, "reload" if approve else "reload_rejected", data={"stock": stock, "empties_handed": handed})
        return row

    def reload_requests(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute("SELECT * FROM couriers WHERE reload_pending IS NOT NULL ORDER BY reload_pending_at"))

    # ---------- «📞 Позвать администратора» — список нерешённых обращений
    def add_admin_call(self, user_id: int, text: str) -> int:
        with self.connection() as conn:
            return int(conn.execute("INSERT INTO admin_calls (user_id, text, created_at) VALUES (?, ?, ?)",
                                    (user_id, text, self.now())).lastrowid)

    # ---------- чат с администрацией (25.10.2026)
    def add_support_message(self, user_id: int, from_admin: bool, text: str | None, admin_id: int | None = None,
                            order_id: int | None = None, photo: tuple[bytes, str] | None = None) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mid = int(conn.execute(
                    """INSERT INTO support_messages (user_id, from_admin, admin_id, text, has_photo, order_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, 1 if from_admin else 0, admin_id, text, 1 if photo else 0, order_id, self.now())).lastrowid)
                if photo:
                    conn.execute("INSERT INTO support_photos (message_id, mime, data) VALUES (?, ?, ?)",
                                 (mid, photo[1], sqlite3.Binary(photo[0])))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return mid

    def support_messages(self, user_id: int, limit: int = 300) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM (SELECT * FROM support_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?)
                   ORDER BY id""", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_support_read(self, user_id: int, by_admin: bool) -> None:
        """Прочитано: админ читает сообщения покупателя, покупатель — ответы администрации."""
        with self.connection() as conn:
            conn.execute("UPDATE support_messages SET read_at = ? WHERE user_id = ? AND from_admin = ? AND read_at IS NULL",
                         (self.now(), user_id, 0 if by_admin else 1))

    def support_unread_for_user(self, user_id: int) -> int:
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM support_messages WHERE user_id = ? AND from_admin = 1 AND read_at IS NULL",
                                    (user_id,)).fetchone()[0])

    def support_unread_for_admin(self) -> int:
        """Сколько покупателей ждут ответа (есть непрочитанные сообщения)."""
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(DISTINCT user_id) FROM support_messages WHERE from_admin = 0 AND read_at IS NULL").fetchone()[0])

    def support_threads(self) -> list[dict[str, Any]]:
        """Список чатов для админа: последний ответ сверху, непрочитанные — число."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT m.user_id, m.text, m.has_photo, m.from_admin, m.created_at,
                          (SELECT COUNT(*) FROM support_messages u WHERE u.user_id = m.user_id AND u.from_admin = 0 AND u.read_at IS NULL) AS unread,
                          us.first_name, us.last_name, us.username, us.last_phone
                   FROM support_messages m
                   JOIN (SELECT user_id, MAX(id) AS last_id FROM support_messages GROUP BY user_id) t ON t.last_id = m.id
                   LEFT JOIN users us ON us.telegram_id = m.user_id
                   ORDER BY m.id DESC""").fetchall()
        return [dict(r) for r in rows]

    def delete_support_chat(self, user_id: int) -> int:
        """Админ удалил чат с покупателем: вся переписка и фото (у покупателя тоже)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""DELETE FROM support_photos WHERE message_id IN
                                (SELECT id FROM support_messages WHERE user_id = ?)""", (user_id,))
                n = conn.execute("DELETE FROM support_messages WHERE user_id = ?", (user_id,)).rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return int(n)

    def support_photo(self, message_id: int) -> tuple[int, bytes, str] | None:
        with self.connection() as conn:
            row = conn.execute("""SELECT m.user_id, p.data, p.mime FROM support_photos p
                                  JOIN support_messages m ON m.id = p.message_id WHERE p.message_id = ?""",
                               (message_id,)).fetchone()
        return (int(row["user_id"]), bytes(row["data"]), row["mime"]) if row else None

    # ---------- чат курьера и покупателя внутри заказа (25.10.2026)
    def add_order_message(self, order_id: int, from_courier: bool, text: str | None,
                          courier_id: int | None = None, photo: tuple[bytes, str] | None = None) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                o = conn.execute("""SELECT o.user_id, o.customer_name, o.courier_id, c.name AS courier_name
                                    FROM orders o LEFT JOIN couriers c ON c.id = o.courier_id WHERE o.id = ?""",
                                 (order_id,)).fetchone()
                mid = int(conn.execute(
                    """INSERT INTO order_messages (order_id, from_courier, courier_id, text, has_photo, created_at,
                                                   customer_id, customer_name, courier_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order_id, 1 if from_courier else 0, courier_id if courier_id is not None else (o["courier_id"] if o else None),
                     text, 1 if photo else 0, self.now(), o["user_id"] if o else None,
                     o["customer_name"] if o else None, o["courier_name"] if o else None)).lastrowid)
                if photo:
                    conn.execute("INSERT INTO order_message_photos (message_id, mime, data) VALUES (?, ?, ?)",
                                 (mid, photo[1], sqlite3.Binary(photo[0])))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return mid

    def order_messages(self, order_id: int, limit: int = 300) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM (SELECT * FROM order_messages WHERE order_id = ? AND archived = 0 ORDER BY id DESC LIMIT ?)
                   ORDER BY id""", (order_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def order_chat_counts(self, order_ids: list[int]) -> dict[int, int]:
        """Сколько сообщений в чате курьер–покупатель у каждого заказа (история заказов админа)."""
        ids = [int(i) for i in order_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.connection() as conn:
            rows = conn.execute(f"SELECT order_id, COUNT(*) AS n FROM order_messages WHERE order_id IN ({marks}) AND archived = 0 "
                                "GROUP BY order_id", ids).fetchall()
        return {int(r["order_id"]): int(r["n"]) for r in rows}

    def mark_order_chat_read(self, order_id: int, by_courier: bool) -> None:
        """Курьер читает сообщения покупателя, покупатель — сообщения курьера."""
        with self.connection() as conn:
            conn.execute("UPDATE order_messages SET read_at = ? WHERE order_id = ? AND from_courier = ? AND read_at IS NULL",
                         (self.now(), order_id, 0 if by_courier else 1))

    def order_chat_unread(self, order_ids: list[int], for_courier: bool) -> dict[int, int]:
        """Непрочитанные по заказам: курьеру — от покупателя, покупателю — от курьера."""
        if not order_ids:
            return {}
        marks = ",".join("?" * len(order_ids))
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT order_id, COUNT(*) AS n FROM order_messages
                    WHERE order_id IN ({marks}) AND from_courier = ? AND read_at IS NULL AND archived = 0 GROUP BY order_id""",
                (*order_ids, 0 if for_courier else 1)).fetchall()
        return {int(r["order_id"]): int(r["n"]) for r in rows}

    def order_message_photo(self, message_id: int) -> tuple[int, bytes, str] | None:
        with self.connection() as conn:
            row = conn.execute("""SELECT m.order_id, p.data, p.mime FROM order_message_photos p
                                  JOIN order_messages m ON m.id = p.message_id WHERE p.message_id = ?""",
                               (message_id,)).fetchone()
        return (int(row["order_id"]), bytes(row["data"]), row["mime"]) if row else None

    def open_admin_calls(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT a.*, u.first_name, u.last_name, u.username, u.last_phone FROM admin_calls a
                   LEFT JOIN users u ON u.telegram_id = a.user_id WHERE a.resolved_at IS NULL ORDER BY a.id""").fetchall()
        return [dict(r) for r in rows]

    def resolve_admin_call(self, call_id: int, admin_id: int) -> bool:
        with self.connection() as conn:
            cur = conn.execute("UPDATE admin_calls SET resolved_at = ?, resolved_by = ? WHERE id = ? AND resolved_at IS NULL",
                               (self.now(), admin_id, call_id))
        return cur.rowcount == 1

    def assign_shortage(self, courier: sqlite3.Row | dict | None, order_ids: list[int]) -> dict[str, tuple[int, int]] | None:
        """Можно ли отдать курьеру эти заказы (28.10.2026, «запретить, никаких старых смен»):
        None — у курьера нет учёта товаров (не на смене / смена без ввода товаров) — назначать нельзя;
        {} — хватает; иначе {название: (нужно, есть)} по сумме всех заказов (заказы, уже у него в работе,
        учтены в остатке)."""
        stock = self.courier_stock(courier)
        if stock is None:
            return None
        cid = int(dict(courier)["id"])
        new_ids = []
        for oid in order_ids:
            o = self.get_order(int(oid))
            if o is not None and not (o["courier_id"] is not None and int(o["courier_id"]) == cid):
                new_ids.append(int(oid))
        need = self.items_in_orders(new_ids)
        names = self.product_names(need)
        out = {}
        for pid, n in need.items():
            have = int(stock["now"].get(str(pid), 0))
            if n > have:
                out[names.get(pid, f"товар {pid}")] = (n, max(0, have))
        return out

    def stock_shortage(self, courier: sqlite3.Row | dict | None, order_id: int) -> dict[str, tuple[int, int]]:
        """Каких товаров не хватает на заказ: {название: (нужно, есть)}. Пусто — хватает
        (или учёт не ведётся: нет смены / остатки не вводились)."""
        stock = self.courier_stock(courier)
        if stock is None:
            return {}
        need = self.order_need(order_id)
        names = self.product_names(need)
        out = {}
        for pid, n in need.items():
            have = int(stock["now"].get(str(pid), 0))
            if n > have:
                out[names.get(pid, f"товар {pid}")] = (n, max(0, have))  # в минус — если админ назначил сверх остатка
        return out

    def get_available_orders_for(self, telegram_id: int) -> list[sqlite3.Row]:
        """Свободных заказов у курьеров нет — заказы раздаёт только администратор (28.10.2026)."""
        return []

    def courier_unacked_assignments(self, telegram_id: int) -> list[sqlite3.Row]:
        """Заказы, которые админ назначил курьеру, а тот ещё не нажал «Понятно»."""
        with self.connection() as conn:
            return list(conn.execute(
                """SELECT o.* FROM orders o JOIN couriers c ON c.id = o.courier_id
                   WHERE c.telegram_id = ? AND o.assign_ack = 0 AND o.status IN ('accepted', 'in_transit')
                   ORDER BY o.id""", (telegram_id,)))

    def ack_assignments(self, telegram_id: int, order_ids: list[int]) -> int:
        if not order_ids:
            return 0
        marks = ",".join("?" * len(order_ids))
        with self.connection() as conn:
            cur = conn.execute(
                f"""UPDATE orders SET assign_ack = 1 WHERE id IN ({marks})
                    AND courier_id = (SELECT id FROM couriers WHERE telegram_id = ?)""",
                (*[int(i) for i in order_ids], telegram_id))
        return cur.rowcount

    def set_order_region(self, order_id: int, region: str | None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE orders SET region = ? WHERE id = ?", (region, order_id))

    def get_announcement(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT announcement, announcement_updated_at FROM shop_settings WHERE id = 1").fetchone()
        if not row or not (row["announcement"] or "").strip():
            return None
        return {"text": row["announcement"], "updated_at": row["announcement_updated_at"]}

    def set_announcement(self, text: str | None) -> dict[str, Any] | None:
        """Текст объявления; пусто / None — убрать плашку."""
        text = (text or "").strip() or None
        with self.connection() as conn:
            conn.execute(
                "UPDATE shop_settings SET announcement = ?, announcement_updated_at = ? WHERE id = 1",
                (text, self.now() if text else None),
            )
        return self.get_announcement()

    def outside_prices(self) -> dict[str, int]:
        """Доставка за пределы Москвы: база и ₽ за км (0 — авторасчёт выключен)."""
        with self.connection() as conn:
            row = conn.execute("SELECT outside_base_price, outside_km_price FROM shop_settings WHERE id = 1").fetchone()
        return {"base": int(row["outside_base_price"] or 0) if row else 500,
                "per_km": int(row["outside_km_price"] or 0) if row else 0}

    def set_outside_prices(self, base: int, per_km: int) -> dict[str, int]:
        with self.connection() as conn:
            conn.execute("UPDATE shop_settings SET outside_base_price = ?, outside_km_price = ? WHERE id = 1",
                         (int(base), int(per_km)))
        return self.outside_prices()

    def route_cached(self, key: str) -> float | None:
        with self.connection() as conn:
            row = conn.execute("SELECT km FROM route_cache WHERE key = ?", (key,)).fetchone()
        return float(row["km"]) if row else None

    def route_remember(self, key: str, km: float) -> None:
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO route_cache (key, km, created_at) VALUES (?, ?, ?)",
                         (key, float(km), self.now()))

    def set_outside_delivery(self, order_id: int, fee: int, km: int, km_price: int | None = None) -> None:
        """Авторасчёт: стоимость доставки в заказ и сразу «как обычный» (individual = 2).
        km_price — цена за км на момент расчёта (км × цена — заработок курьера, 28.10.2026)."""
        with self.connection() as conn:
            conn.execute(
                """UPDATE orders SET total = total - delivery_fee + ?, delivery_fee = ?, delivery_km = ?,
                       delivery_km_price = ?, individual = 2, updated_at = ? WHERE id = ? AND individual = 1 AND status = 'new'""",
                (int(fee), int(fee), int(km), int(km_price) if km_price is not None else None, self.now(), order_id))

    def get_floor_lift_price(self) -> int:
        """Цена услуги «Подъём на этаж», ₽ (фиксированная). 0 — услуга выключена."""
        with self.connection() as conn:
            row = conn.execute("SELECT floor_lift_price FROM shop_settings WHERE id = 1").fetchone()
        return int(row["floor_lift_price"] or 0) if row else 0

    def set_floor_lift_price(self, price: int) -> int:
        price = max(0, int(price))
        with self.connection() as conn:
            conn.execute("UPDATE shop_settings SET floor_lift_price = ? WHERE id = 1", (price,))
        return price

    def get_theme(self) -> dict[str, Any]:
        """Mini App visual theme, set by the admin on the "Разработчик"
        screen. Falls back to DEFAULT_THEME if the row is somehow missing
        or holds invalid JSON, so a corrupt row never breaks every screen
        of the app at once."""
        with self.connection() as conn:
            row = conn.execute("SELECT theme_json FROM app_theme WHERE id = 1").fetchone()
        if not row:
            return dict(DEFAULT_THEME)
        try:
            theme = json.loads(row["theme_json"])
        except (json.JSONDecodeError, TypeError):
            return dict(DEFAULT_THEME)
        merged = dict(DEFAULT_THEME)
        merged.update(theme)
        return merged

    @staticmethod
    def _dev_hash(password: str, salt: str) -> str:
        import hashlib
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()

    def dev_password_is_set(self) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM dev_access WHERE id = 1").fetchone() is not None

    def dev_password_set(self, password: str) -> None:
        import secrets as _secrets
        salt = _secrets.token_hex(16)
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO dev_access (id, salt, pw_hash, updated_at) VALUES (1, ?, ?, ?)",
                         (salt, self._dev_hash(password, salt), self.now()))

    def dev_password_check(self, password: str) -> bool:
        import hmac
        with self.connection() as conn:
            row = conn.execute("SELECT salt, pw_hash FROM dev_access WHERE id = 1").fetchone()
        if row is None:
            return False
        return hmac.compare_digest(self._dev_hash(str(password or ""), row["salt"]), row["pw_hash"])

    def set_theme(self, theme: dict[str, Any]) -> dict[str, Any]:
        """Overwrite the Mini App theme. Missing keys fall back to the
        current defaults rather than being dropped, so a partial update
        from the editor (e.g. only the accent color changed) can't
        accidentally blank out the rest of the theme."""
        merged = dict(DEFAULT_THEME)
        merged.update(self.get_theme())
        merged.update(theme)
        with self.connection() as conn:
            conn.execute(
                "UPDATE app_theme SET theme_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(merged, ensure_ascii=False), self.now()),
            )
        return merged

    def get_purchase_counts(self) -> dict[int, int]:
        """Completed (delivered) order count per customer, for the admin
        purchase-count column. Orders still in progress or cancelled
        don't count as purchases."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT user_id, COUNT(*) AS cnt FROM orders WHERE status = 'delivered' "
                "GROUP BY user_id"
            ).fetchall()
            return {int(row["user_id"]): int(row["cnt"]) for row in rows}

    def get_user_purchase_count(self, telegram_id: int) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE user_id = ? AND status = 'delivered'",
                (telegram_id,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_products(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        with self.connection() as conn:
            query = "SELECT * FROM products"
            if not include_inactive:
                query += " WHERE is_active = 1"
            return list(conn.execute(query + " ORDER BY sort_order, id").fetchall())

    def get_archived_products(self) -> list[sqlite3.Row]:
        """Products snapped off sale (soft-deleted) — kept around because
        they appear in past orders' history."""
        with self.connection() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM products WHERE is_active = 0 ORDER BY sort_order, id"
                ).fetchall()
            )

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()

    def add_product(
        self,
        name: str,
        description: str,
        price: int,
        price_exchange: int | None,
        bonus_text: str | None,
        photo_file_id: str | None,
    ) -> int:
        """price_exchange may be None: the product then has no "exchange"
        option at all (e.g. balloons, mufflers, promo items) — only the
        "new" price applies. bonus_text may be None if there's no bonus.
        Stock/inventory tracking was removed — a product is always
        available for order regardless of any physical stock count."""
        now = self.now()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO products
                    (name, description, price, price_exchange, bonus_text, photo_file_id, created_at, updated_at, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM products))
                """,
                (name, description, price, price_exchange, bonus_text, photo_file_id, now, now),
            )
            return int(cursor.lastrowid)

    def reorder_products(self, ids: list[int]) -> None:
        """Новый порядок товаров в каталоге (перетаскивание в админке): ids — сверху вниз.
        Товары не из списка (архив) остаются после них в прежнем порядке."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rest = [r["id"] for r in conn.execute("SELECT id FROM products ORDER BY sort_order, id").fetchall()
                        if r["id"] not in set(ids)]
                for i, pid in enumerate(list(ids) + rest):
                    conn.execute("UPDATE products SET sort_order = ? WHERE id = ?", (i + 1, int(pid)))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_product_prices(
        self, product_id: int, price: int, price_exchange: int | None
    ) -> int:
        """price_exchange may be None to remove the "exchange" option from
        a product entirely (only the "new" price remains). When it's
        removed this way, any "exchange" cart rows for this product are
        dropped too — otherwise their price would become unresolvable
        (NULL) the next time someone opens their cart or checks out.
        Returns how many such cart rows were removed (0 in all other cases)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE products SET price = ?, price_exchange = ?, updated_at = ? WHERE id = ?",
                    (price, price_exchange, self.now(), product_id),
                )
                removed_cart_rows = 0
                if price_exchange is None:
                    cursor = conn.execute(
                        "DELETE FROM carts WHERE product_id = ? AND variant = 'exchange'",
                        (product_id,),
                    )
                    removed_cart_rows = int(cursor.rowcount)
                conn.execute("COMMIT")
                return removed_cart_rows
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def update_product_bonus(self, product_id: int, bonus_text: str | None) -> None:
        """Старый текстовый бонус (бот). С 28.10.2026 бонус — только товар-подарок (настраивается в приложении),
        поэтому здесь можно только убрать бонус."""
        if not bonus_text:
            self.set_product_bonus(product_id, None, 1, None, True, True)

    @staticmethod
    def gift_label(gift_name: str, qty: int) -> str:
        return f"В подарок: {gift_name}" + (f" × {int(qty)}" if int(qty) > 1 else "")

    def set_product_bonus(self, product_id: int, gift_id: int | None, qty: int, bonus_desc: str | None,
                          for_new: bool, for_exchange: bool) -> None:
        """Бонус товара (28.10.2026): товар-подарок из каталога gift_id, qty штук на каждую штуку товара,
        описание, к «Новому» / к «Замене». gift_id None — бонуса нет.
        bonus_text хранит готовую подпись «В подарок: …» (обновляется при переименовании подарка)."""
        label = None
        if gift_id:
            gift = self.get_product(int(gift_id))
            if not gift:
                raise ValueError("Товар-подарок не найден.")
            label = self.gift_label(gift["name"], qty)
        else:
            gift_id, bonus_desc, qty, for_new, for_exchange = None, None, 1, True, True
        with self.connection() as conn:
            conn.execute(
                "UPDATE products SET bonus_product_id = ?, bonus_qty = ?, bonus_text = ?, bonus_desc = ?, bonus_new = ?, "
                "bonus_exchange = ?, updated_at = ? WHERE id = ?",
                (gift_id, max(1, int(qty)), label, bonus_desc or None, 1 if for_new else 0, 1 if for_exchange else 0,
                 self.now(), product_id),
            )

    def _refresh_gift_labels(self, conn, gift_id: int) -> None:
        g = conn.execute("SELECT name FROM products WHERE id = ?", (gift_id,)).fetchone()
        for r in conn.execute("SELECT id, bonus_qty FROM products WHERE bonus_product_id = ?", (gift_id,)).fetchall():
            conn.execute("UPDATE products SET bonus_text = ? WHERE id = ?",
                         (self.gift_label(g["name"], r["bonus_qty"]) if g else None, r["id"]))

    def update_product_name(self, product_id: int, name: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE products SET name = ?, updated_at = ? WHERE id = ?",
                (name, self.now(), product_id),
            )
            self._refresh_gift_labels(conn, product_id)

    def update_product_description(self, product_id: int, description: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE products SET description = ?, updated_at = ? WHERE id = ?",
                (description, self.now(), product_id),
            )

    def update_product_photo(self, product_id: int, photo_file_id: str | None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE products SET photo_file_id = ?, updated_at = ? WHERE id = ?",
                (photo_file_id, self.now(), product_id),
            )

    def update_product_webapp_photo(self, product_id: int, data_url: str | None) -> None:
        """Set/clear the Mini App-only product image (see the
        webapp_photo_data_url migration note above for why this is a
        separate column from photo_file_id)."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE products SET webapp_photo_data_url = ?, updated_at = ? WHERE id = ?",
                (data_url, self.now(), product_id),
            )

    def delete_product(self, product_id: int) -> bool:
        """Soft-delete a product: hide it from the catalog (sends it to the
        archive) without breaking historical orders that still reference it,
        and drop it from every customer's cart so a hidden product never
        lingers there waiting to be bought."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    """
                    UPDATE products SET is_active = 0, updated_at = ?
                    WHERE id = ? AND is_active = 1
                    """,
                    (self.now(), product_id),
                )
                if updated.rowcount == 1:
                    conn.execute("DELETE FROM carts WHERE product_id = ?", (product_id,))
                conn.execute("COMMIT")
                return updated.rowcount == 1
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def restore_product(self, product_id: int) -> bool:
        """Undo a soft delete: bring a hidden product back into the catalog."""
        with self.connection() as conn:
            updated = conn.execute(
                """
                UPDATE products SET is_active = 1, updated_at = ?
                WHERE id = ? AND is_active = 0
                """,
                (self.now(), product_id),
            )
            return updated.rowcount == 1

    def hard_delete_product(self, product_id: int) -> bool:
        """Permanently remove a product from the database.

        Always allowed, even if the product appears in past orders: each
        order_item already carries its own saved copy of the product's name,
        price and quantity, so deleting the product here does not change
        what anyone's order history shows — it just clears the now-unneeded
        link (order_items.product_id becomes NULL for those rows).
        Safe to call on a product still sitting in someone's cart: the
        cart row is dropped along with the product.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                product = conn.execute(
                    "SELECT id FROM products WHERE id = ?", (product_id,)
                ).fetchone()
                if not product:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("DELETE FROM carts WHERE product_id = ?", (product_id,))
                conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
                conn.execute("UPDATE products SET bonus_product_id = NULL, bonus_text = NULL, bonus_desc = NULL "
                             "WHERE bonus_product_id = ?", (product_id,))
                conn.execute("COMMIT")
                return True
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def wipe_all_products(self) -> int:
        """Permanently delete every product that currently exists — active
        or archived — and every cart item referencing them.

        Order history is untouched: each order_item already carries its own
        saved copy of the product's name, price, quantity and bonus text,
        so deleting the live products table only clears the now-unneeded
        link (order_items.product_id becomes NULL for all past orders).
        Couriers, admins, customers and existing orders are not affected.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                conn.execute("DELETE FROM carts")
                conn.execute("DELETE FROM products")
                conn.execute("COMMIT")
                return int(count)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def wipe_all_order_history(self) -> int:
        """Permanently delete every order that currently exists, for every
        customer, courier and admin.

        order_items, courier_offers, courier_ratings and
        courier_location_broadcasts all reference orders.id with
        ON DELETE CASCADE, so deleting from orders clears them too.
        Also clears each user's saved "last delivery info" (name/phone/
        address), since that's a copy of order history kept for reuse at
        checkout. Products, couriers, customers and admins themselves are
        untouched — only the order history disappears. Courier rating_sum/
        rating_count counters on the couriers table are deliberately left
        as-is even though the individual ratings behind them are gone.

        Also resets orders' AUTOINCREMENT counter, so the next order placed
        after a wipe is numbered #1 again instead of continuing from the
        highest order id that ever existed.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                # Сначала — в архив продаж (для выгрузки в Excel), затем удаление.
                wipe_id = conn.execute(
                    "INSERT INTO history_wipes (wiped_at, orders_deleted) VALUES (?, ?)",
                    (self.now(), int(count)),
                ).lastrowid
                qr_by_id = self._qr_map(conn)
                who = """o.user_id, o.customer_name, o.customer_phone, u.username AS customer_username,
                         o.delivery_address, o.courier_id, c.name AS courier_name,
                         (SELECT rating FROM courier_ratings WHERE order_id = o.id) AS courier_rating,
                         (SELECT rating FROM customer_reviews WHERE order_id = o.id AND rating IS NOT NULL) AS customer_rating,
                         o.floor_lift_floors, o.updated_at AS order_closed_at,
                         (SELECT amount FROM customer_bonuses WHERE order_id = o.id AND kind = 'accrual') AS bonus_accrued"""
                rows = conn.execute(
                    f"""
                    SELECT o.id, o.status, o.created_at,
                           i.product_name || CASE WHEN i.is_gift = 1 THEN ' (подарок)' WHEN i.variant = 'exchange' THEN ' (замена)' ELSE '' END AS item_name,
                           i.quantity, i.quantity * i.unit_price AS line_total,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM order_items i JOIN orders o ON o.id = i.order_id
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.kind != 'return'
                    UNION ALL
                    SELECT o.id, o.status, o.created_at, 'Подъём на этаж (услуга)', 1,
                           o.floor_lift_floors * o.floor_lift_price,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM orders o
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.floor_lift_floors > 0
                    UNION ALL
                    SELECT o.id, o.status, o.created_at, 'Спуск пустого баллона (услуга)', 1,
                           o.descent_count * o.descent_price,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM orders o
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.kind != 'return' AND o.descent_count > 0
                    UNION ALL
                    SELECT o.id, o.status, o.created_at, 'Доставка за пределы Москвы', 1, o.delivery_fee,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM orders o
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.delivery_fee > 0
                    UNION ALL
                    SELECT o.id, o.status, o.created_at, 'Оплата бонусами', 1, -o.bonus_spent,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM orders o
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.bonus_spent > 0
                    UNION ALL
                    SELECT o.id, o.status, o.created_at, 'Оплата баллами', 1, -o.points_spent,
                           o.payment_method, o.crypto_quotes, o.usdt_rate, o.payment_qr_id, o.paid_qr_id, {who}
                    FROM orders o
                    LEFT JOIN users u ON u.telegram_id = o.user_id LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.points_spent > 0
                    """
                ).fetchall()
                archive = []
                for r in rows:
                    pay = self._payment_info(r, qr_by_id)
                    archive.append((
                        wipe_id, r["id"], r["status"], r["created_at"], r["item_name"], r["quantity"], r["line_total"],
                        pay["payment_method"], pay["crypto_currency"], pay["crypto_network"], pay["crypto_rate"],
                        r["user_id"], r["customer_name"], r["customer_phone"], r["customer_username"],
                        r["delivery_address"], r["courier_id"], r["courier_name"], r["courier_rating"], r["customer_rating"],
                        r["floor_lift_floors"], r["order_closed_at"], r["bonus_accrued"],
                    ))
                conn.executemany(
                    """
                    INSERT INTO sales_archive
                        (wipe_id, order_no, order_status, order_created_at, item_name, quantity, line_total,
                         payment_method, crypto_currency, crypto_network, crypto_rate,
                         user_id, customer_name, customer_phone, customer_username, delivery_address,
                         courier_id, courier_name, courier_rating, customer_rating, floor_lift_floors, order_closed_at,
                         bonus_accrued)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    archive,
                )
                # Баллы партнёров не трогаем, но отвязываем от номеров заказов:
                # после сброса нумерация начнётся с №1.
                conn.execute(
                    """UPDATE partner_points
                       SET reason = COALESCE(reason, 'заказ №' || order_id || ' (до сброса истории)'), order_id = NULL
                       WHERE order_id IS NOT NULL"""
                )
                # Бонусы покупателей тоже остаются, номер заказа — в причине.
                conn.execute(
                    """UPDATE customer_bonuses
                       SET reason = COALESCE(reason, 'заказ №' || order_id || ' (до сброса истории)'), order_id = NULL
                       WHERE order_id IS NOT NULL"""
                )
                # Журнал курьеров остаётся; номер заказа — в данных записи (нумерация начнётся с №1).
                conn.execute(
                    """UPDATE courier_events SET data = json_set(COALESCE(data, '{}'), '$.order_no', order_id),
                           order_id = NULL WHERE order_id IS NOT NULL""")
                # Чаты курьеров с покупателями остаются для Excel, но больше не привязаны к номерам
                # (нумерация начнётся с №1, старая переписка не должна попасть в новый заказ).
                conn.execute("UPDATE order_messages SET archived = 1 WHERE archived = 0")
                conn.execute("DELETE FROM orders")
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")
                conn.execute(
                    "UPDATE users SET last_customer_name = NULL, "
                    "last_phone = NULL, last_address = NULL"
                )
                conn.execute("COMMIT")
                return int(count)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    DELETABLE_STATUSES = ("delivered", "cancelled", "not_delivered", "disputed_closed")

    def delete_history_orders(self, order_ids: list[int]) -> dict[str, Any]:
        """Удалить закрытые заказы из истории (01.11.2026) — как будто их не было. Удаляются: заказ, состав, возвраты,
        оценки, переписка курьера с покупателем и фото (пропадают у покупателя, курьера и из Excel), а также ВСЕ
        движения денег и баллов по заказу: записи в журнале курьера (заработал / собрано / отдано за пустые /
        оставил себе / предоплата), бонусы покупателя (начисление, списание, возврат баллона) и баллы партнёра.
        Остатки в машине и пустые баллоны считаются по заказам — после удаления тоже возвращаются.
        Не удаляются заказы в работе и спорные. Возвращает {"deleted": [id…], "skipped": [{"id", "reason"}…]}."""
        ids = sorted({int(i) for i in order_ids})
        deleted: list[int] = []
        skipped: list[dict[str, Any]] = []
        with self.connection() as conn:
            try:
                conn.execute("BEGIN")
                for oid in ids:
                    o = conn.execute("SELECT id, status FROM orders WHERE id = ?", (oid,)).fetchone()
                    if o is None:
                        skipped.append({"id": oid, "reason": "заказа нет"})
                        continue
                    if o["status"] not in self.DELETABLE_STATUSES:
                        skipped.append({"id": oid, "reason": "заказ не закрыт (в работе или спорный)"})
                        continue
                    conn.execute("DELETE FROM partner_points WHERE order_id = ?", (oid,))
                    conn.execute("DELETE FROM customer_bonuses WHERE order_id = ?", (oid,))
                    conn.execute("DELETE FROM courier_events WHERE order_id = ?", (oid,))
                    conn.execute("UPDATE support_messages SET order_id = NULL WHERE order_id = ?", (oid,))
                    conn.execute("DELETE FROM order_message_photos WHERE message_id IN (SELECT id FROM order_messages WHERE order_id = ?)", (oid,))
                    conn.execute("DELETE FROM order_messages WHERE order_id = ?", (oid,))
                    conn.execute("DELETE FROM orders WHERE id = ?", (oid,))
                    deleted.append(oid)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return {"deleted": deleted, "skipped": skipped}

    def get_cart(self, user_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT c.product_id, c.variant, c.quantity, p.name, p.description,
                           p.photo_file_id, CASE WHEN g.id IS NULL OR (c.variant = 'exchange' AND p.bonus_exchange = 0) OR (c.variant <> 'exchange' AND p.bonus_new = 0)
                                THEN NULL ELSE p.bonus_text END AS bonus_text,
                           CASE WHEN g.id IS NULL OR (c.variant = 'exchange' AND p.bonus_exchange = 0) OR (c.variant <> 'exchange' AND p.bonus_new = 0)
                                THEN NULL ELSE g.id END AS gift_id, g.name AS gift_name, p.bonus_qty,
                           CASE WHEN g.id IS NULL OR (c.variant = 'exchange' AND p.bonus_exchange = 0) OR (c.variant <> 'exchange' AND p.bonus_new = 0)
                                THEN NULL ELSE p.bonus_desc END AS bonus_desc,
                           CASE WHEN c.variant = 'exchange' THEN p.price_exchange ELSE p.price END
                               AS price,
                           (c.quantity *
                            CASE WHEN c.variant = 'exchange' THEN p.price_exchange ELSE p.price END)
                               AS line_total
                    FROM carts c
                    JOIN products p ON p.id = c.product_id
                    LEFT JOIN products g ON g.id = p.bonus_product_id AND g.is_active = 1 AND g.id <> p.id
                    WHERE c.user_id = ?
                    ORDER BY c.product_id, c.variant
                    """,
                    (user_id,),
                ).fetchall()
            )

    def set_cart_item(self, user_id: int, product_id: int, variant: str, quantity: int) -> None:
        if variant not in ("new", "exchange"):
            raise ValueError("Unknown cart item variant")
        with self.connection() as conn:
            product = conn.execute(
                "SELECT price_exchange, is_active FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if not product or not product["is_active"]:
                raise ValueError("Товар больше недоступен.")
            if variant == "exchange" and product["price_exchange"] is None:
                raise ValueError("Для этого товара нет варианта «Замена».")
            conn.execute(
                """
                INSERT INTO carts (user_id, product_id, variant, quantity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, product_id, variant) DO UPDATE SET
                    quantity = excluded.quantity,
                    updated_at = excluded.updated_at
                """,
                (user_id, product_id, variant, quantity, self.now()),
            )

    def remove_cart_item(self, user_id: int, product_id: int, variant: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM carts WHERE user_id = ? AND product_id = ? AND variant = ?",
                (user_id, product_id, variant),
            )

    def clear_all_carts(self) -> tuple[int, int]:
        """Empty every customer's cart (does not touch stock — adding a product
        to a cart never reserves stock in this bot, only placing an order does,
        so stock figures stay exactly as they already were).

        Returns (rows_removed, distinct_customers_affected).
        """
        with self.connection() as conn:
            customers = int(
                conn.execute("SELECT COUNT(DISTINCT user_id) FROM carts").fetchone()[0]
            )
            cursor = conn.execute("DELETE FROM carts")
            return int(cursor.rowcount), customers

    def clear_user_cart(self, user_id: int) -> None:
        """Customer's own "Очистить корзину" button."""
        with self.connection() as conn:
            conn.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))

    def clear_cart(self, conn: sqlite3.Connection, user_id: int) -> None:
        conn.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))

    def create_order_from_cart(
        self,
        user_id: int,
        customer_name: str,
        customer_phone: str,
        delivery_address: str,
        location_lat: float | None = None,
        location_lon: float | None = None,
        payment_method: str = "cash_on_delivery",
        floor_lift_floors: int = 0,
        bonus_spend: int = 0,
        individual: bool = False,
        points_spend: int = 0,
        return_items: list[tuple[int, int]] | None = None,
        return_mode: str = "cash",
    ) -> int:
        """Спуск пустого баллона в обычном заказе убран (владелец 28.10.2026, 17:07) — только в «Возврате баллона».
        bonus_spend — сколько бонусов покупатель списывает в счёт товаров
        (1 бонус = 1 ₽). Не больше баланса и не больше суммы товаров минус 1 ₽;
        иначе BonusError. Списание — в той же транзакции, что и заказ."""
        bonus_spend = max(0, int(bonus_spend or 0))
        points_spend = max(0, int(points_spend or 0))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cart = conn.execute(
                    """
                    SELECT c.product_id, c.variant, c.quantity, p.name,
                           CASE WHEN g.id IS NULL OR (c.variant = 'exchange' AND p.bonus_exchange = 0) OR (c.variant <> 'exchange' AND p.bonus_new = 0)
                                THEN NULL ELSE p.bonus_text END AS bonus_text,
                           CASE WHEN g.id IS NULL OR (c.variant = 'exchange' AND p.bonus_exchange = 0) OR (c.variant <> 'exchange' AND p.bonus_new = 0)
                                THEN NULL ELSE g.id END AS gift_id, g.name AS gift_name, p.bonus_qty,
                           CASE WHEN c.variant = 'exchange' THEN p.price_exchange ELSE p.price END
                               AS price
                    FROM carts c
                    JOIN products p ON p.id = c.product_id
                    LEFT JOIN products g ON g.id = p.bonus_product_id AND g.is_active = 1 AND g.id <> p.id
                    WHERE c.user_id = ? AND p.is_active = 1
                    ORDER BY c.product_id, c.variant
                    """,
                    (user_id,),
                ).fetchall()
                if not cart:
                    raise ValueError("Корзина пуста.")

                # No stock/inventory check here by design — quantity is
                # never limited against a physical stock count.
                total = 0
                for item in cart:
                    total += int(item["quantity"]) * int(item["price"])

                # Подъём на этаж: цена берётся из настроек в той же транзакции.
                # Если услуга выключена (цена 0), этажи не учитываются.
                lift_floors = max(0, int(floor_lift_floors or 0))
                lift_price = 0
                if lift_floors:
                    row = conn.execute(
                        "SELECT floor_lift_price FROM shop_settings WHERE id = 1"
                    ).fetchone()
                    lift_price = int(row["floor_lift_price"] or 0) if row else 0
                    if lift_price <= 0:
                        lift_floors = lift_price = 0
                total += lift_floors * lift_price

                items_total = total - lift_floors * lift_price
                if bonus_spend:
                    balance = self._bonus_balance(conn, user_id)
                    if bonus_spend > balance:
                        raise BonusError("balance", balance)
                    if bonus_spend > items_total - 1:
                        raise BonusError("too_much", items_total - 1)
                    total -= bonus_spend
                if points_spend:
                    # Партнёрские баллы (1 балл = 1 ₽): только за товары, вместе с бонусами
                    # хотя бы 1 ₽ остаётся к оплате деньгами.
                    partner = conn.execute(
                        "SELECT 1 FROM referral_requests WHERE user_id = ? AND kind = 'partner' AND status = 'approved'",
                        (user_id,)).fetchone()
                    pbal = int(conn.execute("SELECT COALESCE(SUM(points), 0) FROM partner_points WHERE partner_id = ?",
                                            (user_id,)).fetchone()[0])
                    if not partner or points_spend > pbal:
                        raise BonusError("points_balance", pbal if partner else 0)
                    if points_spend > items_total - 1 - bonus_spend:
                        raise BonusError("points_too_much", max(0, items_total - 1 - bonus_spend))
                    total -= points_spend

                # 01.11.2026: возврат пустых баллонов в этом же заказе. Наличные — зачёт: клиент платит меньше
                # (курьер ничего не отдаёт); баллы — зачисляются клиенту при доставке.
                ret_rows, ret_sum = self._resolve_returns(conn, return_items or [], return_mode)
                ret_payout, ret_points = (0, ret_sum) if return_mode == "points" else (ret_sum, 0)
                if ret_payout:
                    if ret_payout > total - 1:
                        raise ValueError(f"Возврат на {ret_payout} ₽ больше суммы покупки. Выберите возврат баллами "
                                         f"или оформите возврат отдельным заказом.")
                    total -= ret_payout
                created_at = self.now()
                order_cursor = conn.execute(
                    """
                    INSERT INTO orders
                        (user_id, customer_name, customer_phone, delivery_address,
                         payment_method, total, location_lat, location_lon,
                         floor_lift_floors, floor_lift_price, bonus_spent, individual,
                         created_at, updated_at, points_spent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        customer_name,
                        customer_phone,
                        delivery_address,
                        payment_method,
                        total,
                        location_lat,
                        location_lon,
                        lift_floors,
                        lift_price,
                        bonus_spend,
                        1 if individual else 0,
                        created_at,
                        created_at,
                        points_spend,
                    ),
                )
                order_id = int(order_cursor.lastrowid)
                if ret_rows:
                    conn.execute("UPDATE orders SET return_mode = ?, return_payout = ?, return_points = ? WHERE id = ?",
                                 ("points" if return_mode == "points" else "cash", ret_payout, ret_points, order_id))
                    conn.executemany(
                        "INSERT INTO order_returns (order_id, product_id, product_name, unit_price, quantity) VALUES (?, ?, ?, ?, ?)",
                        [(order_id, int(p["id"]), p["name"], pr, q) for p, q, pr in ret_rows])
                if points_spend:
                    conn.execute(
                        """INSERT INTO partner_points (partner_id, order_id, points, kind, reason, created_at)
                           VALUES (?, ?, ?, 'spend', 'оплата товаров', ?)""",
                        (user_id, order_id, -points_spend, created_at),
                    )
                if bonus_spend:
                    conn.execute(
                        """INSERT INTO customer_bonuses (user_id, order_id, amount, kind, created_at)
                           VALUES (?, ?, ?, 'spend', ?)""",
                        (user_id, order_id, -bonus_spend, created_at),
                    )
                conn.executemany(
                    """
                    INSERT INTO order_items
                        (order_id, product_id, product_name, unit_price, quantity, variant, bonus_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            order_id,
                            int(item["product_id"]),
                            item["name"],
                            int(item["price"]),
                            int(item["quantity"]),
                            item["variant"],
                            item["bonus_text"],
                        )
                        for item in cart
                    ],
                )
                # 🎁 Подарки (28.10.2026): за каждую штуку товара с бонусом — bonus_qty штук товара-подарка,
                # отдельной строкой за 0 ₽; списываются с остатка курьера как обычный товар.
                gifts: dict[int, list] = {}
                for item in cart:
                    if item["gift_id"] is not None:
                        g = gifts.setdefault(int(item["gift_id"]), [item["gift_name"], 0])
                        g[1] += int(item["quantity"]) * max(1, int(item["bonus_qty"] or 1))
                if gifts:
                    conn.executemany(
                        """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, variant, bonus_text, is_gift)
                           VALUES (?, ?, ?, 0, ?, 'new', NULL, 1)""",
                        [(order_id, gid, name, qty) for gid, (name, qty) in gifts.items()])
                self.clear_cart(conn, user_id)
                conn.execute("COMMIT")
                return order_id
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT o.*, u.telegram_id, u.username,
                       c.name AS courier_name, c.phone AS courier_phone,
                       c.telegram_id AS courier_telegram_id,
                       c.username AS courier_username,
                       c.rating_sum AS courier_rating_sum,
                       c.rating_count AS courier_rating_count
                FROM orders o JOIN users u ON u.telegram_id = o.user_id
                LEFT JOIN couriers c ON c.id = o.courier_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()

    def set_order_payment_proof(self, order_id: int, file_id: str) -> bool:
        """Save the customer's payment screenshot. Returns True only for the
        FIRST screenshot of an order — that is the moment the order goes
        out to couriers. A repeat screenshot is stored too (the newest one
        wins) but must not trigger a second courier broadcast."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payment_proof_file_id FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                first = bool(row) and not row["payment_proof_file_id"]
                conn.execute(
                    "UPDATE orders SET payment_proof_file_id = ?, updated_at = ? WHERE id = ?",
                    (file_id, self.now(), order_id),
                )
                conn.execute("COMMIT")
                return first
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def set_order_usdt(self, order_id: int, usdt_amount: float, usdt_rate: float) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET usdt_amount = ?, usdt_rate = ?, updated_at = ? WHERE id = ?",
                (usdt_amount, usdt_rate, self.now(), order_id),
            )

    def get_order_returns(self, order_id: int) -> list[sqlite3.Row]:
        """Пустые баллоны, которые курьер забирает в обычном заказе (покупка + возврат)."""
        with self.connection() as conn:
            return list(conn.execute("SELECT * FROM order_returns WHERE order_id = ? ORDER BY id", (order_id,)).fetchall())

    @staticmethod
    def _resolve_returns(conn: sqlite3.Connection, items: list[tuple], mode: str) -> tuple[list[tuple], int]:
        """Баллоны на возврат → [(товар, штук, цена за штуку в «валюте» способа)], сумма (₽ или баллы).
        Элемент items — (товар, штук) или (товар, штук, своя цена): админ договорился с клиентом об
        индивидуальной сумме выкупа (01.11.2026); None/нет — цена из карточки товара."""
        rows = []
        for it in items:
            product_id, qty = it[0], it[1]
            custom = it[2] if len(it) > 2 else None
            if int(qty) <= 0:
                continue
            p = conn.execute("SELECT * FROM products WHERE id = ?", (int(product_id),)).fetchone()
            if not p or int(p["return_price"] or 0) <= 0 or not is_balloon(p["name"]):
                raise ValueError("Этот баллон на возврат не принимается.")
            if mode == "points" and int(p["return_points"] or 0) <= 0:
                raise ValueError(f"{p['name']} баллами не принимается — выберите наличные.")
            default = int(p["return_points"]) if mode == "points" else int(p["return_price"])
            rows.append((p, int(qty), default if custom is None else max(0, int(custom))))
        return rows, sum(q * pr for _p, q, pr in rows)

    def get_order_items(self, order_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
                ).fetchall()
            )

    def get_user_orders(self, user_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT o.*, c.name AS courier_name, c.phone AS courier_phone,
                           c.telegram_id AS courier_telegram_id,
                           c.username AS courier_username,
                           c.rating_sum AS courier_rating_sum,
                           c.rating_count AS courier_rating_count
                    FROM orders o
                    LEFT JOIN couriers c ON c.id = o.courier_id
                    WHERE o.user_id = ?
                    ORDER BY o.id DESC LIMIT 20
                    """,
                    (user_id,),
                ).fetchall()
            )

    def get_all_orders(self) -> list[sqlite3.Row]:
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
                    ORDER BY o.id DESC LIMIT 50
                    """
                ).fetchall()
            )

    REFERRAL_KINDS = ("partner", "loyalty")

    def add_referral_request(self, user_id: int, kind: str, comment: str | None = None) -> str:
        """created — новая заявка; again — подана снова после отказа на 7 дней;
        exists — уже ждёт решения; waiting — отказ на 7 дней ещё действует;
        forever — отклонена навсегда."""
        if kind not in self.REFERRAL_KINDS:
            raise ValueError("Unknown referral request kind")
        now = self.now()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM referral_requests WHERE user_id = ? AND kind = ?", (user_id, kind)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO referral_requests (user_id, kind, created_at, comment) VALUES (?, ?, ?, ?)",
                    (user_id, kind, now, comment),
                )
                return "created"
            if row["status"] == "declined_forever":
                return "forever"
            if row["status"] in ("declined_temp", "dismissed"):
                if row["status"] == "declined_temp" and row["decline_until"] and row["decline_until"] > now:
                    return "waiting"
                conn.execute(
                    """UPDATE referral_requests SET status = 'pending', created_at = ?,
                           declined_at = NULL, decline_until = NULL, comment = ? WHERE id = ?""",
                    (now, comment, row["id"]),
                )
                return "again"
            return "exists"

    def decline_referral_request(self, request_id: int, mode: str, days: int = 7) -> sqlite3.Row | None:
        """mode: temp — на 7 дней; forever — навсегда; dismiss — «просто отклонить»
        (убрать из списка админа, человек может сразу подать снова)."""
        from datetime import datetime, timedelta, timezone

        status = {"temp": "declined_temp", "forever": "declined_forever", "dismiss": "dismissed"}[mode]
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds") if mode == "temp" else None
        with self.connection() as conn:
            cur = conn.execute(
                """UPDATE referral_requests SET status = ?, declined_at = ?, decline_until = ?
                   WHERE id = ? AND status = 'pending'""",
                (status, self.now(), until, request_id),
            )
            if cur.rowcount != 1:
                return None
            return conn.execute("SELECT * FROM referral_requests WHERE id = ?", (request_id,)).fetchone()

    # ------------------------------------------------------------ партнёрская программа
    def approve_partner(self, request_id: int, reward_new: int, reward_exchange: int) -> sqlite3.Row | None:
        """Одобрить заявку: уникальный код ссылки + баллы за каждый баллон
        (reward_new — за новый, reward_exchange — за баллон на замену)."""
        import secrets

        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM referral_requests WHERE id = ? AND kind = 'partner' AND status = 'pending'", (request_id,)
            ).fetchone()
            if not row:
                return None
            code = None
            for _ in range(50):
                candidate = "p_" + "".join(secrets.choice(alphabet) for _ in range(7))
                if not conn.execute("SELECT 1 FROM referral_requests WHERE partner_code = ?", (candidate,)).fetchone():
                    code = candidate
                    break
            if code is None:
                raise RuntimeError("could not generate a unique partner code")
            conn.execute(
                """UPDATE referral_requests SET status = 'approved', partner_code = ?, reward_points = ?,
                       reward_new = ?, reward_exchange = ?, approved_at = ? WHERE id = ?""",
                (code, int(reward_new), int(reward_new), int(reward_exchange), self.now(), request_id),
            )
            return conn.execute("SELECT * FROM referral_requests WHERE id = ?", (request_id,)).fetchone()

    def bump_partner_link_version(self) -> int:
        """«Выслать всем партнёрам новую ссылку»: у всех появится окно, пока не подтвердят."""
        with self.connection() as conn:
            conn.execute("UPDATE shop_settings SET partner_link_version = partner_link_version + 1 WHERE id = 1")
            return int(conn.execute("SELECT partner_link_version FROM shop_settings WHERE id = 1").fetchone()[0])

    def partner_link_notice(self, user_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT r.link_ack_version AS ack, s.partner_link_version AS ver
                   FROM referral_requests r, shop_settings s
                   WHERE r.user_id = ? AND r.kind = 'partner' AND r.status = 'approved' AND s.id = 1""",
                (user_id,),
            ).fetchone()
        return bool(row and int(row["ver"] or 0) > int(row["ack"] or 0))

    def ack_partner_link(self, user_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE referral_requests SET link_ack_version =
                   (SELECT partner_link_version FROM shop_settings WHERE id = 1)
                   WHERE user_id = ? AND kind = 'partner' AND status = 'approved'""",
                (user_id,),
            )

    def get_partner_clients(self, partner_id: int) -> list[dict[str, Any]]:
        """Покупатели, закреплённые за партнёром: для рассылки им новой ссылки."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT telegram_id, username, first_name, last_name FROM users
                   WHERE referred_by = ? ORDER BY referred_at, telegram_id""",
                (partner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_partner(self, user_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM referral_requests WHERE user_id = ? AND kind = 'partner' AND status = 'approved'",
                (user_id,),
            ).fetchone()

    def get_partner_by_code(self, code: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM referral_requests WHERE partner_code = ? AND status = 'approved'", (code,)
            ).fetchone()

    def set_partner_reward(self, user_id: int, reward_new: int, reward_exchange: int) -> bool:
        with self.connection() as conn:
            cur = conn.execute(
                """UPDATE referral_requests SET reward_points = ?, reward_new = ?, reward_exchange = ?
                   WHERE user_id = ? AND kind = 'partner' AND status = 'approved'""",
                (int(reward_new), int(reward_new), int(reward_exchange), user_id),
            )
        return cur.rowcount == 1

    def bind_referral(self, user_id: int, code: str) -> str:
        """Новый клиент пришёл по ссылке партнёра. bound — закреплён навсегда;
        иначе причина: bad_code | self | already | not_new."""
        partner = self.get_partner_by_code(code)
        if not partner:
            return "bad_code"
        if int(partner["user_id"]) == int(user_id):
            return "self"
        with self.connection() as conn:
            u = conn.execute("SELECT referred_by FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
            if u is None:
                return "not_new"
            if u["referred_by"]:
                return "already"
            had = conn.execute("SELECT 1 FROM orders WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
            had = had or conn.execute("SELECT 1 FROM sales_archive WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
            if had:
                return "not_new"  # уже был клиентом — не «новый»
            conn.execute(
                "UPDATE users SET referred_by = ?, referred_at = ? WHERE telegram_id = ? AND referred_by IS NULL",
                (int(partner["user_id"]), self.now(), user_id),
            )
        return "bound"

    def accrue_partner_points(self, order_id: int) -> int:
        """Заказ доставлен: если покупатель пришёл по ссылке партнёра — партнёру
        начисляются баллы за КАЖДЫЙ баллон в заказе по ставке этого баллона
        (одинаково за новый и за замену; сумма заказа и подъём не важны).
        Не баллоны (шарики и т. п.) баллов не дают. Один раз на заказ."""
        with self.connection() as conn:
            o = conn.execute(
                """SELECT o.id, o.user_id, o.status, o.kind, u.referred_by FROM orders o
                   JOIN users u ON u.telegram_id = o.user_id WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
            if not o or o["status"] != "delivered" or not o["referred_by"] or o["kind"] == "return":
                return 0
            partner = conn.execute(
                "SELECT 1 FROM referral_requests WHERE user_id = ? AND kind = 'partner' AND status = 'approved'",
                (o["referred_by"],),
            ).fetchone()
            if not partner:
                return 0
            personal = {int(r["product_id"]): int(r["points"]) for r in conn.execute(
                "SELECT product_id, points FROM partner_rewards WHERE partner_id = ?", (int(o["referred_by"]),))}
            items = conn.execute(
                """SELECT oi.product_id, oi.product_name, oi.quantity, oi.unit_price, p.name AS current_name, p.partner_reward
                   FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id
                   WHERE oi.order_id = ? AND oi.is_gift = 0 ORDER BY oi.id""",
                (order_id,),
            ).fetchall()
            per_name: dict[str, list[int]] = {}  # название → [штук, баллов за штуку]
            for it in items:
                name = it["current_name"] or it["product_name"]
                if it["product_id"] is not None and int(it["product_id"]) in personal and is_balloon(name):
                    rate = personal[int(it["product_id"])]  # личная программа партнёра
                else:
                    rate = partner_reward_for(name, it["partner_reward"])
                if rate <= 0:
                    continue
                entry = per_name.setdefault(it["product_name"], [0, rate])
                entry[0] += int(it["quantity"])
            points = sum(qty * rate for qty, rate in per_name.values())
            if points <= 0:
                return 0
            reason = " + ".join(f"{name}: {qty} × {rate}" for name, (qty, rate) in per_name.items())
            items_total = sum(int(it["quantity"]) * int(it["unit_price"]) for it in items)
            cur = conn.execute(
                """INSERT OR IGNORE INTO partner_points
                       (partner_id, order_id, referee_id, points, kind, reason, order_total, created_at)
                   VALUES (?, ?, ?, ?, 'accrual', ?, ?, ?)""",
                (int(o["referred_by"]), order_id, int(o["user_id"]), points, reason, items_total, self.now()),
            )
            return points if cur.rowcount == 1 else 0

    def set_product_return_price(self, product_id: int, price: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE products SET return_price = ?, updated_at = ? WHERE id = ?",
                         (max(0, int(price)), self.now(), product_id))

    def set_product_return_points(self, product_id: int, points: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE products SET return_points = ?, updated_at = ? WHERE id = ?",
                         (max(0, int(points)), self.now(), product_id))

    def return_products(self) -> list[sqlite3.Row]:
        """Баллоны, которые принимаются на возврат (цена возврата > 0), в каталоге."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM products WHERE is_active = 1 AND return_price > 0 ORDER BY id").fetchall()
        return [r for r in rows if is_balloon(r["name"])]

    def create_return_order(self, user_id: int, customer_name: str, customer_phone: str, delivery_address: str,
                            location_lat: float | None, location_lon: float | None,
                            items: list[tuple[int, int]], individual: bool = False, descent_count: int = 0, mode: str = "cash") -> int:
        """«Возврат баллона»: курьер забирает пустые баллоны и отдаёт покупателю их цену возврата.
        К оплате покупателем — 0 ₽; return_payout — сколько курьер отдаёт наличными (за вычетом
        «⬇️ Спуска пустого баллона»: descent_count × ставка курьеру за спуск, не больше числа баллонов)."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = []
                for product_id, qty in items:
                    p = conn.execute("SELECT * FROM products WHERE id = ? AND is_active = 1 AND return_price > 0",
                                     (int(product_id),)).fetchone()
                    if not p or not is_balloon(p["name"]):
                        raise ValueError("Этот баллон на возврат не принимается.")
                    if mode == "points" and int(p["return_points"] or 0) <= 0:
                        raise ValueError(f"{p['name']} баллами не принимается — выберите наличные.")
                    rows.append((p, int(qty)))
                if not rows:
                    raise ValueError("Выберите баллоны для возврата.")
                points_mode = mode == "points"
                # Цена в выбранной «валюте»: наличные (₽) или баллы (1 балл = 1 ₽ при расчёте спуска).
                unit = (lambda p: int(p["return_points"])) if points_mode else (lambda p: int(p["return_price"]))
                payout = sum(unit(p) * q for p, q in rows)
                descent_count = max(0, min(int(descent_count or 0), sum(q for _p, q in rows)))
                row = conn.execute("SELECT courier_rate_descent FROM shop_settings WHERE id = 1").fetchone()
                descent_price = int(row["courier_rate_descent"] or 0) if row else 0
                if not descent_price:
                    descent_count = 0
                payout = max(0, payout - descent_count * descent_price)
                now = self.now()
                return_points, payout = (payout, 0) if points_mode else (0, payout)
                order_id = int(conn.execute(
                    """INSERT INTO orders (user_id, customer_name, customer_phone, delivery_address, payment_method,
                           total, location_lat, location_lon, individual, created_at, updated_at, kind, return_payout,
                           descent_count, descent_price, return_mode, return_points)
                       VALUES (?, ?, ?, ?, 'cash_on_delivery', 0, ?, ?, ?, ?, ?, 'return', ?, ?, ?, ?, ?)""",
                    (user_id, customer_name, customer_phone, delivery_address, location_lat, location_lon,
                     1 if individual else 0, now, now, payout, descent_count, descent_price,
                     "points" if points_mode else "cash", return_points)).lastrowid)
                conn.executemany(
                    # variant в базе — только new/exchange (CHECK); возврат узнаётся по orders.kind.
                    """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, variant, bonus_text)
                       VALUES (?, ?, ?, ?, ?, 'new', NULL)""",
                    [(order_id, int(p["id"]), p["name"], unit(p), q) for p, q in rows])
                conn.execute("COMMIT")
                return order_id
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def admin_edit_order(self, order_id: int, items: list[dict[str, Any]], lift_floors: int | None = None,
                         descent_count: int | None = None, returns: list[dict[str, Any]] | None = None,
                         return_mode: str | None = None) -> dict[str, Any]:
        """Админ меняет состав активного заказа («Новый»/«В работе»/«В пути») — 31.10.2026.
        Обычный заказ: items — [{product_id, variant, quantity}] (0 — убрать), lift_floors — подъёмы.
        Возврат баллона: items — [{product_id, quantity}], descent_count — спуски.
        Цена уже существовавшей позиции остаётся прежней, новой — по текущему каталогу. Бонусы и баллы,
        списанные покупателем, не меняются. Возвращает {"before": …, "after": …} с суммами для сообщения."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if not o or o["status"] not in ("new", "accepted", "in_transit"):
                    raise ValueError("Заказ уже закрыт — состав менять нельзя.")
                o = dict(o)
                is_return = (o.get("kind") or "order") == "return"
                old_items = [dict(r) for r in conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ? AND is_gift = 0 ORDER BY id", (order_id,)).fetchall()]
                wanted: dict[tuple[int, str], int] = {}
                custom_price: dict[tuple[int, str], int] = {}
                for it in items:
                    pid, qty = int(it["product_id"]), int(it.get("quantity") or 0)
                    if it.get("unit_price") is not None and (o.get("kind") or "order") == "return":
                        custom_price[(pid, "new")] = max(0, int(it["unit_price"]))
                    variant = "new" if is_return else (it.get("variant") if it.get("variant") in ("new", "exchange") else "new")
                    if qty < 0 or qty > 99:
                        raise ValueError("Количество — от 0 до 99.")
                    if qty:
                        wanted[(pid, variant)] = wanted.get((pid, variant), 0) + qty
                if not wanted:
                    raise ValueError("В заказе должен остаться хотя бы один товар. Чтобы убрать всё — отмените заказ.")
                old_prices = {(int(r["product_id"]), r["variant"]): int(r["unit_price"]) for r in old_items if r["product_id"] is not None}
                mode = o.get("return_mode") or "cash"
                new_rows = []
                for (pid, variant), qty in wanted.items():
                    p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
                    if not p:
                        raise ValueError("Такого товара нет.")
                    if is_return:
                        if int(p["return_price"] or 0) <= 0 or not is_balloon(p["name"]):
                            raise ValueError(f"{p['name']} на возврат не принимается.")
                        if mode == "points" and int(p["return_points"] or 0) <= 0:
                            raise ValueError(f"{p['name']} баллами не принимается.")
                        # 01.11.2026: своя сумма выкупа (админ) → она; не вписана → цена из карточки товара
                        price = custom_price.get((pid, variant))
                        if price is None:
                            price = int(p["return_points"]) if mode == "points" else int(p["return_price"])
                    else:
                        if (pid, variant) in old_prices:
                            price = old_prices[(pid, variant)]
                        else:
                            if not int(p["is_active"]):
                                raise ValueError(f"{p['name']} снят с продажи.")
                            if variant == "exchange" and p["price_exchange"] is None:
                                raise ValueError(f"{p['name']}: «Замена» недоступна.")
                            price = int(p["price_exchange"] if variant == "exchange" else p["price"])
                    new_rows.append((p, variant, qty, price))
                before = {"total": int(o["total"]), "payout": int(o.get("return_payout") or 0), "points": int(o.get("return_points") or 0)}
                now = self.now()
                if is_return:
                    balloons = sum(q for _p, _v, q, _pr in new_rows)
                    descents = int(o.get("descent_count") or 0) if descent_count is None else max(0, int(descent_count))
                    descent_price = int(o.get("descent_price") or 0)
                    if descents and not descent_price:
                        descent_price = int((conn.execute("SELECT courier_rate_descent FROM shop_settings WHERE id = 1").fetchone() or [0])[0] or 0)
                        if not descent_price:
                            raise ValueError("Спуск выключен: ставка за спуск равна 0.")
                    if descents > balloons:
                        raise ValueError("Спусков не может быть больше, чем баллонов.")
                    gross = sum(pr * q for _p, _v, q, pr in new_rows)
                    net = max(0, gross - descents * descent_price)
                    payout, points = (0, net) if mode == "points" else (net, 0)
                    conn.execute("""UPDATE orders SET return_payout = ?, return_points = ?, descent_count = ?, descent_price = ?,
                                    updated_at = ? WHERE id = ?""", (payout, points, descents, descent_price if descents else 0, now, order_id))
                    after = {"total": 0, "payout": payout, "points": points}
                else:
                    old_sum = sum(int(r["quantity"]) * int(r["unit_price"]) for r in old_items)
                    new_sum = sum(q * pr for _p, _v, q, pr in new_rows)
                    old_lift = int(o.get("floor_lift_floors") or 0) * int(o.get("floor_lift_price") or 0)
                    floors = int(o.get("floor_lift_floors") or 0) if lift_floors is None else max(0, int(lift_floors))
                    lift_price = int(o.get("floor_lift_price") or 0)
                    if floors and not lift_price:
                        lift_price = int((conn.execute("SELECT floor_lift_price FROM shop_settings WHERE id = 1").fetchone() or [0])[0] or 0)
                        if lift_price <= 0:
                            raise ValueError("Подъём на этаж выключен: цена подъёма равна 0.")
                    if floors > 99:
                        raise ValueError("Подъёмов не больше 99.")
                    new_lift = floors * lift_price
                    spent = int(o.get("bonus_spent") or 0) + int(o.get("points_spent") or 0)
                    if spent > new_sum - 1:
                        raise ValueError(f"Сумма товаров не может быть меньше списанных бонусов и баллов ({spent}).")
                    # 01.11.2026: «возврат в этом же заказе» — админ может добавить/убрать/сменить способ.
                    old_payout = int(o.get("return_payout") or 0)
                    old_points = int(o.get("return_points") or 0)
                    new_payout, new_points, mode_out = old_payout, old_points, (o.get("return_mode") or "cash")
                    ret_rows = None
                    if returns is not None:
                        had = conn.execute("SELECT COUNT(*) FROM order_returns WHERE order_id = ?", (order_id,)).fetchone()[0]
                        mode_out = ("points" if (return_mode if (return_mode and not had) else (o.get("return_mode") or "cash")) == "points" else "cash")
                        ret_rows, ret_sum = self._resolve_returns(
                            conn, [(int(r["product_id"]), int(r.get("quantity") or 0), r.get("unit_price")) for r in returns], mode_out)
                        new_payout, new_points = (0, ret_sum) if mode_out == "points" else (ret_sum, 0)
                    total = int(o["total"]) + (new_sum - old_sum) + (new_lift - old_lift) + (old_payout - new_payout)
                    if total < 1:
                        raise ValueError("Итог заказа не может быть меньше 1 ₽: возврат больше суммы покупки. "
                                         "Выберите возврат баллами или уменьшите возврат.")
                    conn.execute("""UPDATE orders SET total = ?, floor_lift_floors = ?, floor_lift_price = ?, updated_at = ?
                                    WHERE id = ?""", (total, floors, lift_price if floors else int(o.get("floor_lift_price") or 0), now, order_id))
                    if ret_rows is not None:
                        conn.execute("UPDATE orders SET return_mode = ?, return_payout = ?, return_points = ? WHERE id = ?",
                                     (mode_out if ret_rows else "cash", new_payout, new_points, order_id))
                        conn.execute("DELETE FROM order_returns WHERE order_id = ?", (order_id,))
                        conn.executemany(
                            "INSERT INTO order_returns (order_id, product_id, product_name, unit_price, quantity) VALUES (?, ?, ?, ?, ?)",
                            [(order_id, int(p["id"]), p["name"], pr, q) for p, q, pr in ret_rows])
                    after = {"total": total, "payout": new_payout, "points": new_points}
                conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
                conn.executemany(
                    """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, variant, bonus_text)
                       VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                    [(order_id, int(p["id"]), p["name"], pr, q, v) for p, v, q, pr in new_rows])
                if not is_return:
                    # 🎁 подарки пересчитываются по новому составу (как при оформлении)
                    gifts: dict[int, list] = {}
                    for p, v, q, pr in new_rows:
                        if p["bonus_product_id"] is None or (v == "exchange" and not p["bonus_exchange"]) or (v != "exchange" and not p["bonus_new"]):
                            continue
                        g = conn.execute("SELECT id, name FROM products WHERE id = ? AND is_active = 1 AND id <> ?",
                                         (int(p["bonus_product_id"]), int(p["id"]))).fetchone()
                        if g:
                            e = gifts.setdefault(int(g["id"]), [g["name"], 0])
                            e[1] += q * max(1, int(p["bonus_qty"] or 1))
                    if gifts:
                        conn.executemany(
                            """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, variant, bonus_text, is_gift)
                               VALUES (?, ?, ?, 0, ?, 'new', NULL, 1)""",
                            [(order_id, gid, name, qty) for gid, (name, qty) in gifts.items()])
                conn.execute("COMMIT")
                return {"before": before, "after": after, "is_return": is_return}
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def set_product_partner_reward(self, product_id: int, reward: int | None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE products SET partner_reward = ?, updated_at = ? WHERE id = ?",
                         (None if reward is None else int(reward), self.now(), product_id))

    def partner_reward_table(self, partner_id: int | None = None, include_zero: bool = False) -> list[dict[str, Any]]:
        """Баллы за 1 баллон по баллонам каталога (активные товары). С partner_id — с учётом
        личной программы партнёра (personal=True там, где у него своя ставка)."""
        personal = self.partner_personal_rewards(partner_id) if partner_id is not None else {}
        out = []
        for p in self.get_products():
            if not is_balloon(p["name"]):
                continue
            general = partner_reward_for(p["name"], p["partner_reward"])
            pts = personal.get(int(p["id"]), general)
            if pts > 0 or include_zero:
                out.append({"product_id": int(p["id"]), "name": p["name"], "points": pts, "general": general,
                            "personal": int(p["id"]) in personal})
        return out

    def partner_personal_rewards(self, partner_id: int) -> dict[int, int]:
        with self.connection() as conn:
            return {int(r["product_id"]): int(r["points"]) for r in conn.execute(
                "SELECT product_id, points FROM partner_rewards WHERE partner_id = ?", (partner_id,))}

    def set_partner_personal_rewards(self, partner_id: int, rewards: dict[int, int | None]) -> None:
        """{product_id: баллы} — своя ставка; None — вернуть общую."""
        with self.connection() as conn:
            for product_id, points in rewards.items():
                if points is None:
                    conn.execute("DELETE FROM partner_rewards WHERE partner_id = ? AND product_id = ?",
                                 (partner_id, int(product_id)))
                else:
                    conn.execute(
                        """INSERT INTO partner_rewards (partner_id, product_id, points) VALUES (?, ?, ?)
                           ON CONFLICT (partner_id, product_id) DO UPDATE SET points = excluded.points""",
                        (partner_id, int(product_id), int(points)))

    def partner_points_balance(self, partner_id: int) -> int:
        with self.connection() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(points), 0) FROM partner_points WHERE partner_id = ?", (partner_id,)
            ).fetchone()[0])

    def write_off_partner_points(self, partner_id: int, points: int, reason: str | None) -> int:
        """Списать баллы (не больше остатка). Возвращает новый остаток."""
        balance = self.partner_points_balance(partner_id)
        if points <= 0 or points > balance:
            raise ValueError("bad amount")
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO partner_points (partner_id, points, kind, reason, created_at)
                   VALUES (?, ?, 'writeoff', ?, ?)""",
                (partner_id, -int(points), reason, self.now()),
            )
        return balance - int(points)

    def get_partner_points_history(self, partner_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT p.*, u.first_name, u.last_name FROM partner_points p
                   LEFT JOIN users u ON u.telegram_id = p.referee_id
                   WHERE p.partner_id = ? ORDER BY p.id DESC LIMIT ?""",
                (partner_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_partners(self) -> list[dict[str, Any]]:
        """Одобренные партнёры: код, баллы за заказ, приглашённые, баланс."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*, u.first_name, u.last_name, u.username, u.last_phone,
                       c.id AS courier_id, c.phone AS courier_phone,
                       (SELECT COUNT(*) FROM users x WHERE x.referred_by = r.user_id) AS invited,
                       (SELECT COUNT(*) FROM partner_points p WHERE p.partner_id = r.user_id AND p.kind = 'accrual') AS rewarded_orders,
                       (SELECT COALESCE(SUM(points), 0) FROM partner_points p WHERE p.partner_id = r.user_id AND p.kind = 'accrual') AS earned,
                       (SELECT COALESCE(-SUM(points), 0) FROM partner_points p WHERE p.partner_id = r.user_id AND p.kind = 'writeoff') AS written_off
                FROM referral_requests r
                JOIN users u ON u.telegram_id = r.user_id
                LEFT JOIN couriers c ON c.telegram_id = r.user_id
                WHERE r.kind = 'partner' AND r.status = 'approved'
                ORDER BY r.approved_at DESC, r.id DESC
                """
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["balance"] = int(d["earned"] or 0) - int(d["written_off"] or 0)
            result.append(d)
        return result

    def get_invited_count(self, partner_id: int) -> int:
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (partner_id,)).fetchone()[0])

    def count_pending_referral_requests(self) -> dict[str, int]:
        """Сколько заявок ждут решения админа — по видам (для счётчиков на главной)."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM referral_requests WHERE status = 'pending' GROUP BY kind"
            ).fetchall()
        counts = {"partner": 0, "loyalty": 0}
        counts.update({r["kind"]: int(r["n"]) for r in rows})
        return counts

    def get_user_referral_requests(self, user_id: int) -> dict[str, dict[str, Any]]:
        """{вид заявки: {status, created_at, decline_until}} — заявки этого человека."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT kind, status, created_at, decline_until FROM referral_requests WHERE user_id = ?", (user_id,)
            ).fetchall()
        now = self.now()
        result = {}
        for r in rows:
            status = r["status"]
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
