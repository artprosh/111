"""Рабочий «мост» Mini App → Telegram.

Повторяет побочные эффекты обработчиков из main.py (уведомления админа,
курьеров, покупателя, лог-группы), вызывая ТЕ ЖЕ функции main.py, поэтому
тексты сообщений и правила рассылки не дублируются.

Модуль main передаётся объектом (sys.modules[__name__] из main.py), а не
импортируется заново: main.py запускается как __main__, и повторный
`import main` создал бы вторую копию модуля со своим db/router.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from collections import OrderedDict
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .auth import TelegramUser
from .crypto import RATE_TTL, RateCache

logger = logging.getLogger(__name__)


class TelegramBridge:
    def __init__(self, bot: Bot, main_module: Any) -> None:
        self.bot = bot
        self.m = main_module
        self.db = main_module.db
        self._file_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._rates = RateCache()

    # ------------------------------------------------------------ orders
    async def dispatch_new_order(self, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if order:
            await self.m.dispatch_new_order(order, self.db.get_order_items(order_id), order_id, self.bot)

    async def customer_cancelled(self, order_id: int) -> None:
        # Как customer_cancel_order() в main.py.
        m = self.m
        self.db.close_courier_offers(order_id, None)
        await m.close_courier_offer_messages(
            self.bot, order_id, None, closed_text=f"❌ Заказ №{order_id} отменён."
        )
        try:
            await m.send_to_admins(self.bot, "send_message", f"❌ Заказ №{order_id} отменён покупателем (Mini App).")
        except Exception:
            logger.exception("Could not notify admin about cancelled order %s", order_id)
        await m.notify_log_group(self.bot, f"❌ Заказ №{order_id} отменён покупателем")

    async def customer_received(self, order_id: int) -> None:
        # Как customer_confirm_received_rated() в main.py (оценка уже сохранена).
        m = self.m
        order = self.db.get_order(order_id)
        rating = self.db.get_courier_rating_for_order(order_id)
        suffix = f" (оценка вам: {rating} ⭐)" if rating else ""
        await m.stop_courier_location_broadcasts(self.bot, order_id)
        await m.notify_courier_text(self.bot, order, f"✅ Покупатель подтвердил получение заказа №{order_id}{suffix}.")
        await m.notify_log_group(self.bot, f"✅ Заказ №{order_id} доставлен")

    async def courier_delivered(self, order_id: int, prev_status: str) -> None:
        # Как courier_mark_delivered() в main.py.
        m = self.m
        order = self.db.get_order(order_id)
        await m.stop_courier_location_broadcasts(self.bot, order_id)
        try:
            await m.notify_customer_status(self.bot, order, prev_status)
        except Exception:
            logger.exception("Could not notify customer that order %s was delivered", order_id)
        await m.notify_log_group(self.bot, f"✅ Заказ №{order_id} доставлен")
        try:
            await m.send_to_admins(self.bot, "send_message", f"✅ Курьер отметил заказ №{order_id} доставленным.")
        except Exception:
            logger.exception("Could not notify admin about delivered order %s", order_id)

    async def courier_arrived(self, order_id: int) -> None:
        # Как courier_mark_arrived() в main.py: сообщение покупателю в чат.
        await self.m.notify_customer_arrived(self.bot, self.db.get_order(order_id))

    async def customer_review(self, order_id: int, rating: int | None, comment: str | None) -> None:
        # Отзыв курьера о покупателе — только админу (как в main.py).
        order = self.db.get_order(order_id)
        await self.m.notify_admin_customer_review(self.bot, order, order["courier_name"], rating, comment)

    async def customer_blocked(self, user_id: int, name: str, blocked: bool, reason: str | None) -> None:
        """Запись в канал (лог): покупатель добавлен в чёрный список / убран из него."""
        if blocked:
            text = f"🚫 Покупатель {escape(name)} (ID <code>{user_id}</code>) добавлен в чёрный список"
            if reason:
                text += f"\nПричина: {escape(reason)}"
        else:
            text = f"✅ Покупатель {escape(name)} (ID <code>{user_id}</code>) убран из чёрного списка"
        await self.m.notify_log_group(self.bot, text)

    async def referral_request(self, user_id: int, name: str, contact: str, is_courier: bool, kind: str,
                               comment: str | None = None) -> None:
        """Новая заявка «🤝 Реферальная программа» — админу и в канал (с комментарием человека)."""
        m = self.m
        who = "Курьер" if is_courier else "Покупатель"
        program = "партнёрскую программу" if kind == "partner" else "систему лояльности"
        text = f"🤝 <b>Заявка на {program}</b>\n{who}: {escape(name)} ({escape(contact)})"
        if comment:
            text += f"\n💬 Комментарий: {escape(comment)}"
        try:
            await m.send_to_admins(self.bot, "send_message", text)
        except Exception:
            logger.exception("Could not notify admin about referral request from %s", user_id)
        await m.notify_log_group(self.bot, f"🤝 {who} {escape(name)} ({escape(contact)}) оставил заявку на {program}")

    async def bot_link(self) -> str:
        """Ссылка на чат с ботом: t.me/<бот>."""
        if getattr(self, "_bot_username", None) is None:
            self._bot_username = (await self.bot.me()).username
        return f"https://t.me/{self._bot_username}"

    async def courier_live_instruction(self, telegram_id: int) -> None:
        await self.m.courier_live_instruction(self.bot, int(telegram_id))

    async def partner_link(self, code: str) -> str:
        """Ссылка партнёра: t.me/<бот>?start=<код> (параметр — до 64 символов base64url)."""
        if getattr(self, "_bot_username", None) is None:
            self._bot_username = (await self.bot.me()).username
        return f"https://t.me/{self._bot_username}?start={code}"

    async def partner_event(self, text: str) -> None:
        await self.m.notify_log_group(self.bot, escape(text))

    async def admin_alert(self, text: str) -> None:
        """Важное событие от курьера — админам в бот и запись в канал."""
        await self.m.send_to_admins(self.bot, "send_message", escape(text))
        await self.m.notify_log_group(self.bot, escape(text))

    async def courier_message(self, telegram_id: int, text: str) -> None:
        await self.bot.send_message(int(telegram_id), escape(text))

    async def order_changed(self, order_id: int, text: str) -> None:
        """Админ изменил состав заказа: сообщение курьеру и покупателю (если им можно писать)."""
        order = self.db.get_order(order_id)
        if not order:
            return
        targets = {int(order["user_id"])}
        if order["courier_id"]:
            c = self.db.get_courier(int(order["courier_id"]))
            if c and c["telegram_id"]:
                targets.add(int(c["telegram_id"]))
        for tid in targets:
            try:
                await self.bot.send_message(tid, escape(text))
            except Exception:
                logger.warning("order_changed: не удалось написать %s", tid)

    # ------------------------------------------------------------ 🖥 карта для ПК (/dispatch)
    async def dispatch_link(self, telegram_id: int, url: str) -> None:
        """Одноразовая ссылка входа — админу в личку. Предпросмотр ссылки
        выключен, а ссылка в <code>: Telegram не открывает её сам."""
        from aiogram.types import LinkPreviewOptions

        from .dispatch import SESSION_TTL

        text = (
            "🖥 <b>Вход в диспетчерскую карту на ПК</b>\n\n"
            f"<code>{escape(url)}</code>\n\n"
            "• Откройте ссылку на компьютере и нажмите «Войти».\n"
            "• Открыть её можно <b>один раз</b>: повторно или у другого человека она не сработает.\n"
            f"• Вход действует {SESSION_TTL // 3600} ч с момента открытия.\n"
            "• Новая ссылка отменяет эту, если её ещё не открыли. Не пересылайте её."
        )
        await self.bot.send_message(
            int(telegram_id), text, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

    async def dispatch_login(self, telegram_id: int, session: dict[str, Any]) -> None:
        """Уведомление о входе на ПК с кнопкой «Отозвать этот вход»."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from .core import msk_datetime

        text = (
            "🔐 <b>Выполнен вход в карту для ПК</b>\n"
            f"Браузер: {escape(session.get('browser') or '—')}\n"
            f"IP: {escape(session.get('ip') or '—')}\n"
            f"Когда: {msk_datetime(session.get('created_at'))} (МСК)\n"
            f"Действует до: {msk_datetime(session.get('expires_at'))} (МСК)\n\n"
            "Если это не вы — нажмите «Отозвать»."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Отозвать этот вход", callback_data=f"dsp_revoke:{int(session['id'])}")
        ]])
        await self.bot.send_message(int(telegram_id), text, reply_markup=markup)

    async def bonus_gift(self, recipient_id: int, text: str) -> None:
        """Другу пришли бонусы. 28.10.2026 (владелец): покупателю в бот только «Курьер у двери» и сообщения
        в чатах — об этом он узнаёт в приложении («Мои бонусы»)."""
        return

    async def bonus_event(self, text: str) -> None:
        """Бонусы покупателей (ручные начисления/списания, процент) — запись в канал."""
        await self.m.notify_log_group(self.bot, escape(text))

    async def referral_declined(self, name: str, kind: str, mode: str) -> None:
        program = "партнёрскую программу" if kind == "partner" else "систему лояльности"
        how = {"forever": "навсегда", "temp": "на 7 дней"}.get(mode, "(можно подать снова)")
        await self.m.notify_log_group(self.bot, f"🚫 Заявка {escape(name)} на {program} отклонена {how}")

    async def courier_released(self, order_id: int, prev: dict[str, Any]) -> None:
        """Курьер нажал «↩️ Взял заказ по ошибке»: заказ снова ждёт назначения администратором."""
        m = self.m
        await m.stop_courier_location_broadcasts(self.bot, order_id)
        courier = escape(prev.get("courier_name") or "—")
        try:
            await m.send_to_admins(
                self.bot,
                "send_message",
                f"↩️ Курьер {courier} вернул заказ №{order_id} («взял по ошибке»). "
                "Заказ снова ждёт назначения — назначьте курьера.",
            )
        except Exception:
            logger.exception("Could not notify admin about released order %s", order_id)
        await m.notify_log_group(self.bot, f"↩️ Заказ №{order_id}: курьер {courier} взял по ошибке — снова ждёт курьера")

    async def order_disputed(self, order_id: int) -> None:
        """Курьер нажал «📵 Покупатель не выходит на связь» — заказ в «Спорных»."""
        m = self.m
        order = self.db.get_order(order_id)
        await m.stop_courier_location_broadcasts(self.bot, order_id)
        courier = escape(order["courier_name"] or "—")
        try:
            await m.send_to_admins(
                self.bot,
                "send_message",
                f"📵 <b>Заказ №{order_id}: покупатель не выходит на связь</b> (курьер {courier}).\n"
                "Заказ в разделе «⚠️ Спорные заказы».\n\n"
                + m.order_text(order, self.db.get_order_items(order_id), True),
            )
        except Exception:
            logger.exception("Could not notify admin about disputed order %s", order_id)
        await m.notify_log_group(self.bot, f"📵 Заказ №{order_id}: покупатель не выходит на связь (курьер {courier}) — в «Спорных»")

    async def dispute_returned(self, order_id: int, prev: dict[str, Any]) -> None:
        """Админ: «🔁 Вернуть в работу» — заказ снова ждёт назначения."""
        m = self.m
        await m.notify_courier_text(self.bot, prev, f"↩️ Спорный заказ №{order_id} снят с вас администратором.")
        await m.notify_log_group(self.bot, f"🔁 Спорный заказ №{order_id} возвращён в работу — снова ждёт курьера")

    async def dispute_closed(self, order_id: int, prev: dict[str, Any]) -> None:
        """Админ: «🗑 Удалить заказ» — статус «Спорный — закрыт»."""
        m = self.m
        await m.notify_courier_text(self.bot, prev, f"🗑 Спорный заказ №{order_id} закрыт администратором.")
        await m.notify_log_group(self.bot, f"🗑 Спорный заказ №{order_id} закрыт администратором")

    async def courier_not_delivered(self, order_id: int, reason: str | None) -> None:
        # Как finish_not_delivered() в main.py.
        m = self.m
        order = self.db.get_order(order_id)
        await m.stop_courier_location_broadcasts(self.bot, order_id)
        reason_line = f"\nПричина: {escape(reason)}" if reason else ""
        courier_name = order["courier_name"] or "—"
        try:
            await self.bot.send_message(
                int(order["user_id"]),
                f"❌ Заказ №{order_id} не удалось доставить. Администратор свяжется с вами.",
            )
        except Exception:
            logger.exception("Could not notify customer about not delivered order %s", order_id)
        try:
            await m.send_to_admins(
                self.bot,
                "send_message",
                f"❌ <b>Заказ №{order_id} НЕ доставлен</b> (курьер {escape(courier_name)}){reason_line}\n\n"
                + m.order_text(order, self.db.get_order_items(order_id), True),
                reply_markup=m.admin_order_keyboard(order_id, order["status"], order["courier_id"]),
            )
        except Exception:
            logger.exception("Could not notify admin about not delivered order %s", order_id)
        await m.notify_log_group(
            self.bot, f"❌ Заказ №{order_id} не доставлен (курьер {escape(courier_name)}){reason_line}"
        )

    async def courier_location(self, order_id: int, lat: float, lon: float, first: bool) -> None:
        """Координаты уже записаны в БД — карта в Mini App покупателя их видит.
        Один раз (при первой передаче) покупателю дополнительно приходит точка
        в чат, чтобы он узнал об этом, даже не открывая приложение."""
        if not first or not self.m.customer_chat_enabled():
            return  # режим «только приложение»: покупатель видит курьера на карте в приложении
        order = self.db.get_order(order_id)
        customer_id = int(order["user_id"])
        await self.bot.send_location(customer_id, latitude=lat, longitude=lon)
        await self.bot.send_message(
            customer_id,
            f"📍 Курьер поделился геолокацией по заказу №{order_id}.\n"
            "Его положение обновляется на карте в приложении (раздел «Активные заказы»).",
        )

    # ------------------------------------------------------------ admin
    async def admin_status(self, order_id: int, prev: dict[str, Any] | None, status: str) -> None:
        # Как admin_status() в main.py (prev — заказ ДО изменения).
        m = self.m
        order = self.db.get_order(order_id)
        if status in ("delivered", "cancelled", "new"):
            await m.stop_courier_location_broadcasts(self.bot, order_id)
        if status == "delivered":
            await m.notify_log_group(self.bot, f"✅ Заказ №{order_id} доставлен")
        if status == "cancelled":
            self.db.close_courier_offers(order_id, None)
            await m.close_courier_offer_messages(
                self.bot, order_id, None, closed_text=f"❌ Заказ №{order_id} отменён."
            )
            await m.notify_courier_text(self.bot, prev, f"❌ Заказ №{order_id} отменён администратором.")
            await m.notify_log_group(self.bot, f"❌ Заказ №{order_id} отменён администратором")
        if status == "delivered" and prev and prev.get("status") != "delivered":
            await m.notify_courier_text(
                self.bot, prev, f"✅ Администратор отметил заказ №{order_id} доставленным."
            )
        if status == "new" and prev and prev.get("courier_id") is not None:
            await m.notify_courier_text(self.bot, prev, f"↩️ Заказ №{order_id} снят с вас администратором.")
            await m.notify_log_group(self.bot, f"↩️ Заказ №{order_id} снова ждёт курьера")
        try:
            await m.notify_customer_status(self.bot, order, prev.get("status", "") if prev else "")
        except Exception:
            logger.exception("Could not notify customer for order %s", order_id)

    async def admin_assigned(self, order_id: int, prev: dict[str, Any] | None) -> None:
        # Как admin_choose_courier() в main.py.
        m = self.m
        order = self.db.get_order(order_id)
        self.db.close_courier_offers(order_id, order["courier_id"])
        await m.close_courier_offer_messages(
            self.bot, order_id, order["courier_id"], winner_name=order["courier_name"]
        )
        await m.notify_assigned_parties(self.bot, prev or None, order)

    async def call_admin(self, user: TelegramUser, text: str) -> None:
        # Как call_admin_message() в main.py.
        m = self.m
        name = " ".join(p for p in (user.first_name, user.last_name) if p) or "Покупатель"
        contact = f"@{user.username}" if user.username else f"ID {user.id}"
        await m.send_to_admins(
            self.bot,
            "send_message",
            f"📞 <b>Вопрос от покупателя (Mini App)</b>\n{escape(name)} ({escape(contact)})\n\n{escape(text)}",
        )
        await m.notify_log_group(self.bot, f"📞 Покупатель {escape(name)} ({escape(contact)}) позвал администратора")

    async def support_message(self, user: TelegramUser, text: str | None, has_photo: bool) -> None:
        """Покупатель написал в чат с администрацией — админам в бот (ответ — в приложении)."""
        name = " ".join(p for p in (user.first_name, user.last_name) if p) or "Покупатель"
        courier = False
        try:
            courier = self.db.get_active_courier_by_telegram_id(user.id) is not None
        except Exception:
            pass
        if courier:
            name = "🚴 Курьер " + name
        contact = f"@{user.username}" if user.username else f"ID {user.id}"
        body = escape(text[:500]) if text else ""
        await self.m.send_to_admins(
            self.bot, "send_message",
            f"💬 <b>Чат: {escape(name)}</b> ({escape(contact)})\n{body}{chr(10) + '📷 Фото' if has_photo else ''}\n\n"
            "Ответить — в приложении: «💬 Чаты с покупателями» на главной админа",
        )

    async def support_reply(self, user_id: int) -> None:
        """RelaxGas ответил в чате — покупателю уведомление (кнопка «Открыть приложение» — сама)."""
        await self.bot.send_message(int(user_id), "💬 <b>Новое сообщение от RelaxGas</b>\nОткройте приложение → «💬 Чат с RelaxGas»")

    async def order_chat(self, order_id: int, to_courier: bool, text: str | None, has_photo: bool) -> None:
        """Сообщение в чате заказа — второй стороне в бот (без контактов отправителя)."""
        order = self.db.get_order(order_id)
        if not order:
            return
        preview = escape((text or "")[:200]) + ("\n📷 Фото" if has_photo else "")
        if to_courier:
            chat_id = order["courier_telegram_id"]
            head = f"💬 <b>Покупатель по заказу №{order_id}</b>"
            tail = "Ответить — в приложении: «🚚 В работе» → «💬 Чат с покупателем»"
        else:
            chat_id = order["user_id"]
            head = f"💬 <b>Курьер по заказу №{order_id}</b>"
            tail = "Ответить — в приложении: «Активные заказы» → «💬 Чат с курьером»"
        if chat_id:
            await self.bot.send_message(int(chat_id), f"{head}\n{preview}\n\n{tail}")

    # ------------------------------------------------------------ payments
    async def upload_payment_proof(self, data: bytes, mime: str, caption: str) -> tuple[str, Any]:
        """Скриншот оплаты загружается в Telegram (в чат админа) — так у него
        появляется file_id, который бот хранит в orders.payment_proof_file_id,
        ровно как при отправке скриншота в чат."""
        ext = mimetypes.guess_extension(mime) or ".jpg"
        sent = await self.bot.send_photo(
            self.m.ADMIN_ID,
            BufferedInputFile(data, filename=f"payment{ext}"),
            caption=escape(caption),
        )
        return sent.photo[-1].file_id, (sent.chat.id, sent.message_id)

    async def upload_image(self, data: bytes, mime: str, caption: str) -> str:
        """Фото шапки каталога / QR оплаты из приложения → file_id в Telegram
        (фото отправляется в чат админа, как будто он прислал его боту)."""
        ext = mimetypes.guess_extension(mime) or ".jpg"
        sent = await self.bot.send_photo(
            self.m.ADMIN_ID, BufferedInputFile(data, filename=f"image{ext}"), caption=escape(caption)
        )
        return sent.photo[-1].file_id

    async def get_crypto_rates(self, codes: list[str]) -> dict[str, float]:
        return await self._rates.get(codes)

    async def get_crypto_rates_now(self, codes: list[str]) -> tuple[dict[str, float], float | None]:
        return await self._rates.get_now(codes)

    def start_rate_updates(self) -> None:
        """Раз в час обновлять курсы в фоне, чтобы покупатель и курьер никогда
        не ждали CoinGecko и курс не был старше часа."""
        if getattr(self, "_rate_loop", None) is not None:
            return

        async def loop() -> None:
            while True:
                self._rates.refresh_in_background()
                await asyncio.sleep(RATE_TTL)

        self._rate_loop = asyncio.create_task(loop())

    async def send_payment_photo(self, order_id: int, data: bytes, mime: str, caption: str) -> str:
        """Фото экрана покупателя с переводом — в Telegram-канал (LOG_GROUP_CHAT_ID),
        куда дублируется вся активность; если канал не задан — админу."""
        ext = mimetypes.guess_extension(mime) or ".jpg"
        chat_id = self.m.LOG_GROUP_CHAT_ID if self.m.LOG_GROUP_CHAT_ID is not None else self.m.ADMIN_ID
        sent = await self.bot.send_photo(
            chat_id, BufferedInputFile(data, filename=f"payment_{order_id}{ext}"), caption=escape(caption)[:1024]
        )
        return sent.photo[-1].file_id

    async def send_document(self, chat_id: int, data: bytes, filename: str, caption: str) -> None:
        # Выгрузка Excel — только в личный чат администратора: не в канал/группу и не курьерам.
        if int(chat_id) not in self.m.ADMIN_IDS:
            raise PermissionError("Выгрузка отправляется только администраторам в личный чат.")
        await self.bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=escape(caption))

    async def test_log_group(self) -> bool:
        # Как admin_test_log() в main.py.
        if self.m.LOG_GROUP_CHAT_ID is None:
            return False
        await self.bot.send_message(
            self.m.LOG_GROUP_CHAT_ID,
            "Здравствуйте, это сообщение создано для проверки коммуникации "
            "телеграм-бота с телеграм-каналом. Спасибо за внимание.",
        )
        return True

    async def finalize_payment_proof(self, ref: Any, caption: str) -> None:
        if not ref:
            return
        chat_id, message_id = ref
        await self.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=escape(caption))

    async def get_usdt_rate(self) -> float | None:
        return await self.m.get_usdt_rub_rate()

    # ------------------------------------------------------------ media
    async def fetch_file(self, file_id: str) -> tuple[bytes, str] | None:
        """Скачать картинку из Telegram по file_id (фото товара из бота, QR).
        Небольшой кэш в памяти, чтобы не ходить в Telegram на каждый показ."""
        if file_id in self._file_cache:
            self._file_cache.move_to_end(file_id)
            return self._file_cache[file_id]
        cached = self._disk_get(file_id)
        if cached is None:
            file = await self.bot.get_file(file_id)
            buffer = await self.bot.download_file(file.file_path)
            data = buffer.read() if buffer else b""
            mime = mimetypes.guess_type(file.file_path or "")[0] or "image/jpeg"
            if data:
                self._disk_put(file_id, data, mime)
            cached = (data, mime)
        self._file_cache[file_id] = cached
        while len(self._file_cache) > 128:
            self._file_cache.popitem(last=False)
        return cached

    # Картинки (фото товаров, QR) хранятся и на диске рядом с базой (/data):
    # после перезапуска/обновления их не нужно заново качать из Telegram.
    # file_id в Telegram неизменен для одного и того же файла, поэтому кэш
    # не устаревает; новая картинка — новый file_id.
    MEDIA_CACHE_MAX_FILES = 500

    def _media_dir(self) -> Path:
        return Path(str(self.db.path)).resolve().parent / "media_cache"

    def _media_path(self, file_id: str) -> Path:
        return self._media_dir() / hashlib.sha256(file_id.encode()).hexdigest()

    def _disk_get(self, file_id: str) -> tuple[bytes, str] | None:
        try:
            path = self._media_path(file_id)
            mime = path.with_suffix(".mime").read_text(encoding="utf-8").strip() or "image/jpeg"
            return path.read_bytes(), mime
        except OSError:
            return None

    def _disk_put(self, file_id: str, data: bytes, mime: str) -> None:
        try:
            folder = self._media_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = self._media_path(file_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            path.with_suffix(".mime").write_text(mime, encoding="utf-8")
            files = sorted((f for f in folder.iterdir() if not f.suffix), key=lambda f: f.stat().st_mtime)
            for old in files[: max(0, len(files) - self.MEDIA_CACHE_MAX_FILES)]:
                old.unlink(missing_ok=True)
                old.with_suffix(".mime").unlink(missing_ok=True)
        except OSError:
            logger.exception("Mini App: не удалось сохранить картинку в кэш на диске")
