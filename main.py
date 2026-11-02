from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import (
    CopyMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendLocation,
    SendMessage,
    SendPhoto,
    SendVenue,
)
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)

try:
    from config import (
        ADMIN_ID,
        ADMIN_IDS,
        BOT_TOKEN,
        DB_PATH,
        LOG_GROUP_CHAT_ID,
        WEBAPP_ENABLED,
        WEBAPP_HOST,
        ORS_API_KEY,
        WEBAPP_PORT,
        WEBAPP_URL,
    )
    from db import Database
except ModuleNotFoundError:
    from .config import (
        ADMIN_ID,
        ADMIN_IDS,
        BOT_TOKEN,
        DB_PATH,
        LOG_GROUP_CHAT_ID,
        WEBAPP_ENABLED,
        WEBAPP_HOST,
        ORS_API_KEY,
        WEBAPP_PORT,
        WEBAPP_URL,
    )
    from .db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

db = Database(DB_PATH)
router = Router()


async def register_user_middleware(handler, event, data):
    """Make sure whoever writes to the bot in a private chat exists in the
    `users` table before any handler runs. Carts and orders reference
    users(telegram_id) with a FOREIGN KEY, and users used to be registered
    only by /start — so anyone who tapped a button without sending /start
    first (e.g. after the database was recreated on a new server, or from
    an old message) got "IntegrityError: FOREIGN KEY constraint failed"
    when adding a product to the cart."""
    user = getattr(event, "from_user", None)
    chat = event.chat if isinstance(event, Message) else (
        event.message.chat if isinstance(event, CallbackQuery) and event.message else None
    )
    if user and not user.is_bot and chat is not None and chat.type == "private":
        try:
            db.upsert_user(user.id, user.username, user.first_name, user.last_name)
        except Exception:
            logger.exception("Could not register user %s", user.id)
    return await handler(event, data)


router.message.outer_middleware(register_user_middleware)
router.callback_query.outer_middleware(register_user_middleware)


STATUS_LABELS = {
    "new": "Активен",
    "accepted": "В работе",
    "in_transit": "В работе",
    "delivered": "Завершён",
    "cancelled": "Отменён",
    "not_delivered": "Не доставлен",
    "disputed": "Спорный (покупатель не выходит на связь)",
    "disputed_closed": "Спорный — закрыт",
}


def msk_datetime(iso: str | None) -> str:
    """«2026-10-25T09:21:20+00:00» → «25.10.2026 12:21» (время по Москве, без секунд)."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso)).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(iso)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def send_to_admins(bot: Bot, method: str, *args, **kwargs):
    """Уведомление всем администраторам (ADMIN_ID — до 20 ID через запятую).
    Ошибка доставки одному админу не мешает остальным; если не дошло ни
    одному — пробрасываем последнюю ошибку (вызывающий код её логирует).
    Возвращает сообщение, отправленное первому админу, до которого дошло."""
    first = None
    last_exc: Exception | None = None
    for admin_id in ADMIN_IDS:
        try:
            result = await getattr(bot, method)(admin_id, *args, **kwargs)
        except Exception as exc:
            last_exc = exc
            logger.warning("Could not deliver %s to admin %s: %r", method, admin_id, exc)
            continue
        if first is None:
            first = result
    if first is None and last_exc is not None:
        raise last_exc
    return first


def menu_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    """Бот — только вход в приложение и уведомления (28.10.2026): одна кнопка «🛒 Магазин»."""
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Магазин", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def item_line(name: str, variant: str | None, quantity: int, unit_price: int) -> str:
    """'• Баллон 2 л (замена) — 3 шт. × 5 000 ₽ = 15 000 ₽' — the line total
    is printed explicitly so it's obvious the quantity is multiplied."""
    label = " (замена)" if variant == "exchange" else ""
    return (
        f"• {escape(name)}{label} — {quantity} шт. × {money(unit_price)}"
        f" = <b>{money(quantity * unit_price)}</b>"
    )


def order_items_text(items) -> str:
    lines = []
    for item in items:
        if "is_gift" in item.keys() and item["is_gift"]:
            lines.append(f"• 🎁 {escape(item['product_name'])} — {int(item['quantity'])} шт. (подарок)")
            continue
        line = item_line(
            item["product_name"],
            item["variant"] if "variant" in item.keys() else None,
            int(item["quantity"]),
            int(item["unit_price"]),
        )
        if "bonus_text" in item.keys() and item["bonus_text"]:
            line += f"\n  🎁 {escape(item['bonus_text'])}"
        lines.append(line)
    return "\n".join(lines)


def delivery_fee_text(order) -> str:
    """Строка «доставка за пределы Москвы» (уже входит в orders.total)."""
    fee = int(order["delivery_fee"] or 0) if "delivery_fee" in order.keys() else 0
    toll = int(order["delivery_toll"] or 0) if "delivery_toll" in order.keys() else 0
    if not fee:
        return ""
    return f"\n🚚 Доставка за пределы Москвы: {money(fee)}" + (f" (в т. ч. платная дорога {money(toll)})" if toll else "")


def bonus_spent_text(order) -> str:
    """Строка «оплачено бонусами» (уже вычтено из orders.total)."""
    spent = int(order["bonus_spent"] or 0) if "bonus_spent" in order.keys() else 0
    points = int(order["points_spent"] or 0) if "points_spent" in order.keys() else 0
    return (f"\n💰 Оплачено бонусами: −{money(spent)}" if spent else "") + (
        f"\n⭐ Оплачено баллами: −{money(points)}" if points else "")


def floor_lift_text(order) -> str:
    """Строки об услугах «Подъём на этаж» и (28.10.2026) «Спуск пустого баллона» к «Замене» — их стоимость
    уже входит в orders.total; без услуг — пустая строка. У возврата спуск пишет return_order_text."""
    keys = order.keys()
    out = ""
    if "floor_lift_floors" in keys and order["floor_lift_floors"]:
        floors = int(order["floor_lift_floors"])
        price = int(order["floor_lift_price"] or 0)
        # 28.10.2026: цена — за подъём одного баллона, floors — сколько подъёмов.
        out += f"\n🏢 Подъём на этаж: {floors} × {money(price)} = {money(floors * price)}"
    is_return = "kind" in keys and order["kind"] == "return"
    if not is_return and "descent_count" in keys and order["descent_count"]:
        n, price = int(order["descent_count"]), int(order["descent_price"] or 0)
        out += f"\n⬇️ Спуск пустого баллона: {n} × {money(price)} = {money(n * price)}"
    return out


def has_exchange_items(items) -> bool:
    return any(item["variant"] == "exchange" for item in items)


def location_text(order) -> str:
    """A copy-paste-friendly coordinates block for the courier's navigator app,
    when the customer shared a location pin instead of a typed address."""
    try:
        lat = order["location_lat"]
        lon = order["location_lon"]
    except (KeyError, IndexError):
        return ""
    if lat is None or lon is None:
        return ""
    maps_link = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"
    return (
        f"\n\n📍 Координаты клиента (нажмите, чтобы скопировать):\n"
        f"<code>{lat:.6f}, {lon:.6f}</code>\n"
        f"Открыть на карте: {maps_link}"
    )


PAYMENT_METHOD_LABELS = {
    "cash_on_delivery": "💵 Наличные курьеру",
    "sbp": "📱 СБП по QR-коду",
    "crypto": "💎 Криптовалюта по QR-коду",
}
PAYMENT_METHODS = ("cash_on_delivery", "sbp", "crypto")


def payment_method_label(method: str) -> str:
    return PAYMENT_METHOD_LABELS.get(method, method)


# ---------------------------------------------------------------- USDT rate
# Live RUB price of 1 USDT from CoinGecko's public "simple price" endpoint
# (no API key; CoinGecko documents a 60-second cache for keyless access).
# Cached here for the same 60 s so the bot doesn't hit the API on every tap.
USDT_RATE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"
USDT_RATE_TTL = 60          # seconds a fetched rate is reused
USDT_RATE_MAX_AGE = 15 * 60  # an older cached rate is never shown to a buyer
_usdt_rate_cache: dict[str, float] = {}


async def fetch_usdt_rub_rate() -> float:
    """One request to the rate API. Raises on any problem."""
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(USDT_RATE_URL) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
    rate = float(data["tether"]["rub"])
    if not 1 <= rate <= 10_000:
        raise ValueError(f"Implausible USDT/RUB rate: {rate}")
    return rate


async def get_usdt_rub_rate() -> float | None:
    """Current RUB per 1 USDT, or None if no fresh-enough rate is available
    (the caller must then not offer a USDT amount at all)."""
    now = time.time()
    cached_at = _usdt_rate_cache.get("at", 0.0)
    if "rate" in _usdt_rate_cache and now - cached_at < USDT_RATE_TTL:
        return _usdt_rate_cache["rate"]
    try:
        rate = await fetch_usdt_rub_rate()
    except Exception:
        logger.exception("Could not fetch USDT/RUB rate")
        if "rate" in _usdt_rate_cache and now - cached_at < USDT_RATE_MAX_AGE:
            return _usdt_rate_cache["rate"]
        return None
    _usdt_rate_cache.update(rate=rate, at=now)
    return rate


def usdt_text(amount) -> str:
    return f"{Decimal(str(amount)):.2f} USDT"


def rate_text(rate: float) -> str:
    return f"{Decimal(str(rate)).quantize(Decimal('0.01'))} ₽"


def order_usdt_suffix(order) -> str:
    """' = 21.35 USDT (курс 1 USDT = 93.70 ₽)' for crypto orders with a fixed rate."""
    keys = order.keys()
    if "usdt_amount" in keys and order["usdt_amount"] is not None and order["usdt_rate"]:
        return f" = <b>{usdt_text(order['usdt_amount'])}</b> (курс 1 USDT = {rate_text(order['usdt_rate'])})"
    return ""


def pay_on_delivery_line(order) -> str:
    """СБП/криптовалюта при курьере (новая схема из Mini App): крупное
    предупреждение и суммы в монетах по курсу на момент оформления."""
    method = order["payment_method"]
    head = "⚠️ <b>Внимание! Оплата через СБП</b>" if method == "sbp" else "⚠️ <b>Внимание! Оплата криптовалютой</b>"
    text = f"{head} — покупатель переводит при курьере по QR-коду из приложения.\nК оплате: <b>{money(int(order['total']))}</b>"
    chosen = None
    if "payment_qr_id" in order.keys() and order["payment_qr_id"]:
        chosen = db.get_payment_qr_code_any(int(order["payment_qr_id"]))
    if method == "crypto":
        import json as _json
        try:
            quotes = _json.loads(order["crypto_quotes"] or "{}")
        except (TypeError, ValueError):
            quotes = {}
        if chosen is not None and chosen["currency"]:
            # Покупатель выбрал конкретную монету и сеть — показываем только её.
            label = chosen["currency"] + (f" · {chosen['network']}" if chosen["network"] else "")
            text += f"\nВалюта покупателя: <b>{escape(label)}</b>"
            quotes = {k: v for k, v in quotes.items() if k == chosen["currency"]}
        for code, q in quotes.items():
            try:
                rate = f"{float(q.get('rate')):,.2f}".replace(",", " ").removesuffix(".00")
            except (TypeError, ValueError):
                rate = "?"
            text += f"\n• {escape(str(q.get('amount')))} {escape(code)} (курс 1 {escape(code)} = {rate} ₽)"
    if "payment_confirmed_at" in order.keys() and order["payment_confirmed_at"]:
        text += "\n✅ Оплата подтверждена курьером (фото в канале)."
    return text


def staff_payment_line(order) -> str:
    """Payment line for the admin and couriers: tells the courier plainly
    whether to collect cash at the door or not."""
    method = order["payment_method"] if "payment_method" in order.keys() else "cash_on_delivery"
    if "pay_on_delivery" in order.keys() and order["pay_on_delivery"] and method != "cash_on_delivery":
        return pay_on_delivery_line(order)
    if method == "cash_on_delivery":
        return f"💵 <b>Оплата: НАЛИЧНЫЕ</b> — получить с клиента {money(int(order['total']))}"
    has_proof = bool(order["payment_proof_file_id"]) if "payment_proof_file_id" in order.keys() else False
    usdt = order_usdt_suffix(order)
    if usdt:
        usdt = f"\nК оплате: {money(int(order['total']))}{usdt}"
    if has_proof:
        return (
            f"✅ <b>Оплата: {payment_method_label(method)}</b> — клиент прислал скриншот оплаты, "
            f"деньги с клиента НЕ брать{usdt}"
        )
    return f"⏳ <b>Оплата: {payment_method_label(method)}</b> — скриншот оплаты ещё не прислан{usdt}"


def deliver_time_text(order) -> str | None:
    """«Доставка ко времени»: время по Москве «18:30» или None."""
    if "deliver_at" not in order.keys() or not order["deliver_at"]:
        return None
    try:
        return datetime.fromisoformat(order["deliver_at"]).astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")
    except ValueError:
        return None


def return_order_text(order, items) -> str:
    """«Возврат баллона»: курьер забирает пустые баллоны и отдаёт покупателю деньги."""
    lines = "\n".join(
        f"• {escape(it['product_name'])} × {int(it['quantity'])} — {money(int(it['unit_price']) * int(it['quantity']))}"
        for it in items)
    keys = order.keys()
    descent = int(order["descent_count"] or 0) if "descent_count" in keys else 0
    descent_line = (f"\n⬇️ Спуск пустого баллона: {descent} × {money(int(order['descent_price'] or 0))} "
                    f"= −{money(descent * int(order['descent_price'] or 0))}" if descent else "")
    return (f"♻️ <b>ВОЗВРАТ БАЛЛОНА</b> — курьер забирает пустые баллоны\n\n{lines}{descent_line}\n\n"
            f"Отдать покупателю наличными: <b>{money(int(order['return_payout'] or 0))}</b>")


def returns_text(order) -> str:
    """Возврат пустых баллонов в обычном заказе (01.11.2026): что забрать и как рассчитаться."""
    try:
        rows = db.get_order_returns(int(order["id"]))
    except Exception:
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"• {escape(r['product_name'])} — {int(r['quantity'])} шт." for r in rows)
    if "return_mode" in order.keys() and order["return_mode"] == "points":
        pay = f"клиенту начислят {int(order['return_points'] or 0)} баллов (наличные не отдавать)"
    else:
        pay = f"зачтено в оплату: −{money(int(order['return_payout'] or 0))}"
    return f"\n\n♻️ <b>Забрать пустые:</b>\n{lines}\n{pay}"


def order_money_block(order, items) -> str:
    """Товары, суммы и оплата для сообщений курьерам и в канал; у возврата баллона — что забрать и сколько отдать."""
    if "kind" in order.keys() and order["kind"] == "return":
        return return_order_text(order, items)
    return (f"{order_items_text(items)}{floor_lift_text(order)}{delivery_fee_text(order)}{bonus_spent_text(order)}{returns_text(order)}\n\n"
            f"Сумма: <b>{money(int(order['total']))}</b>\n{staff_payment_line(order)}")


def plain_name(name) -> str:
    """Имя без «(@логин)» (01.11.2026): Telegram клиента и курьера друг другу не показываем."""
    import re as _re
    return _re.sub(r"\s*\(@[^)]*\)", "", str(name or "")).strip()


def order_text(order, items, include_contacts: bool = False) -> str:
    method = order["payment_method"] if "payment_method" in order.keys() else "cash_on_delivery"
    deliver = deliver_time_text(order)
    if "kind" in order.keys() and order["kind"] == "return":
        text = (
            f"<b>Заказ №{order['id']}</b>\n"
            + (f"🟣 <b>КО ВРЕМЕНИ: К {deliver}</b>\n" if deliver else "")
            + f"Статус: <b>{STATUS_LABELS.get(order['status'], order['status'])}</b>\n\n"
            + return_order_text(order, items)
        )
        keys = order.keys()
        if "customer_comment" in keys and order["customer_comment"]:
            text += f"\n\n💬 Комментарий: {escape(order['customer_comment'])}"
        if include_contacts:
            text += (
                f"\n\nИмя: {escape(plain_name(order['customer_name']))}"
                f"\nТелефон: {escape(order['customer_phone'])}"
                f"\nАдрес: {escape(order['delivery_address'])}"
                f"\nВремя: {escape(msk_datetime(order['created_at']))}"
            )
            text += location_text(order)
        if "courier_name" in keys and order["courier_name"]:
            text += f"\n\nКурьер: {escape(order['courier_name'])}"
        return text
    text = (
        f"<b>Заказ №{order['id']}</b>\n"
        + (f"🟣 <b>ДОСТАВКА КО ВРЕМЕНИ: К {deliver}</b>\n" if deliver else "")
        + f"Статус: <b>{STATUS_LABELS.get(order['status'], order['status'])}</b>\n\n"
        f"{order_items_text(items)}{floor_lift_text(order)}{delivery_fee_text(order)}{bonus_spent_text(order)}{returns_text(order)}\n\n"
        f"Сумма: <b>{money(int(order['total']))}</b>\n"
        + (
            staff_payment_line(order)
            if include_contacts
            else f"Оплата: {payment_method_label(method)}"
            + (
                f"\nК оплате: {money(int(order['total']))}{order_usdt_suffix(order)}"
                if order_usdt_suffix(order)
                else ""
            )
        )
    )
    if has_exchange_items(items):
        text += "\n\n♻️ В заказе есть замена баллона — приготовьте пустой баллон к приезду курьера."
    keys = order.keys()
    if "customer_comment" in keys and order["customer_comment"]:
        text += f"\n\n💬 Комментарий: {escape(order['customer_comment'])}"
    if include_contacts:
        text += (
            f"\n\nИмя: {escape(plain_name(order['customer_name']))}"
            f"\nТелефон: {escape(order['customer_phone'])}"
            f"\nАдрес: {escape(order['delivery_address'])}"
            f"\nВремя: {escape(msk_datetime(order['created_at']))}"
        )
        text += location_text(order)
    if "courier_name" in order.keys() and order["courier_name"]:
        text += f"\n\nКурьер: {escape(order['courier_name'])}"
        if include_contacts and order["courier_phone"]:
            text += f"\nТелефон курьера: {escape(order['courier_phone'])}"
    return text


def new_order_admin_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Keyboard on the admin's informational message for a brand-new order.

    The order has already been broadcast to every active courier by the time
    the admin sees this; these buttons are an override for the admin to pick
    a courier manually, resend the broadcast, or cancel the order outright.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚴 Выбрать курьера вручную ⚫   ",
                    callback_data=f"admin:choose_courier:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ ⚫   ", callback_data=f"admin:status:{order_id}:cancelled"
                )
            ],
            [InlineKeyboardButton(text="🏠 В меню 🟣   ", callback_data="menu")],
        ]
    )


def customer_chat_enabled() -> bool:
    """Писать ли покупателю в чат с ботом о статусах заказа — НЕТ (владелец 28.10.2026: покупателю в бот
    только «Курьер у двери» (notify_customer_arrived) и сообщения в чатах — от курьера и от RelaxGas)."""
    return False


async def notify_customer_arrived(bot: Bot, order) -> None:
    """Курьер нажал «Я на месте» (или «Напомнить покупателю ещё раз») — покупателю
    в чат «Курьер у двери». Приходит и в режиме «только приложение»."""
    courier = escape(order["courier_name"]) if order["courier_name"] else "Курьер"
    await bot.send_message(
        int(order["user_id"]),
        f"🚪 <b>Курьер у двери</b>\n{courier} с заказом №{order['id']} ждёт вас.",
    )


async def notify_admin_customer_review(
    bot: Bot, order, courier_name: str | None, rating: int | None, comment: str | None
) -> None:
    """Отзыв курьера о покупателе — только админу (не в лог-группу и не покупателю).
    Без комментария сообщаем лишь о низкой оценке (1–2), чтобы не засорять чат."""
    if not comment and (rating is None or rating > 2):
        return
    stars = f"оценка {rating} ⭐" if rating is not None else "комментарий"
    text = (
        f"📝 <b>Курьер {escape(courier_name or '—')} о покупателе</b> ({stars}), заказ №{order['id']}\n"
        f"Покупатель: {escape(plain_name(order['customer_name']))}, {escape(order['customer_phone'])}"
    )
    if comment:
        text += f"\n\n{escape(comment)}"
    try:
        await send_to_admins(bot, "send_message", text)
    except Exception:
        logger.exception("Could not notify admin about customer review for order %s", order["id"])


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject | None = None) -> None:
    await state.clear()
    user = message.from_user
    db.upsert_user(user.id, user.username, user.first_name, user.last_name)
    # Ссылка партнёра: t.me/<бот>?start=p_XXXXXXX — новый клиент закрепляется
    # за партнёром навсегда (баллы партнёру за каждый баллон в его заказах).
    code = (command.args or "").strip() if command else ""
    if code.startswith("p_"):
        try:
            result = db.bind_referral(user.id, code)
            if result == "bound":
                partner = db.get_partner_by_code(code)
                await notify_log_group(
                    message.bot,
                    f"🤝 Новый клиент {escape(user.full_name)} пришёл по ссылке партнёра "
                    f"(ID {int(partner['user_id'])})",
                )
        except Exception:
            logger.exception("Could not bind referral code %s for user %s", code, user.id)
    text = (
        "Добро пожаловать в магазин Relax Gas, тут вы можете заказать газ для начала "
        "или продолжения хорошего вечера."
    )
    text += "\n\nНажмите «🛒 Магазин», чтобы открыть каталог и оформить заказ."
    await message.answer(text, reply_markup=menu_keyboard(user.id))


@router.message(Command("id"))
async def user_id_command(message: Message) -> None:
    user = message.from_user
    db.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await message.answer(str(user.id))


@router.message(Command("chatid"))
async def chat_id_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    if message.chat.type not in {"group", "supergroup"}:
        return
    await message.answer(str(message.chat.id))


def is_individual_pending(order) -> bool:
    """Индивидуальный заказ (доставка не по Москве), который ещё не отдан курьерам."""
    return "individual" in order.keys() and int(order["individual"] or 0) == 1


async def notify_log_group(bot: Bot, text: str) -> None:
    """Best-effort duplicate of a key order/event notification into the
    admin+courier log group (LOG_GROUP_CHAT_ID), if one is configured.

    This is a separate chat from GROUP_CHAT_ID (the courier job-offer
    group) — purely an activity feed so admins and couriers can see
    movement and talk to each other; a failure here must never break the
    surrounding order flow.
    """
    if LOG_GROUP_CHAT_ID is None:
        return
    try:
        await bot.send_message(LOG_GROUP_CHAT_ID, text)
    except Exception:
        logger.exception("Could not send log-group notification")


async def dispatch_new_order(order, items, order_id: int, bot: Bot) -> None:
    """Notify the admin and broadcast to couriers that an order is ready to
    be picked up. Shared by the cash flow (called right after the order is
    created) and the crypto/SBP flow (called only once the customer has
    sent a payment screenshot) — see confirm_order / payment_screenshot_*."""
    # 1) Inform the admin that the order exists — this is informational,
    # the admin does not need to "accept" it for couriers to see it.
    individual = is_individual_pending(order)
    head = (
        f"🧾 ИНДИВИДУАЛЬНЫЙ ЗАКАЗ №{order_id} — доставка не по Москве.\n"
        "Свяжитесь с покупателем и согласуйте условия, затем в приложении «🚴 Назначить курьера».\n\n"
        if individual else f"🛒 НОВЫЙ ЗАКАЗ №{order_id}\n\n"
    )
    try:
        await send_to_admins(
            bot,
            "send_message",
            head + order_text(order, items, True),
            reply_markup=new_order_admin_keyboard(order_id),
        )
    except Exception:
        logger.exception("Could not notify admin about order %s", order_id)

    await notify_log_group(
        bot,
        (f"🧾 <b>Индивидуальный заказ №{order_id}</b> (не Москва — ждёт администратора)\n\n" if individual
         else f"🆕 <b>Новый заказ №{order_id}</b>\n\n")
        + f"{order_money_block(order, items)}\n"
        f"Адрес: {escape(order['delivery_address'])}",
    )

    # Курьерам заказ не рассылается: заказы раздаёт только администратор (28.10.2026).


# Срок трансляции «Пока не отключу» — Telegram присылает live_period = 0x7FFFFFFF.
LIVE_FOREVER = 0x7FFFFFFF


async def courier_live_instruction(bot: Bot, telegram_id: int) -> None:
    """Курьер нажал в приложении «📡 Включить трансляцию» — инструкция в чат с ботом."""
    await bot.send_message(
        telegram_id,
        "📡 <b>Включите трансляцию геопозиции</b>\n\n"
        "1. Внизу этого чата нажмите скрепку 📎\n"
        "2. Выберите <b>Геопозиция</b>\n"
        "3. Нажмите <b>Транслировать</b>\n"
        "4. Выберите срок <b>Пока не отключу</b>\n"
        "5. Вернитесь в приложение\n\n"
        "Не выключайте трансляцию до конца смены",
    )


@router.message(F.location)
async def courier_live_location_started(message: Message, bot: Bot) -> None:
    """A courier shared their (live) location with the bot — fan it out as
    a mirrored live-location message to every order currently assigned to
    them, so only that order's own customer ever sees it."""
    location = message.location
    if location.live_period is None:
        # Not a live share (e.g. a one-off pin from a customer's own
        # address step) — nothing for this handler to do.
        return
    courier = db.get_active_courier_by_telegram_id(message.from_user.id)
    if not courier:
        return
    # Трансляция привязывается к самому курьеру (24.10.2026): каждое обновление — на карту
    # админа и в карты всех его заказов в работе, в том числе назначенных позже.
    sent_at = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
    until = sent_at + timedelta(seconds=int(location.live_period))
    db.start_courier_live(message.from_user.id, message.chat.id, message.message_id,
                          location.latitude, location.longitude, until.isoformat(timespec="seconds"))
    text = "📡 <b>Трансляция включена</b>\nВернитесь в приложение"
    if int(location.live_period) < LIVE_FOREVER:
        msk = until.astimezone(timezone(timedelta(hours=3)))
        text += (f"\n\n⚠️ Трансляция выключится в {msk:%H:%M} по Москве\n"
                 "Лучше выбрать срок <b>Пока не отключу</b>")
    # Покупателю трансляцию в чат не пересылаем (28.10.2026): карта курьера — только в приложении.
    await message.answer(text)

@router.edited_message(F.location)
async def courier_live_location_updated(edited_message: Message, bot: Bot) -> None:
    """Mirror an ongoing live-location update to every customer-facing
    message it is currently being broadcast into."""
    location = edited_message.location
    if location is None:
        return
    # Трансляция курьера: без live_period — курьер её остановил.
    db.update_courier_live(edited_message.chat.id, edited_message.message_id,
                           location.latitude, location.longitude, stopped=location.live_period is None)
    broadcasts = db.get_location_broadcasts_by_source(
        edited_message.chat.id, edited_message.message_id
    )
    for row in broadcasts:
        db.set_order_courier_location(int(row["order_id"]), location.latitude, location.longitude)
        if not int(row["customer_chat_id"]):
            continue  # трансляция только в приложение (без сообщения покупателю)
        try:
            await bot.edit_message_live_location(
                chat_id=int(row["customer_chat_id"]),
                message_id=int(row["customer_message_id"]),
                latitude=location.latitude,
                longitude=location.longitude,
            )
        except Exception:
            logger.exception(
                "Could not update live-location broadcast for order %s",
                row["order_id"],
            )


@router.callback_query(F.data.startswith("dsp_revoke:"))
async def dispatch_revoke(callback: CallbackQuery) -> None:
    """🖥 Карта для ПК: «Отозвать этот вход» из уведомления о входе (webapp/dispatch.py)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    from webapp.dispatch import DispatchAuth

    try:
        session_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    done = DispatchAuth(db).revoke_session(session_id, callback.from_user.id)
    await callback.answer("Вход отозван." if done else "Этот вход уже закрыт.", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# Всё остальное — в приложении (28.10.2026): бот больше ничего не делает сам. Эти обработчики — последними.
APP_ONLY_TEXT = "📱 Всё делается в приложении — нажмите «🛒 Магазин»."


@router.message(F.chat.type == "private")
async def anything_else(message: Message) -> None:
    await message.answer(APP_ONLY_TEXT, reply_markup=menu_keyboard(message.from_user.id))


@router.callback_query()
async def any_old_button(callback: CallbackQuery) -> None:
    """Кнопки из старых сообщений бота (меню, заказы, админка) больше не работают."""
    await callback.answer("Теперь всё — в приложении: откройте его кнопкой «🛒 Магазин» в чате с ботом.", show_alert=True)




def admin_order_keyboard(
    order_id: int, current_status: str, courier_id: int | None = None
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if current_status in ("delivered", "cancelled", "disputed_closed"):
        transitions = []
    elif current_status == "disputed":
        # Спорный: основное управление — в приложении («⚠️ Спорные заказы»).
        transitions = [
            ("🔁 Вернуть в работу ⚫\xa0\xa0\xa0", "new"),
            ("✅ Всё-таки доставлен ⚫\xa0\xa0\xa0", "delivered"),
            ("❌ Отменить ⚫\xa0\xa0\xa0", "cancelled"),
        ]
    elif current_status == "not_delivered":
        transitions = [
            ("🔁 Передать заново (снять курьера) ⚫\xa0\xa0\xa0", "new"),
            ("✅ Всё-таки доставлен ⚫\xa0\xa0\xa0", "delivered"),
            ("❌ Отменить ⚫\xa0\xa0\xa0", "cancelled"),
        ]
    elif courier_id is None:
        # Nobody has the order yet: it can only be given to a courier or cancelled.
        transitions = [("❌ Отменить ⚫   ", "cancelled")]
    else:
        transitions = [
            ("🚚 В пути ⚫   ", "in_transit"),
            ("✅ Доставлен ⚫   ", "delivered"),
            ("↩️ Снять курьера ⚫   ", "new"),
            ("❌ Отменить ⚫   ", "cancelled"),
        ]
    for label, status in transitions:
        if status != current_status:
            buttons.append(
                [InlineKeyboardButton(text=label, callback_data=f"admin:status:{order_id}:{status}")]
            )
    if current_status == "new" and courier_id is None:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🚴 ВЫБРАТЬ КУРЬЕРА ⚫   ",
                    callback_data=f"admin:choose_courier:{order_id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="🛠 Админка ⚫   ", callback_data="admin")])
    buttons.append([InlineKeyboardButton(text="🏠 В меню 🟣   ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def notify_courier_text(bot: Bot, order, text: str) -> None:
    """Best-effort message to the courier on this order (if they have a Telegram ID)."""
    telegram_id = order["courier_telegram_id"] if order and "courier_telegram_id" in order.keys() else None
    if not telegram_id:
        return
    try:
        await bot.send_message(int(telegram_id), text)
    except Exception:
        logger.exception("Could not notify courier about order %s", order["id"])


async def notify_customer_status(bot: Bot, order, old_status: str) -> None:
    """Статусы заказа покупателю в бот не приходят (владелец 28.10.2026) — только в приложении."""
    return


def courier_delivered_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📍 Поделиться геолокацией 🟠   ", callback_data=f"courier:share_loc:{order_id}")],
            [InlineKeyboardButton(text="🚪 Я на месте 🟠   ", callback_data=f"courier:arrived:{order_id}")],
            [InlineKeyboardButton(text="✅ Заказ доставлен 🟠   ", callback_data=f"courier:delivered:{order_id}")],
            [InlineKeyboardButton(text="❌ Заказ не доставлен 🟠   ", callback_data=f"courier:not_delivered:{order_id}")],
            [InlineKeyboardButton(text="🏠 В меню 🟣   ", callback_data="menu")],
        ]
    )


async def notify_courier_assignment(bot: Bot, order) -> bool:
    """Notify the assigned courier when a Telegram ID is available."""
    if not order["courier_telegram_id"]:
        return False
    items = db.get_order_items(int(order["id"]))
    message = (
        f"<b>Назначен заказ №{order['id']}</b>\n"
        f"Адрес доставки: {escape(order['delivery_address'])}\n"
        f"Телефон получателя: {escape(order['customer_phone'])}\n\n"
        f"{order_money_block(order, items)}"
    )
    message += location_text(order)
    ret_rows = db.get_order_returns(int(order["id"]))
    if has_exchange_items(items) or ret_rows:
        ex = [f"{escape(i['product_name'])} × {int(i['quantity'])}" for i in items
              if "variant" in i.keys() and i["variant"] == "exchange" and not i["is_gift"]]
        rt = [f"{escape(r['product_name'])} × {int(r['quantity'])}" for r in ret_rows]
        parts = ([f"замена: {', '.join(ex)}"] if ex else []) + ([f"возврат: {', '.join(rt)}"] if rt else [])
        plural = "пустые баллоны" if len(ex) + len(rt) > 1 else "пустой баллон"
        message += f"\n\n⚠️ Заберите у клиента {plural} — {'; '.join(parts)}!"
    await bot.send_message(
        int(order["courier_telegram_id"]),
        message,
        reply_markup=courier_delivered_keyboard(int(order["id"])),
    )
    # Трансляция геопозиции идёт — напоминание не нужно; выключена — короткая инструкция.
    courier = db.get_courier(int(order["courier_id"])) if order["courier_id"] else None
    if not db.courier_live(courier)["active"]:
        await bot.send_message(
            int(order["courier_telegram_id"]),
            "📡 <b>Трансляция геопозиции выключена</b>\n"
            "Покупатель не видит вас на карте\n\n"
            "1. Внизу этого чата нажмите скрепку 📎\n"
            "2. Выберите <b>Геопозиция</b>\n"
            "3. Нажмите <b>Транслировать</b>\n"
            "4. Выберите срок <b>Пока не отключу</b>",
        )
    return True


async def stop_courier_location_broadcasts(bot: Bot, order_id: int) -> None:
    """Stop any live-location message(s) currently being mirrored to this
    order's customer (called when the order is delivered or cancelled)."""
    rows = db.deactivate_location_broadcasts_by_order(order_id)
    for row in rows:
        if not int(row["customer_chat_id"]):
            continue
        try:
            await bot.stop_message_live_location(
                chat_id=int(row["customer_chat_id"]),
                message_id=int(row["customer_message_id"]),
            )
        except Exception:
            logger.exception(
                "Could not stop live-location broadcast for order %s", order_id
            )


def courier_assignment_changed(current_order, updated_order) -> bool:
    """Return whether the selected courier differs from the previous assignment."""
    return not current_order or current_order["courier_id"] != updated_order["courier_id"]


async def close_courier_offer_messages(
    bot: Bot,
    order_id: int,
    winner_courier_id: int | None,
    closed_text: str | None = None,
    winner_name: str | None = None,
) -> None:
    """Replace the "Принять заказ" offer messages so nobody keeps tapping a
    dead button. closed_text overrides the text (e.g. for a cancelled order)."""
    offers = db.get_courier_offers(order_id)
    for offer in offers:
        if closed_text:
            text = closed_text
        elif offer["channel"] == "group":
            # One shared message for all couriers — say who took it.
            text = f"✅ Заказ №{order_id} взял курьер {escape(winner_name or '—')}."
        elif offer["courier_id"] == winner_courier_id:
            text = f"✅ Заказ №{order_id} взят вами."
        else:
            text = f"Заказ №{order_id} уже взят другим курьером."
        try:
            await bot.edit_message_text(
                text,
                chat_id=int(offer["chat_id"]),
                message_id=int(offer["message_id"]),
            )
        except Exception:
            logger.exception(
                "Could not close courier offer message %s for order %s",
                offer["message_id"],
                order_id,
            )


async def notify_assigned_parties(
    bot: Bot, current_order, order, notify_courier: bool = True
) -> None:
    if not courier_assignment_changed(current_order, order):
        return
    # 28.10.2026 (владелец): покупателю в бот — только «Курьер у двери» и сообщения в чатах; «Курьер найден» — в приложении.
    if notify_courier:
        try:
            await notify_courier_assignment(bot, order)
        except Exception:
            logger.exception("Could not notify courier about order %s", order["id"])
    courier_name = order["courier_name"] if "courier_name" in order.keys() and order["courier_name"] else "—"
    await notify_log_group(
        bot, f"🚜 Заказ №{order['id']} взят курьером: {escape(courier_name)}"
    )


# ---------------------------------------------------------------- бот — только уведомления (28.10.2026)
# Владелец: «работать исключительно как приложение». Бот: /start (вход и партнёрские ссылки), /id, /chatid,
# приём трансляции геопозиции курьера, «Отозвать вход» в карту для ПК и уведомления курьерам и админам
# (покупателю — только «Курьер у двери» и сообщения в чатах). В каждом сообщении — одна кнопка
# «📱 Открыть приложение»; все действия — в приложении. На всё остальное бот отвечает «Всё в приложении».
APP_BUTTON_TEXT = "📱 Открыть приложение"
_SEND_METHODS = (SendMessage, SendPhoto, SendLocation, SendDocument, SendVenue, CopyMessage)
_EDIT_METHODS = (EditMessageText, EditMessageCaption, EditMessageReplyMarkup)
_KEEP_CALLBACKS = ("dsp_revoke:",)  # кнопки, которые бот обрабатывает сам (см. dispatch_revoke)


async def app_only_markup(bot: Bot, chat_id) -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    if isinstance(chat_id, int) and chat_id > 0:
        button = InlineKeyboardButton(text=APP_BUTTON_TEXT, web_app=WebAppInfo(url=WEBAPP_URL))
    else:
        # В группах кнопки web_app запрещены Telegram — ведём в личный чат с ботом.
        me = await bot.me()
        button = InlineKeyboardButton(text=APP_BUTTON_TEXT, url=f"https://t.me/{me.username}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def _allowed_button(b: InlineKeyboardButton) -> bool:
    return bool(b.web_app) or bool(b.callback_data and b.callback_data.startswith(_KEEP_CALLBACKS))


class NotificationsOnlyMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        if isinstance(method, _SEND_METHODS + _EDIT_METHODS):
            chat_id = getattr(method, "chat_id", None)
            markup = getattr(method, "reply_markup", None)
            if isinstance(method, _EDIT_METHODS):
                # Правка без кнопок — так и оставляем (кнопки просто исчезают).
                if markup is not None and not getattr(method, "inline_message_id", None):
                    method.reply_markup = await app_only_markup(bot, chat_id)
            elif isinstance(markup, InlineKeyboardMarkup) and all(
                _allowed_button(b) for row in markup.inline_keyboard for b in row
            ):
                pass  # кнопка приложения или «Отозвать вход»
            elif not isinstance(markup, ReplyKeyboardRemove):
                method.reply_markup = await app_only_markup(bot, chat_id)
        return await make_request(bot, method)


def setup_bot(bot: Bot) -> None:
    bot.session.middleware(NotificationsOnlyMiddleware())


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    setup_bot(bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    webapp_runner = await start_mini_app(bot)
    logger.info("Gas delivery bot started in polling mode")
    try:
        await dispatcher.start_polling(bot)
    finally:
        if webapp_runner is not None:
            await webapp_runner.cleanup()


async def start_mini_app(bot: Bot):
    """Запустить веб-сервер Mini App в этом же процессе (та же база db), если
    он не выключен (WEBAPP_ENABLED=0). Ошибка запуска сервера не мешает
    боту работать — она уходит в лог и админу."""
    if not WEBAPP_ENABLED:
        return None
    import sys

    from webapp.bridge import TelegramBridge
    from webapp.core import Api
    from webapp.server import start_webapp

    try:
        bridge = TelegramBridge(bot, sys.modules[__name__])
        api = Api(db, ADMIN_IDS, bridge)
        if WEBAPP_URL:  # адрес для ссылок «🖥 Карта для ПК»: только https://домен
            from urllib.parse import urlsplit as _urlsplit

            _u = _urlsplit(WEBAPP_URL)
            api.public_url = f"{_u.scheme}://{_u.netloc}" if _u.scheme and _u.netloc else None
        # Своя база адресов OpenStreetMap: лежит рядом с базой магазина (на Amvera — /data),
        # свежая скачивается из Releases на GitHub (см. webapp/geodb.py).
        from pathlib import Path as _Path

        from webapp.geodb import AddressDB

        api.geo = AddressDB(_Path(DB_PATH).parent / "address_db" / "addresses.sqlite")
        # Маршруты по дорогам для доставки за пределы Москвы (ключ openrouteservice — ORS_API_KEY в Amvera).
        from webapp.routing import RouteService

        api.routing = RouteService(ORS_API_KEY)
        runner = await start_webapp(api, bridge, BOT_TOKEN, WEBAPP_HOST, WEBAPP_PORT)
        bridge.start_rate_updates()  # курсы криптовалют — раз в час, в фоне
        api.geo.start_updates()  # база адресов: скачать, только если её ещё нет; обновление — по кнопке админа
    except Exception as exc:
        logger.exception("Mini App server failed to start")
        try:
            await send_to_admins(
                bot,
                "send_message",
                f"⚠️ Веб-сервер Mini App не запустился: <code>{escape(repr(exc))}</code>\n"
                "Бот при этом работает как обычно.",
            )
        except Exception:
            logger.exception("Could not notify admin about Mini App start failure")
        return None
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            logger.exception("Could not set the Mini App menu button")
    else:
        logger.warning("WEBAPP_URL is not set: the Mini App server runs, but the bot shows no button for it")
    return runner


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Gas delivery bot stopped")
