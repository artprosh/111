"""Выгрузка продаж в Excel (.xlsx) для админа.

Формат (по заданию): строка = товар или услуга из заказа, по времени размещения
заказа. «Сумма» в строке — только эта строка (количество × цена).

Сверху у заголовков — фильтры (дата, товар/услуга, оплата, монета…). Над
таблицей два итога: «по выборке» — формула SUBTOTAL, пересчитывается при
фильтре (Excel, Google Таблицы, Numbers, LibreOffice), и «всё» — готовые числа,
включая сброшенные из истории товары (видны в любом просмотрщике).

Оплата криптой: монета и сеть, сумма строки в монете по курсу на момент
оформления заказа, курс на момент выгрузки и сколько это в рублях сейчас.
Второй лист «Крипта» — то же по заказам и итог по каждой монете.

Суточная выгрузка — ровно сутки с 12:00 до 12:00 по Москве; общая — с
первого заказа. Строки из сброшенной истории заказов помечаются, моменты
сброса показываются отдельными строками.
"""
from __future__ import annotations

from copy import copy
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

from .crypto import CURRENCIES
from .regions import REGION_LABELS

# Москва — UTC+3 круглый год (переход на летнее время отменён в 2014 году).
MSK = timezone(timedelta(hours=3), "МСК")
SHIFT_HOUR = 12
MONEY = '#,##0" ₽"'
RATE = '#,##0.00" ₽"'


def to_utc_iso(dt: datetime) -> str:
    """В том же формате, что db.Database.now(): '2026-10-16T09:00:00+00:00'."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def msk(value: str) -> datetime:
    return parse_iso(value).astimezone(MSK)


def shift_bounds(day: date) -> tuple[datetime, datetime]:
    """Сутки, начинающиеся в 12:00 МСК дня `day`, до 12:00 МСК следующего дня."""
    start = datetime.combine(day, time(SHIFT_HOUR), tzinfo=MSK)
    return start, start + timedelta(days=1)


def last_completed_shift_day(now: datetime | None = None) -> date:
    """Начало последних ЗАВЕРШЁННЫХ суток: после 12:00 МСК — вчера, до — позавчера."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    return (now - timedelta(days=1 if now.hour >= SHIFT_HOUR else 2)).date()


# Колонки основного листа: (заголовок, ширина-минимум)
COLUMNS = [
    ("Дата", 11), ("Время", 8), ("Месяц", 17), ("День недели", 9), ("Заказ №", 9), ("Номер заказа", 18),
    ("Тип", 9), ("Товар / услуга", 24), ("Вариант", 11), ("Цена за шт., ₽", 11),
    ("Кол-во", 9), ("Новых, шт", 9), ("На замену, шт", 10), ("Сумма, ₽", 12),
    ("Доставка", 12), ("Подъём", 13), ("Этажей подъёма", 10),
    ("Покупатель", 18), ("Telegram", 14), ("Telegram ID", 12), ("Телефон", 15), ("Адрес", 30), ("Курьер", 15),
    ("Оплата", 10), ("Монета", 9), ("Сеть", 11), ("Сумма в крипте", 17),
    ("Курс при заказе", 16), ("Курс на выгрузку", 16), ("По курсу выгрузки, ₽", 15),
    ("Сброшен", 10), ("Примечание", 40),
]
# 1 → «да», 0 → «нет»: в ячейке число (итог SUBTOTAL считает), на экране и в фильтре — да/нет.
YES_NO = '"да";"да";"нет"'
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
             "сентябрь", "октябрь", "ноябрь", "декабрь"]
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def order_number(user_id: Any, order_no: Any) -> str:
    """«622221060_24» — как у покупателя в приложении («Номер вашего заказа»)."""
    return f"{user_id}_{order_no}" if user_id not in (None, "") else str(order_no)


def month_text(d: date) -> str:
    """«2026-10 октябрь» — сортируется по порядку и читается."""
    return f"{d:%Y-%m} {MONTHS_RU[d.month - 1]}"


def nick(username: str | None) -> str:
    u = (username or "").strip().lstrip("@")
    return f"@{u}" if u else ""
C = {name: i + 1 for i, (name, _) in enumerate(COLUMNS)}
PAY_LABELS = {"cash_on_delivery": "Наличные", "sbp": "СБП", "crypto": "Крипта"}


def coin_amount(rub: int, rate: float, currency: str) -> Decimal:
    """Сумма в монете по курсу — вверх, как при оформлении заказа."""
    decimals = CURRENCIES.get(currency, (None, 6))[1]
    step = Decimal(1).scaleb(-decimals)
    return (Decimal(int(rub)) / Decimal(str(rate))).quantize(step, rounding=ROUND_CEILING)


def coin_format(currency: str) -> str:
    decimals = CURRENCIES.get(currency, (None, 6))[1]
    return "0." + "0" * decimals if decimals else "0"


def _put(ws, row: int, col: int, value: Any, fmt: str | None = None) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    if fmt:
        cell.number_format = fmt


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def build_xlsx(
    data: dict[str, Any],
    title: str,
    generated_at: datetime | None = None,
    rates: dict[str, float] | None = None,
    rates_at: float | None = None,
    full: dict[str, Any] | None = None,
) -> bytes:
    generated_at = (generated_at or datetime.now(timezone.utc)).astimezone(MSK)
    rates = rates or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Продажи"
    ncol = len(COLUMNS)
    bold = Font(bold=True)
    thin = Side(style="thin", color="BBBBBB")
    head_fill = PatternFill("solid", fgColor="FFE8D2")
    total_fill = PatternFill("solid", fgColor="FFF7EE")
    wipe_fill = PatternFill("solid", fgColor="FFF4D6")
    crypto_fill = PatternFill("solid", fgColor="EEF4FF")
    archived_font = Font(color="9A3412", italic=True)
    widths = {i + 1: w for i, (_, w) in enumerate(COLUMNS)}

    def track(col: int, text: Any) -> None:
        widths[col] = max(widths[col], min(48, int(len(str(text)) * 1.2) + 3))

    def merged(row: int, value: str, font: Font) -> None:
        cell = ws.cell(row=row, column=1, value=value)
        cell.font = font
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)

    merged(1, title, Font(bold=True, size=13))
    merged(2, (
        "Только доставленные заказы, строка = товар или подъём. Время — МСК, по моменту размещения заказа. "
        "«Доставка» и «Подъём» (да/нет) стоят в первой строке каждого заказа — итог сверху считает доставки и подъёмы, "
        f"а не строки. Фильтры — стрелки в заголовках. Сформировано {generated_at:%d.%m.%Y %H:%M} МСК."
    ), Font(italic=True, color="666666"))
    coins = sorted({x["crypto_currency"] for x in data["lines"] if x.get("crypto_currency")})
    if coins:
        when = datetime.fromtimestamp(rates_at, timezone.utc).astimezone(MSK) if rates_at else generated_at
        known = [f"1 {c} = {rates[c]:,.2f} ₽".replace(",", " ") for c in coins if rates.get(c)]
        missing = [c for c in coins if not rates.get(c)]
        text = f"Курс на момент выгрузки (CoinGecko, {when:%d.%m.%Y %H:%M} МСК): " + ("; ".join(known) or "—")
        if missing:
            text += f". Нет курса: {', '.join(missing)} — колонки «на выгрузку» пустые."
    else:
        text = "Оплат криптовалютой в этом периоде нет."
    merged(3, text, Font(color="1E3A8A"))
    ws.row_dimensions[2].height = 30
    ws.row_dimensions[3].height = 30

    # Строки продаж и отметки о сбросах истории — по времени.
    events: list[tuple[str, int, dict[str, Any]]] = []
    for line in data["lines"]:
        events.append((parse_iso(line["created_at"]).isoformat(), 0, line))
    for wipe in data["wipes"]:
        events.append((parse_iso(wipe["wiped_at"]).isoformat(), 1, wipe))
    events.sort(key=lambda e: (e[0], e[1]))

    total_row_sel, total_row_all, header_row = 5, 6, 8
    for col, (name, _) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=header_row, column=col, value=name)
        cell.font = bold
        cell.fill = head_fill
        cell.border = Border(bottom=thin)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[header_row].height = 32

    row = header_row + 1
    first_data_row = row
    sum_qty = sum_rub = sum_now = sum_new = sum_exch = sum_deliveries = sum_lifts = 0
    seen_orders: set[tuple] = set()
    # Был ли в заказе подъём (строка подъёма идёт последней в заказе).
    order_lifts: dict[tuple, bool] = {}
    for line in data["lines"]:
        key = (line["order_no"], line["created_at"], bool(line.get("archived")))
        order_lifts[key] = order_lifts.get(key, False) or line.get("line_kind") == "lift"
    crypto_orders: dict[tuple, dict[str, Any]] = {}
    for _, kind, item in events:
        if kind == 1:
            when = msk(item["wiped_at"])
            ws.cell(row=row, column=C["Дата"], value=when.date()).number_format = "DD.MM.YYYY"
            ws.cell(row=row, column=C["Время"], value=when.time().replace(second=0, microsecond=0)).number_format = "HH:MM"
            ws.cell(row=row, column=C["Товар / услуга"], value="⚠️ Сброс истории заказов")
            note = f"Удалено заказов: {item['orders_deleted']}. Проданное до сброса — в строках «сброшен»."
            ws.cell(row=row, column=C["Примечание"], value=note)
            for col in range(1, ncol + 1):
                cell = ws.cell(row=row, column=col)
                cell.fill = wipe_fill
                cell.font = Font(bold=True, color="92400E")
            row += 1
            continue
        when = msk(item["created_at"])
        rub = int(item["line_total"])
        is_lift = item.get("line_kind") == "lift"
        is_points = item.get("line_kind") == "points"  # «Оплата баллами» партнёра: тоже минус
        is_bonus = item.get("line_kind") == "bonus" or is_points  # «Оплата бонусами»: сумма со знаком минус
        is_descent = item.get("line_kind") == "descent"  # ⬇️ спуск пустого баллона к «Замене» (28.10.2026)
        # доставка за пределы Москвы (назначил админ); спуск — такая же услуга без штук товара
        is_delivery = item.get("line_kind") == "delivery" or is_descent
        qty = 0 if is_lift or is_bonus or is_delivery else int(item["quantity"])
        is_exchange = item.get("variant") == "exchange"
        sum_qty += qty
        sum_rub += rub
        sum_new += 0 if is_lift or is_bonus or is_delivery or is_exchange else qty
        sum_exch += qty if is_exchange else 0
        okey = (item["order_no"], item["created_at"], bool(item.get("archived")))
        first_in_order = okey not in seen_orders
        seen_orders.add(okey)
        put = lambda name, value, fmt=None: _put(ws, row, C[name], value, fmt)  # noqa: E731
        put("Дата", when.date(), "DD.MM.YYYY")
        put("Время", when.time().replace(second=0, microsecond=0), "HH:MM")
        put("Месяц", month_text(when.date()))
        put("День недели", WEEKDAYS_RU[when.weekday()])
        put("Заказ №", int(item["order_no"]))
        put("Номер заказа", order_number(item.get("user_id"), item["order_no"]))
        put("Тип", "Подъём" if is_lift else ("Баллы" if is_points else ("Бонусы" if is_bonus else (
            "Спуск" if is_descent else ("Доставка" if is_delivery else "Товар")))))
        name = item.get("product") or item["item_name"]
        put("Товар / услуга", name)
        track(C["Товар / услуга"], name)
        if not is_lift and not is_bonus and not is_delivery:
            put("Вариант", "на замену" if is_exchange else "новый")
            put("Кол-во", qty)
            put("Новых, шт", 0 if is_exchange else qty)
            put("На замену, шт", qty if is_exchange else 0)
        floors = int(item.get("floor_lift_floors") or 0)
        if item.get("unit_price") is not None:
            put("Цена за шт., ₽", int(item["unit_price"]), MONEY)
        if is_lift and floors:
            put("Этажей подъёма", floors)
        put("Сумма, ₽", rub, MONEY)
        if first_in_order:
            had_lift = order_lifts.get(okey, False)
            put("Доставка", 1, YES_NO)
            put("Подъём", 1 if had_lift else 0, YES_NO)
            sum_deliveries += 1
            sum_lifts += 1 if had_lift else 0
        put("Покупатель", item.get("customer_name") or ("нет данных" if item.get("archived") and not item.get("user_id") else ""))
        put("Telegram", nick(item.get("customer_username")))
        put("Telegram ID", item.get("user_id") or "")
        put("Телефон", item.get("customer_phone") or "")
        put("Адрес", item.get("delivery_address") or "")
        put("Курьер", item.get("courier_name") or "")
        method = item.get("payment_method")
        ws.cell(row=row, column=C["Оплата"], value=PAY_LABELS.get(method, "—" if not method else method))
        cur = item.get("crypto_currency")
        if method == "crypto":
            label = cur + (f" · {item['crypto_network']}" if item.get("crypto_network") else "") if cur else "нет данных"
            ws.cell(row=row, column=C["Монета"], value=cur or "нет данных")
            ws.cell(row=row, column=C["Сеть"], value=item.get("crypto_network") or "")
            track(C["Сеть"], item.get("crypto_network") or "")
            rate = item.get("crypto_rate")
            if cur and rate:
                amount = coin_amount(rub, rate, cur)
                c = ws.cell(row=row, column=C["Сумма в крипте"], value=float(amount))
                c.number_format = coin_format(cur) + f'" {cur}"'
                track(C["Сумма в крипте"], f"{amount} {cur}")
                track(C["Курс при заказе"], f"{rate:,.2f} ₽")
                ws.cell(row=row, column=C["Курс при заказе"], value=float(rate)).number_format = RATE
                now_rate = rates.get(cur)
                if now_rate:
                    ws.cell(row=row, column=C["Курс на выгрузку"], value=float(now_rate)).number_format = RATE
                    now_rub = int((amount * Decimal(str(now_rate))).to_integral_value())
                    ws.cell(row=row, column=C["По курсу выгрузки, ₽"], value=now_rub).number_format = MONEY
                    sum_now += now_rub
                key = (item["order_no"], item["created_at"], cur)
                o = crypto_orders.setdefault(key, {
                    "order_no": item["order_no"], "when": when, "label": label, "currency": cur,
                    "rub": 0, "rate": float(rate), "now_rate": now_rate, "archived": item.get("archived"),
                })
                o["rub"] += rub
            for col in range(C["Оплата"], C["По курсу выгрузки, ₽"] + 1):
                ws.cell(row=row, column=col).fill = crypto_fill
        if item.get("archived"):
            ws.cell(row=row, column=C["Сброшен"], value="сброшен")
            wiped = msk(item["wiped_at"])
            ws.cell(row=row, column=C["Примечание"], value=f"Сброшен из истории {wiped:%d.%m.%Y %H:%M} МСК")
            for col in range(1, ncol + 1):
                ws.cell(row=row, column=col).font = archived_font
        row += 1
    last_data_row = row - 1

    if not data["lines"]:
        ws.cell(row=row, column=1, value="За этот период доставленных заказов нет.").font = Font(italic=True)
        row += 1

    # Итоги над таблицей: «по выборке» (формула, следует за фильтром) и «всё».
    # Итоговые столбцы: число в строке итогов. «Доставка» / «Подъём» — количество.
    total_cols = {
        "Кол-во": None, "Новых, шт": None, "На замену, шт": None, "Сумма, ₽": MONEY,
        "Доставка": '0" дост."', "Подъём": '0" подъём."', "По курсу выгрузки, ₽": MONEY,
    }

    def total_line(r: int, label: str, values: dict[str, Any], fill) -> None:
        ws.cell(row=r, column=1, value=label).font = bold
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=C["Цена за шт., ₽"])
        for name, fmt in total_cols.items():
            cell = ws.cell(row=r, column=C[name], value=values.get(name, 0))
            cell.font = bold
            if fmt:
                cell.number_format = fmt
        for col in range(1, ncol + 1):
            ws.cell(row=r, column=col).fill = fill
            ws.cell(row=r, column=col).border = Border(top=thin, bottom=thin)

    if last_data_row >= first_data_row:
        rng = lambda col: f"{get_column_letter(col)}{first_data_row}:{get_column_letter(col)}{last_data_row}"  # noqa: E731
        total_line(total_row_sel, "ИТОГО ПО ВЫБОРКЕ (фильтр)",
                   {name: f"=SUBTOTAL(109,{rng(C[name])})" for name in total_cols}, head_fill)
    else:
        total_line(total_row_sel, "ИТОГО ПО ВЫБОРКЕ", {}, head_fill)
    total_line(total_row_all, "ИТОГО ВСЁ (вкл. сброшенные)", {
        "Кол-во": sum_qty, "Новых, шт": sum_new, "На замену, шт": sum_exch, "Сумма, ₽": sum_rub,
        "Доставка": sum_deliveries, "Подъём": sum_lifts, "По курсу выгрузки, ₽": sum_now,
    }, total_fill)
    merged(total_row_all + 1, (
        "«По выборке» пересчитывается, когда вы выбираете дату, месяц, товар, вариант, покупателя, курьера или оплату "
        "стрелками в заголовках (Excel, Google Таблицы, Numbers). «Кол-во» — баллоны (подъём не считается). "
        "Сумма подъёмов — фильтр «Тип» = Подъём. «Тип» = Бонусы — оплата бонусами (со знаком минус), "
        "поэтому «Сумма» — реально полученные деньги. «По курсу выгрузки» — сколько стоят полученные монеты сейчас."
    ), Font(italic=True, color="888888", size=9))

    if data.get("unfinished"):
        warn = ws.cell(row=row + 1, column=1, value=(
            f"⚠️ Ещё не завершено заказов, размещённых в этот период: {data['unfinished']}. "
            "Они появятся в выгрузке, когда будут доставлены."
        ))
        warn.font = Font(italic=True, color="92400E")

    track(C["Курс на выгрузку"], f"{max(rates.values()):,.2f} ₽" if rates else "")
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width
    for r in range(first_data_row, last_data_row + 1):
        ws.cell(row=r, column=C["Примечание"]).alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    end_row = max(last_data_row, header_row + 1)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ncol)}{end_row}"
    # Печать / PDF: альбомная, по ширине страницы.
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.3, right=0.3, top=0.4, bottom=0.4)
    ws.print_title_rows = f"{header_row}:{header_row}"

    if full is not None:
        _full_sheets(wb, full)
        _partners_sheet(wb, full)
        _admins_sheet(wb, full)
        _order_chats_sheet(wb, full)
        if full.get("resets"):
            _resets_sheet(wb, full)
    _crypto_sheet(wb, list(crypto_orders.values()), rates)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _crypto_sheet(wb: Workbook, orders: list[dict[str, Any]], rates: dict[str, float]) -> None:
    """Лист «Крипта»: каждый заказ, оплаченный криптой, и итог по каждой монете."""
    ws = wb.create_sheet("Крипта")
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E0EAFF")
    headers = ["Дата", "Время", "Заказ №", "Монета · сеть", "Сумма заказа, ₽", "Сумма в крипте",
               "Курс при заказе", "Курс на выгрузку", "По курсу выгрузки, ₽", "Разница, ₽", "Сброшен"]
    widths = [12, 8, 9, 26, 15, 18, 16, 16, 16, 13, 10]
    ws.cell(row=1, column=1, value="Оплаты криптовалютой: сумма в монете по курсу при оформлении и её стоимость по курсу на момент выгрузки").font = Font(bold=True, size=12)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    for col, name in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=name)
        cell.font, cell.fill = bold, head_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w
    row = 4
    per_coin: dict[str, dict[str, Any]] = {}
    for o in sorted(orders, key=lambda x: (x["when"], x["order_no"])):
        cur = o["currency"]
        amount = coin_amount(o["rub"], o["rate"], cur)
        ws.cell(row=row, column=1, value=o["when"].date()).number_format = "DD.MM.YYYY"
        ws.cell(row=row, column=2, value=o["when"].time().replace(second=0, microsecond=0)).number_format = "HH:MM"
        ws.cell(row=row, column=3, value=int(o["order_no"]))
        ws.cell(row=row, column=4, value=o["label"])
        ws.cell(row=row, column=5, value=o["rub"]).number_format = MONEY
        ws.cell(row=row, column=6, value=float(amount)).number_format = coin_format(cur) + f'" {cur}"'
        ws.cell(row=row, column=7, value=o["rate"]).number_format = RATE
        c = per_coin.setdefault(cur, {"amount": Decimal(0), "rub": 0, "now": 0, "has_now": False})
        c["amount"] += amount
        c["rub"] += o["rub"]
        if o["now_rate"]:
            now_rub = int((amount * Decimal(str(o["now_rate"]))).to_integral_value())
            ws.cell(row=row, column=8, value=float(o["now_rate"])).number_format = RATE
            ws.cell(row=row, column=9, value=now_rub).number_format = MONEY
            ws.cell(row=row, column=10, value=now_rub - o["rub"]).number_format = '+#,##0" ₽";-#,##0" ₽";0" ₽"'
            c["now"] += now_rub
            c["has_now"] = True
        if o.get("archived"):
            ws.cell(row=row, column=11, value="сброшен")
        row += 1
    if not orders:
        ws.cell(row=row, column=1, value="Оплат криптовалютой в этом периоде нет.").font = Font(italic=True)
        return
    row += 1
    ws.cell(row=row, column=1, value="Итого по монетам").font = Font(bold=True, size=12)
    row += 1
    for col, name in enumerate(["Монета", "", "", "", "Сумма заказов, ₽", "Получено монет", "", "Курс на выгрузку",
                                "По курсу выгрузки, ₽", "Разница, ₽"], 1):
        ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True)
        if name:
            cell = ws.cell(row=row, column=col, value=name)
            cell.font, cell.fill = bold, head_fill
    row += 1
    for cur, c in sorted(per_coin.items()):
        ws.cell(row=row, column=1, value=cur).font = bold
        ws.cell(row=row, column=5, value=c["rub"]).number_format = MONEY
        ws.cell(row=row, column=6, value=float(c["amount"])).number_format = coin_format(cur) + f'" {cur}"'
        if c["has_now"] and rates.get(cur):
            ws.cell(row=row, column=8, value=float(rates[cur])).number_format = RATE
            ws.cell(row=row, column=9, value=c["now"]).number_format = MONEY
            ws.cell(row=row, column=10, value=c["now"] - c["rub"]).number_format = '+#,##0" ₽";-#,##0" ₽";0" ₽"'
        row += 1
    ws.freeze_panes = "A4"


# ---------------------------------------------------------------- полная выгрузка
STATUS_RU = {
    "new": "Активен (ждёт курьера)", "accepted": "В работе", "in_transit": "В пути",
    "delivered": "Доставлен", "cancelled": "Отменён", "not_delivered": "Не доставлен",
    "disputed": "Спорный", "disputed_closed": "Спорный — закрыт",
}
PAY_FULL = {"cash_on_delivery": "Наличные", "sbp": "СБП", "crypto": "Крипта"}


def rating_text(avg: float | None, count: int) -> str:
    """«4.8 (12 оценок)» — среднее всех оценок с одним знаком после запятой."""
    if not count or avg is None:
        return "нет оценок"
    word = "оценка" if count % 10 == 1 and count % 100 != 11 else (
        "оценки" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "оценок")
    return f"{avg:.1f} ({count} {word})"


def _at(value: str | None) -> str:
    return f"{msk(value):%d.%m.%Y %H:%M}" if value else ""


def _nick(username: str | None) -> str:
    u = (username or "").strip().lstrip("@")
    return f"@{u}" if u else ""


def _orders_unified(full: dict[str, Any]) -> list[dict[str, Any]]:
    """Текущие заказы и заказы из сброшенной истории — в одном виде."""
    out: list[dict[str, Any]] = []
    for o in full["orders"]:
        parts = [f"{i['product_name']}{' (замена)' if i['variant'] == 'exchange' else ''} × {i['quantity']}" for i in o["items"]]
        lift = int(o.get("floor_lift_floors") or 0)
        if lift:
            parts.append("Подъём на этаж (услуга)")
        for r in o.get("returns") or []:  # 01.11.2026: возврат пустых баллонов в обычном заказе
            parts.append(f"♻️ Возврат: {r['product_name']} × {r['quantity']}")
        crypto = ""
        if o.get("payment_method") == "crypto" and o.get("crypto_currency"):
            crypto = o["crypto_currency"] + (f" · {o['crypto_network']}" if o.get("crypto_network") else "")
        n_new = sum(int(i["quantity"]) for i in o["items"] if i["variant"] != "exchange")
        n_exch = sum(int(i["quantity"]) for i in o["items"] if i["variant"] == "exchange")
        lift_rub = lift * int(o.get("floor_lift_price") or 0)
        out.append({
            "region": REGION_LABELS.get(o.get("region") or "", ""),
            "delivery_rub": int(o.get("delivery_fee") or 0),
            "bonus_spent": int(o.get("bonus_spent") or 0),
            "points_spent": int(o.get("points_spent") or 0),
            "bonus_accrued": int(full.get("bonus_by_order", {}).get(int(o["id"]), {}).get("accrual", 0)),
            "n_new": n_new, "n_exch": n_exch, "lift_floors": lift or None, "had_lift": bool(lift), "lift_rub": lift_rub,
            "items_rub": sum(int(i["quantity"]) * int(i["unit_price"]) for i in o["items"]),
            "coin": o.get("crypto_currency") if o.get("payment_method") == "crypto" else None,
            "network": o.get("crypto_network") if o.get("payment_method") == "crypto" else None,
            "order_no": o["id"], "created_at": o["created_at"], "status": o["status"],
            "closed_at": o.get("updated_at") if o["status"] in ("delivered", "cancelled", "disputed_closed", "not_delivered") else None,
            "user_id": o.get("user_id"), "customer_name": o.get("customer_name"), "username": o.get("customer_username"),
            "phone": o.get("customer_phone"), "address": o.get("delivery_address"),
            "items": parts, "item_rows": [(f"{i['product_name']}{' (замена)' if i['variant'] == 'exchange' else ''}", int(i["quantity"])) for i in o["items"]] + ([("Подъём на этаж (услуга)", 1)] if lift else []),
            "total": int(o.get("total") or 0),
            "payment": PAY_FULL.get(o.get("payment_method") or "", o.get("payment_method") or ""), "crypto": crypto,
            "courier_id": o.get("courier_id"), "courier_name": o.get("courier_name"),
            "courier_rating": o.get("courier_rating"), "customer_rating": o.get("customer_rating"),
            "comments": o.get("review_comments") or "", "archived": False, "wiped_at": None,
        })
    groups: dict[tuple, dict[str, Any]] = {}
    for a in full["archive"]:
        key = (a["wipe_id"], a["order_no"])
        g = groups.get(key)
        if g is None:
            crypto = ""
            if a.get("payment_method") == "crypto" and a.get("crypto_currency"):
                crypto = a["crypto_currency"] + (f" · {a['crypto_network']}" if a.get("crypto_network") else "")
            g = groups[key] = {
                "bonus_spent": 0, "points_spent": 0, "bonus_accrued": int(a.get("bonus_accrued") or 0), "delivery_rub": 0, "region": "",
                "n_new": 0, "n_exch": 0, "lift_floors": a.get("floor_lift_floors") or None, "had_lift": False,
                "lift_rub": 0, "items_rub": 0,
                "coin": a.get("crypto_currency") if a.get("payment_method") == "crypto" else None,
                "network": a.get("crypto_network") if a.get("payment_method") == "crypto" else None,
                "order_no": a["order_no"], "created_at": a["order_created_at"], "status": a["order_status"],
                "closed_at": a.get("order_closed_at") if a["order_status"] in ("delivered", "cancelled", "disputed_closed", "not_delivered") else None,
                "user_id": a.get("user_id"), "customer_name": a.get("customer_name"), "username": a.get("customer_username"),
                "phone": a.get("customer_phone"), "address": a.get("delivery_address"),
                "items": [], "item_rows": [], "total": 0,
                "payment": PAY_FULL.get(a.get("payment_method") or "", a.get("payment_method") or ""), "crypto": crypto,
                "courier_id": a.get("courier_id"), "courier_name": a.get("courier_name"),
                "courier_rating": a.get("courier_rating"), "customer_rating": a.get("customer_rating"),
                "comments": "", "archived": True, "wiped_at": a["wiped_at"],
            }
        qty = int(a["quantity"])
        if a["item_name"].startswith("Оплата баллами"):
            g["points_spent"] += -int(a["line_total"])
            g["total"] += int(a["line_total"])
            continue
        if a["item_name"].startswith("Оплата бонусами"):
            g["bonus_spent"] += -int(a["line_total"])
            g["total"] += int(a["line_total"])
            continue
        if a["item_name"].startswith("Доставка"):
            g["delivery_rub"] += int(a["line_total"])
            g["total"] += int(a["line_total"])
            g["items"].append(f"Доставка за пределы Москвы — {int(a['line_total'])} ₽")
            continue
        if a["item_name"].startswith("Подъём"):
            g["had_lift"] = True
            g["lift_rub"] += int(a["line_total"])
        else:
            g["items_rub"] += int(a["line_total"])
            g["n_exch" if a["item_name"].endswith(" (замена)") else "n_new"] += qty
        g["items"].append(a["item_name"] if a["item_name"].startswith("Подъём") else f"{a['item_name']} × {qty}")
        g["item_rows"].append((a["item_name"], qty))
        g["total"] += int(a["line_total"])
    out += list(groups.values())
    out.sort(key=lambda x: (parse_iso(x["created_at"]), x["order_no"]))
    return out


def _sheet(wb: Workbook, title: str, intro: str, headers: list[tuple[str, int]], header_row: int = 3):
    ws = wb.create_sheet(title)
    ws.cell(row=1, column=1, value=intro).font = Font(bold=True, size=12)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    fill = PatternFill("solid", fgColor="FFE8D2")
    for col, (name, width) in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col, value=name)
        cell.font, cell.fill = Font(bold=True), fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[header_row].height = 46
    ws.freeze_panes = f"A{header_row + 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return ws


_WRAP_TOP = Alignment(wrap_text=True, vertical="top")
_LIGHT = PatternFill("solid", fgColor="F7F7F7")


def _rows(ws, rows: list[list[Any]], money_cols: tuple[int, ...] = (), stripe: bool = True, header_row: int = 3) -> None:
    # Один и тот же объект стиля на все ячейки — openpyxl не создаёт тысячи
    # одинаковых стилей (выгрузка большой истории быстрее в несколько раз).
    money = set(money_cols)
    # Стиль считаем один раз для 4 вариантов (деньги / полоса) и копируем готовый
    # массив стиля в ячейку: присваивание объектов стиля в openpyxl хеширует их
    # заново для каждой ячейки и на большой истории занимало секунды.
    templates: dict[tuple[bool, bool], Any] = {}

    def style_for(is_money: bool, striped: bool):
        key = (is_money, striped)
        if key not in templates:
            tpl = ws.cell(row=1, column=1)  # временно — стиль заголовка восстановим ниже
            saved = copy(tpl._style)
            tpl.alignment = _WRAP_TOP
            if is_money:
                tpl.number_format = MONEY
            if striped:
                tpl.fill = _LIGHT
            templates[key] = copy(tpl._style)
            tpl._style = saved
        return templates[key]

    for r, values in enumerate(rows, header_row + 1):
        striped = stripe and r % 2 == 1
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell._style = copy(style_for(c in money, striped))
    if rows:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(rows[0]))}{header_row + len(rows)}"


def _full_sheets(wb: Workbook, full: dict[str, Any]) -> None:
    orders = _orders_unified(full)

    # ---------- «Все заказы»: каждый заказ любого статуса, одно значение в ячейке
    users_by_id = {int(u["telegram_id"]): u for u in full["users"]}
    partner_by_id = {
        int(p["user_id"]): " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or f"ID {p['user_id']}"
        for p in full.get("partners", [])
    }
    headers = [
        ("Дата", 11), ("Время", 8), ("Месяц", 17), ("День недели", 9), ("Заказ №", 10), ("Номер заказа", 18), ("Статус", 16),
        ("Доставка", 12), ("Подъём", 13), ("Этажей подъёма", 10),
        ("Новых, шт", 9), ("На замену, шт", 10), ("Баллонов всего", 10),
        ("Товары, ₽", 11), ("Подъём, ₽", 10), ("Доставка, ₽", 10), ("Бонусами, ₽", 10), ("Баллами, ₽", 10), ("Итого, ₽", 11),
        ("Начислено бонусов", 10),
        ("Оплата", 10), ("Монета", 9), ("Сеть", 11),
        ("Покупатель", 20), ("Telegram", 14), ("Telegram ID", 12), ("Телефон", 15), ("Адрес", 32), ("Регион", 10),
        ("Пришёл от партнёра", 16), ("Что заказано", 30),
        ("Курьер", 15), ("Оценка курьеру", 9), ("Оценка покупателю", 10), ("Закрыт", 15),
        ("Комментарии курьера", 30), ("Сброшен", 9), ("Дата сброса", 15), ("Период", 24),
    ]
    resets = full.get("resets") or []
    H = {name: i + 1 for i, (name, _) in enumerate(headers)}
    header_row = 6
    ws = _sheet(wb, "Все заказы", "Все заказы за всё время — любого статуса, включая сброшенные из истории",
                headers, header_row=header_row)
    rows = []
    for o in orders:
        when = msk(o["created_at"])
        u = users_by_id.get(int(o["user_id"])) if o.get("user_id") else None
        ref = int(u["referred_by"]) if u and u.get("referred_by") else None
        n_all = o["n_new"] + o["n_exch"]
        rows.append([
            when.date(), when.time().replace(second=0, microsecond=0), month_text(when.date()), WEEKDAYS_RU[when.weekday()],
            int(o["order_no"]), order_number(o.get("user_id"), o["order_no"]), STATUS_RU.get(o["status"], o["status"]),
            1 if o["status"] == "delivered" else 0, 1 if o["had_lift"] else 0, o["lift_floors"] or "",
            o["n_new"], o["n_exch"], n_all,
            o["items_rub"], o["lift_rub"], o["delivery_rub"], o["bonus_spent"], o["points_spent"], o["total"], o["bonus_accrued"],
            o["payment"], o["coin"] or "", o["network"] or "",
            o["customer_name"] or ("нет данных" if o["archived"] else ""), _nick(o["username"]), o.get("user_id") or "",
            o["phone"] or "", o["address"] or "", o["region"],
            (partner_by_id.get(ref, f"ID {ref}") if ref else ""), "\n".join(o["items"]),
            o["courier_name"] or "", o["courier_rating"] or "", o["customer_rating"] or "", _at(o.get("closed_at")),
            o["comments"], 1 if o["archived"] else 0, _at(o["wiped_at"]) if o["archived"] else "",
            period_label(o["created_at"], resets),
        ])
    _rows(ws, rows, money_cols=(H["Товары, ₽"], H["Подъём, ₽"], H["Доставка, ₽"], H["Бонусами, ₽"], H["Баллами, ₽"], H["Итого, ₽"]),
          header_row=header_row)
    first, last = header_row + 1, header_row + len(rows)
    for r in range(first, last + 1):
        ws.cell(row=r, column=H["Дата"]).number_format = "DD.MM.YYYY"
        ws.cell(row=r, column=H["Время"]).number_format = "HH:MM"
        for name in ("Доставка", "Подъём", "Сброшен"):
            ws.cell(row=r, column=H[name]).number_format = YES_NO
    ws.cell(row=2, column=1, value=(
        "Фильтры — стрелки в заголовках: по дате, месяцу, статусу, доставке и подъёму (да/нет), оплате, покупателю, курьеру. "
        "Итог «по выборке» пересчитывается под фильтр. «Итого» = товары + подъём + доставка − бонусы − баллы (к оплате деньгами). "
        "У заказов, сброшенных до 20.10.2026, нет данных о покупателе, адресе и числе этажей — тогда их не сохраняли."
    )).font = Font(italic=True, color="666666", size=9)
    sums = {  # столбец → формат итога
        "Заказ №": '0" зак."', "Доставка": '0" дост."', "Подъём": '0" подъём."', "Этажей подъёма": None,
        "Новых, шт": None, "На замену, шт": None, "Баллонов всего": None,
        "Товары, ₽": MONEY, "Подъём, ₽": MONEY, "Доставка, ₽": MONEY, "Бонусами, ₽": MONEY, "Баллами, ₽": MONEY, "Итого, ₽": MONEY,
        "Начислено бонусов": None,
    }
    totals_all = {
        "Заказ №": len(rows), "Доставка": sum(r[H["Доставка"] - 1] for r in rows),
        "Подъём": sum(r[H["Подъём"] - 1] for r in rows),
        "Этажей подъёма": sum(int(r[H["Этажей подъёма"] - 1] or 0) for r in rows),
        **{name: sum(r[H[name] - 1] for r in rows) for name in
           ("Новых, шт", "На замену, шт", "Баллонов всего", "Товары, ₽", "Подъём, ₽", "Доставка, ₽", "Бонусами, ₽", "Баллами, ₽", "Итого, ₽",
            "Начислено бонусов")},
    }
    sel_fill, all_fill = PatternFill("solid", fgColor="FFE8D2"), PatternFill("solid", fgColor="FFF7EE")
    for r, label, fill in ((4, "ИТОГО ПО ВЫБОРКЕ (фильтр)", sel_fill), (5, "ИТОГО ВСЁ", all_fill)):
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=H["Заказ №"] - 1)
        for name, fmt in sums.items():
            col = H[name]
            letter = get_column_letter(col)
            if r == 4:
                func = 103 if name == "Заказ №" else 109  # 103 — количество видимых, 109 — сумма видимых
                value = f"=SUBTOTAL({func},{letter}{first}:{letter}{max(last, first)})"
            else:
                value = totals_all[name]
            cell = ws.cell(row=r, column=col, value=value)
            cell.font = Font(bold=True)
            if fmt:
                cell.number_format = fmt
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = fill

    # ---------- «Клиенты»
    users = {int(u["telegram_id"]): u for u in full["users"]}
    partner_names = {
        int(p["user_id"]): " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or f"ID {p['user_id']}"
        for p in full.get("partners", [])
    }
    reviews_by_user: dict[int, list[dict[str, Any]]] = {}
    for rv in full["reviews"]:
        reviews_by_user.setdefault(int(rv["user_id"]), []).append(rv)
    courier_ids = {int(c["telegram_id"]) for c in full["couriers"] if c.get("telegram_id")}
    by_user: dict[int, list[dict[str, Any]]] = {}
    for o in orders:
        if o["user_id"] is not None:
            by_user.setdefault(int(o["user_id"]), []).append(o)
    ws = _sheet(wb, "Клиенты", "Клиенты: контакты, что заказывали, адреса, рейтинг и комментарии курьеров", [
        ("Покупатель", 22), ("Telegram", 16), ("Телефоны", 16), ("Telegram ID", 13), ("Заказов", 8), ("Доставлено", 10),
        ("Отменено / не доставлено", 12), ("Сумма покупок, ₽", 13),
        ("Новых баллонов", 9), ("Баллонов на замену", 9), ("Подъёмов", 9),
        ("Бонусы, %", 9), ("Остаток бонусов", 10), ("Начислено бонусов", 10), ("Потрачено бонусов", 10),
        ("Отправил друзьям бонусов", 10), ("Получил от друзей бонусов", 10),
        ("Первый заказ", 15), ("Последний заказ", 15),
        ("Рейтинг от курьеров", 16), ("Что заказывал (доставлено)", 32), ("Адреса", 40), ("Комментарии курьеров", 44),
        ("Оценка админа", 9), ("Заметка админа", 24), ("Чёрный список", 18), ("Пришёл по ссылке партнёра", 18),
    ])
    rows = []
    for uid in sorted(set(users) | set(by_user), key=lambda u: -sum(o["total"] for o in by_user.get(u, []) if o["status"] == "delivered")):
        u = users.get(uid, {})
        os_ = by_user.get(uid, [])
        if not os_ and uid in courier_ids:
            continue  # курьер без своих заказов — он на листе «Курьеры»
        delivered = [o for o in os_ if o["status"] == "delivered"]
        name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or next((o["customer_name"] for o in os_ if o["customer_name"]), "")
        phones = list(dict.fromkeys([p for p in [o["phone"] for o in reversed(os_)] + [u.get("last_phone")] if p]))
        addresses = list(dict.fromkeys(o["address"] for o in reversed(os_) if o["address"]))
        bought: dict[str, int] = {}
        for o in delivered:
            for item, qty in o["item_rows"]:
                bought[item] = bought.get(item, 0) + qty
        revs = reviews_by_user.get(uid, [])
        rated = [int(r["rating"]) for r in revs if r.get("rating")]
        notes = []
        for r in reversed(revs):
            if not r.get("comment"):
                continue
            stars = f" ({r['rating']}★)" if r.get("rating") else ""
            notes.append(f"{_at(r['created_at'])} — {r.get('courier_name') or 'курьер'}{stars}: {r['comment']}")
        rows.append([
            name or "—", _nick(u.get("username") or next((o["username"] for o in os_ if o["username"]), None)),
            "\n".join(phones), uid, len(os_), len(delivered),
            sum(1 for o in os_ if o["status"] in ("cancelled", "not_delivered", "disputed", "disputed_closed")),
            sum(o["total"] for o in delivered),
            sum(o["n_new"] for o in delivered), sum(o["n_exch"] for o in delivered),
            sum(1 for o in delivered if o["had_lift"]),
            float(u["bonus_percent"]) if u.get("bonus_percent") is not None else float(full.get("bonus_default_percent", 2)),
            full.get("bonus_balances", {}).get(uid, 0),
            full.get("bonus_totals", {}).get(uid, {}).get("earned", 0),
            full.get("bonus_totals", {}).get(uid, {}).get("spent", 0),
            full.get("bonus_totals", {}).get(uid, {}).get("gifted", 0),
            full.get("bonus_totals", {}).get(uid, {}).get("gifts", 0),
            _at(os_[0]["created_at"]) if os_ else "", _at(os_[-1]["created_at"]) if os_ else "",
            rating_text(sum(rated) / len(rated) if rated else None, len(rated)),
            "\n".join(f"{k} × {v}" for k, v in sorted(bought.items(), key=lambda kv: -kv[1])),
            "\n".join(addresses), "\n".join(notes),
            u.get("admin_rating") or "", u.get("admin_note") or "",
            ("да" + (f": {u['blocked_reason']}" if u.get("blocked_reason") else "")) if u.get("is_blocked") else "",
            partner_names.get(int(u["referred_by"]), f"ID {u['referred_by']}") if u.get("referred_by") else "",
        ])
    _rows(ws, rows, money_cols=(8,))
    ws.cell(row=2, column=1, value="Рейтинг — среднее всех оценок курьеров, с одним знаком после запятой. Отсортировано по сумме покупок.").font = Font(italic=True, color="666666", size=9)

    # ---------- «Курьеры»: только сводка по каждому курьеру (без длинных списков в ячейках)
    by_courier: dict[int, list[dict[str, Any]]] = {}
    for o in orders:
        if o["courier_id"] is not None:
            by_courier.setdefault(int(o["courier_id"]), []).append(o)
    ws = _sheet(wb, "Курьеры", "Курьеры: контакты, рейтинг и итоги за всё время", [
        ("Курьер", 20), ("Telegram", 16), ("Телефон", 15), ("Telegram ID", 13), ("Статус", 11), ("Рейтинг", 16),
        ("Доставлено заказов", 11), ("На сумму, ₽", 12),
        ("Новых баллонов", 9), ("Баллонов на замену", 9), ("Подъёмов", 9), ("Рабочих дней", 9),
        ("Первая доставка", 15), ("Последняя доставка", 15),
    ])
    rows = []
    for c in full["couriers"]:
        cid = int(c["id"])
        delivered = [o for o in by_courier.get(cid, []) if o["status"] == "delivered"]
        count = int(c.get("rating_count") or 0)
        rows.append([
            c["name"], _nick(c.get("username")), c.get("phone") or "", c.get("telegram_id") or "",
            "активен" if c.get("is_active") else "неактивен",
            rating_text(int(c.get("rating_sum") or 0) / count if count else None, count),
            len(delivered), sum(o["total"] for o in delivered),
            sum(o["n_new"] for o in delivered), sum(o["n_exch"] for o in delivered),
            sum(1 for o in delivered if o["had_lift"]),
            len({_trip_when(o).date() for o in delivered}),
            _at(delivered[0]["created_at"]) if delivered else "", _at(delivered[-1]["created_at"]) if delivered else "",
        ])
    _rows(ws, rows, money_cols=(8,))
    ws.cell(row=2, column=1, value=(
        "Рейтинг — среднее всех оценок покупателей за всё время. Что и куда возил в конкретный день — "
        "листы «Курьеры по дням» (итог дня) и «Курьеры — доставки» (каждый адрес)."
    )).font = Font(italic=True, color="666666", size=9)

    names = {int(c["id"]): c["name"] for c in full["couriers"]}
    trips = sorted(
        (o for o in orders if o["courier_id"] is not None and o["status"] == "delivered"),
        # курьер → дни (свежие сверху) → внутри дня по порядку маршрута
        key=lambda o: (names.get(int(o["courier_id"]), o["courier_name"] or "").lower(),
                       -_trip_when(o).date().toordinal(), _trip_when(o)),
    )
    _courier_days_sheet(wb, trips, names)
    _courier_trips_sheet(wb, trips, names)


def period_label(at: str | None, resets: list[dict[str, Any]]) -> str:
    """Таблица №1 (28.10.2026): к какому периоду относится запись. Период меняется при каждом
    «⬇️🗑 Скачать и обнулить»; без сбросов — пустая ячейка."""
    if not resets or not at:
        return ""
    t = parse_iso(at)
    n = sum(1 for r in resets if parse_iso(r["at"]) <= t)
    if n == 0:
        return f"Период 1 (до {msk(resets[0]['at']):%d.%m.%Y %H:%M})"
    since = f"с {msk(resets[n - 1]['at']):%d.%m.%Y %H:%M}"
    until = f" до {msk(resets[n]['at']):%d.%m.%Y %H:%M}" if n < len(resets) else ""
    return f"Период {n + 1} ({since}{until})"


def _resets_sheet(wb: Workbook, full: dict[str, Any]) -> None:
    resets = full.get("resets") or []
    ws = _sheet(wb, "Сбросы", "Сбросы таблицы №2 («Скачать и обнулить») — границы периодов в этой таблице", [
        ("Период", 10), ("С", 18), ("До", 18), ("Кто обнулил", 24),
    ], header_row=3)
    rows = []
    for i in range(len(resets) + 1):
        since = msk(resets[i - 1]["at"]) if i else None
        until = msk(resets[i]["at"]) if i < len(resets) else None
        who = (resets[i]["admin_name"] or f"ID {resets[i]['admin_id']}") if i < len(resets) else "идёт сейчас"
        rows.append([i + 1, f"{since:%d.%m.%Y %H:%M}" if since else "с первого заказа",
                     f"{until:%d.%m.%Y %H:%M}" if until else "—", who])
    _rows(ws, rows, header_row=3)
    ws.cell(row=2, column=1, value="Эта таблица (№1) не обнуляется. После каждого сброса таблицы №2 здесь начинается новый период.").font = Font(italic=True, color="666666", size=9)


def _trip_when(o: dict[str, Any]) -> datetime:
    """Когда доставлен (МСК): время закрытия заказа, а если его нет — время оформления."""
    return msk(o.get("closed_at") or o["created_at"])


def _totals_rows(ws, headers: list[tuple[str, int]], sums: dict[str, str | None], data_rows: list[list[Any]],
                 header_row: int, label_cols: int) -> None:
    """Строки 4–5: «ИТОГО ПО ВЫБОРКЕ» (пересчитывается под фильтр) и «ИТОГО ВСЁ»."""
    H = {name: i + 1 for i, (name, _) in enumerate(headers)}
    first, last = header_row + 1, header_row + max(len(data_rows), 1)
    sel_fill, all_fill = PatternFill("solid", fgColor="FFE8D2"), PatternFill("solid", fgColor="FFF7EE")
    for r, label, fill in ((4, "ИТОГО ПО ВЫБОРКЕ (фильтр)", sel_fill), (5, "ИТОГО ВСЁ", all_fill)):
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=label_cols)
        for name, fmt in sums.items():
            col = H[name]
            letter = get_column_letter(col)
            value = (f"=SUBTOTAL(109,{letter}{first}:{letter}{last})" if r == 4
                     else sum(int(row[col - 1] or 0) for row in data_rows))
            cell = ws.cell(row=r, column=col, value=value)
            cell.font = Font(bold=True)
            if fmt:
                cell.number_format = fmt
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).fill = fill


def _day_stripes(ws, keys: list[Any], header_row: int, ncols: int) -> None:
    """Один рабочий день курьера — одним цветом: дни чередуются белым и голубым."""
    band = PatternFill("solid", fgColor="EAF3FB")
    banded: dict[tuple, Any] = {}  # стиль ячейки → тот же стиль с голубой заливкой (считаем один раз)
    shade, prev = False, object()
    for i, key in enumerate(keys):
        if key != prev:
            shade, prev = not shade, key
        if not shade:
            continue  # строки «белого» дня — без заливки (как и были после _rows с stripe=False)
        for col in range(1, ncols + 1):
            cell = ws.cell(row=header_row + 1 + i, column=col)
            k = tuple(cell._style)
            if k not in banded:
                cell.fill = band
                banded[k] = copy(cell._style)
            else:
                cell._style = copy(banded[k])


def _courier_days_sheet(wb: Workbook, trips: list[dict[str, Any]], names: dict[int, str]) -> None:
    """«Курьеры по дням»: строка = курьер + день. Выбрали курьера и дату в фильтре — видно весь день."""
    headers = [
        ("Курьер", 18), ("Дата", 11), ("День недели", 8), ("Доставок", 9), ("Новых, шт", 9), ("На замену, шт", 10),
        ("Баллонов всего", 10), ("Подъёмов", 9), ("Сумма, ₽", 12), ("Наличными, ₽", 12), ("СБП, ₽", 11), ("Крипта, ₽", 11),
        ("Первая доставка", 9), ("Последняя доставка", 9), ("Адреса за день", 50),
    ]
    header_row = 6
    ws = _sheet(wb, "Курьеры по дням", "Курьеры по дням: сколько и что отвёз каждый курьер за день", headers, header_row=header_row)
    days: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for o in trips:
        days.setdefault((names.get(int(o["courier_id"]), o["courier_name"] or "—"), _trip_when(o).date()), []).append(o)
    rows = []
    for (name, day), os_ in days.items():
        times = sorted(_trip_when(o) for o in os_)
        pay = lambda label: sum(o["total"] for o in os_ if o["payment"] == label)  # noqa: E731
        rows.append([
            name, day, WEEKDAYS_RU[day.weekday()], len(os_),
            sum(o["n_new"] for o in os_), sum(o["n_exch"] for o in os_), sum(o["n_new"] + o["n_exch"] for o in os_),
            sum(1 for o in os_ if o["had_lift"]), sum(o["total"] for o in os_),
            pay("Наличные"), pay("СБП"), pay("Крипта"),
            times[0].time().replace(second=0, microsecond=0), times[-1].time().replace(second=0, microsecond=0),
            "\n".join(f"{_trip_when(o):%H:%M} — {o['address'] or 'адрес не сохранён'}" for o in sorted(os_, key=_trip_when)),
        ])
    H = {n: i + 1 for i, (n, _) in enumerate(headers)}
    _rows(ws, rows, money_cols=tuple(H[n] for n in ("Сумма, ₽", "Наличными, ₽", "СБП, ₽", "Крипта, ₽")),
          stripe=False, header_row=header_row)
    for r in range(header_row + 1, header_row + 1 + len(rows)):
        ws.cell(row=r, column=H["Дата"]).number_format = "DD.MM.YYYY"
        for n in ("Первая доставка", "Последняя доставка"):
            ws.cell(row=r, column=H[n]).number_format = "HH:MM"
    _day_stripes(ws, [(r[0], r[1]) for r in rows], header_row, len(headers))
    _totals_rows(ws, headers, {
        "Доставок": '0" дост."', "Новых, шт": None, "На замену, шт": None, "Баллонов всего": None, "Подъёмов": None,
        "Сумма, ₽": MONEY, "Наличными, ₽": MONEY, "СБП, ₽": MONEY, "Крипта, ₽": MONEY,
    }, rows, header_row, label_cols=3)
    ws.cell(row=2, column=1, value=(
        "Как смотреть день курьера: стрелка в «Курьер» — выберите курьера, стрелка в «Дата» — выберите день. "
        "Строка «по выборке» пересчитается. Каждый адрес отдельно — лист «Курьеры — доставки». Только доставленные заказы."
    )).font = Font(italic=True, color="666666", size=9)


def _courier_trips_sheet(wb: Workbook, trips: list[dict[str, Any]], names: dict[int, str]) -> None:
    """«Курьеры — доставки»: строка = одна доставка; фильтр «Курьер» + «Дата» = маршрут за день."""
    headers = [
        ("Курьер", 18), ("Дата", 11), ("Время", 7), ("Заказ №", 9), ("Номер заказа", 15), ("Адрес", 40), ("Регион", 10),
        ("Покупатель", 20), ("Телефон", 15), ("Что отвёз", 26), ("Новых, шт", 8), ("На замену, шт", 9),
        ("Подъём", 8), ("Оплата", 10), ("Сумма, ₽", 11), ("В т.ч. доставка, ₽", 10),
        ("Оценка от покупателя", 9), ("Комментарий курьера", 30),
    ]
    header_row = 6
    ws = _sheet(wb, "Курьеры — доставки", "Курьеры — доставки: каждый адрес, что и кому отвезли", headers, header_row=header_row)
    rows = []
    for o in trips:
        when = _trip_when(o)
        rows.append([
            names.get(int(o["courier_id"]), o["courier_name"] or "—"), when.date(), when.time().replace(second=0, microsecond=0),
            int(o["order_no"]), order_number(o.get("user_id"), o["order_no"]), o["address"] or "адрес не сохранён", o["region"],
            o["customer_name"] or "", o["phone"] or "",
            "\n".join(f"{k} × {v}" for k, v in o["item_rows"] if not k.startswith("Подъём")),
            o["n_new"], o["n_exch"], 1 if o["had_lift"] else 0, o["payment"], o["total"], o["delivery_rub"],
            o["courier_rating"] or "", o["comments"],
        ])
    H = {n: i + 1 for i, (n, _) in enumerate(headers)}
    _rows(ws, rows, money_cols=(H["Сумма, ₽"], H["В т.ч. доставка, ₽"]), stripe=False, header_row=header_row)
    for r in range(header_row + 1, header_row + 1 + len(rows)):
        ws.cell(row=r, column=H["Дата"]).number_format = "DD.MM.YYYY"
        ws.cell(row=r, column=H["Время"]).number_format = "HH:MM"
        ws.cell(row=r, column=H["Подъём"]).number_format = YES_NO
    _day_stripes(ws, [(r[0], r[1]) for r in rows], header_row, len(headers))
    _totals_rows(ws, headers, {
        "Новых, шт": None, "На замену, шт": None, "Подъём": '0" подъём."', "Сумма, ₽": MONEY, "В т.ч. доставка, ₽": MONEY,
    }, rows, header_row, label_cols=3)
    ws.cell(row=2, column=1, value=(
        "Маршрут курьера за день: стрелка в «Курьер» — выберите курьера, в «Дата» — день. Один день — одним цветом. "
        "Только доставленные заказы; время — когда заказ закрыт."
    )).font = Font(italic=True, color="666666", size=9)


def _admins_sheet(wb: Workbook, full: dict[str, Any]) -> None:
    """«Администраторы»: кто админ (ADMIN_ID) и журнал их действий — кто кому начислил
    или списал бонусы, баллы партнёрам, чёрный список, стоимость доставки."""
    actions = full.get("admin_actions", [])
    current = list(full.get("admin_ids", []))
    users = {int(u["telegram_id"]): u for u in full["users"]}
    ids = current + sorted({int(a["admin_id"]) for a in actions} - set(current))
    list_head = [("Telegram ID", 13), ("Администратор", 22), ("Telegram", 16), ("Статус", 14),
                 ("Начислил бонусов", 11), ("Списал бонусов", 11), ("Всего действий", 9), ("Последнее действие", 16)]
    log_head = [("Дата", 11), ("Время", 7), ("Администратор", 22), ("Telegram ID админа", 13), ("Действие", 26),
                ("Клиент / партнёр", 22), ("Telegram ID клиента", 13), ("Заказ №", 8), ("Бонусы / баллы / ₽", 11),
                ("Остаток после", 10), ("Причина / детали", 34)]
    header_row = 4 + len(ids) + 3  # список админов сверху, журнал — ниже
    ws = _sheet(wb, "Администраторы", "Администраторы и их действия", log_head, header_row=header_row)
    fill = PatternFill("solid", fgColor="FFE8D2")
    ws.cell(row=2, column=1, value=(
        "Администраторы — из переменной ADMIN_ID в Amvera (до 20, через запятую). Имя появляется, когда админ "
        "хоть раз откроет приложение. Ниже — журнал: кто, когда и кому начислил или списал бонусы и т. п. "
        "Фильтр — стрелки в заголовках журнала."
    )).font = Font(italic=True, color="666666", size=9)
    for col, (name, _) in enumerate(list_head, 1):
        c = ws.cell(row=3, column=col, value=name)
        c.font, c.fill = Font(bold=True), fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, aid in enumerate(ids):
        u = users.get(aid, {})
        mine = [a for a in actions if int(a["admin_id"]) == aid]
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) \
            or next((a["admin_name"] for a in reversed(mine) if a.get("admin_name")), "") or "ещё не заходил в приложение"
        bonus = [int(a["amount"] or 0) for a in mine if a["action"].startswith("Бонусы:") and a.get("amount")]
        values = [aid, name, _nick(u.get("username")), "действующий" if aid in current else "больше не админ",
                  sum(x for x in bonus if x > 0), -sum(x for x in bonus if x < 0), len(mine),
                  _at(mine[-1]["created_at"]) if mine else ""]
        for col, v in enumerate(values, 1):
            ws.cell(row=4 + i, column=col, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    ws.cell(row=header_row - 1, column=1, value="Журнал действий администраторов").font = Font(bold=True, size=11)
    rows = []
    for a in reversed(actions):  # свежие сверху
        when = msk(a["created_at"])
        rows.append([
            when.date(), when.time().replace(second=0, microsecond=0), a.get("admin_name") or "", int(a["admin_id"]),
            a["action"], a.get("target_name") or "", a.get("target_user_id") or "", a.get("order_id") or "",
            a.get("amount") if a.get("amount") is not None else "",
            a.get("balance_after") if a.get("balance_after") is not None else "", a.get("details") or "",
        ])
    _rows(ws, rows, header_row=header_row)
    for r in range(header_row + 1, header_row + 1 + len(rows)):
        ws.cell(row=r, column=1).number_format = "DD.MM.YYYY"
        ws.cell(row=r, column=2).number_format = "HH:MM"
    if not rows:
        ws.cell(row=header_row + 1, column=1, value="Пока действий нет.").font = Font(italic=True, color="666666")


def _order_chats_sheet(wb: Workbook, full: dict[str, Any]) -> None:
    """«Чаты курьер–покупатель»: вся переписка внутри заказов. В приложении чат исчезает после
    доставки или отмены заказа, здесь — остаётся (и после сброса истории заказов)."""
    head = [("Номер заказа", 15), ("Заказ №", 8), ("Статус заказа", 16), ("Дата", 11), ("Время", 7),
            ("Кто написал", 12), ("Курьер", 18), ("Покупатель", 22), ("Сообщение", 50), ("Фото", 6)]
    ws = _sheet(wb, "Чаты курьер–покупатель", "Переписка курьеров с покупателями внутри заказов", head)
    ws.cell(row=2, column=1, value=(
        "В приложении чат виден, пока заказ не доставлен; потом он удаляется из приложения и хранится здесь. "
        "Фото сохраняются в базе, здесь — отметка «да». Фильтр — стрелки в заголовках."
    )).font = Font(italic=True, color="666666", size=9)
    rows = []
    for m in full.get("order_chats", []):
        when = msk(m["created_at"])
        status = "до сброса истории" if m.get("archived") else STATUS_RU.get(m.get("order_status") or "", m.get("order_status") or "")
        rows.append([
            order_number(m.get("customer_id"), m["order_id"]), int(m["order_id"]), status,
            when.date(), when.time().replace(second=0, microsecond=0),
            "Курьер" if m.get("from_courier") else "Покупатель",
            m.get("courier_name") or "", (m.get("customer_name") or "").split(" (@")[0],
            m.get("text") or "", "да" if m.get("has_photo") else "",
        ])
    _rows(ws, rows)
    for r in range(4, 4 + len(rows)):
        ws.cell(row=r, column=4).number_format = "DD.MM.YYYY"
        ws.cell(row=r, column=5).number_format = "HH:MM"
        ws.cell(row=r, column=9).alignment = Alignment(wrap_text=True, vertical="top")
    if not rows:
        ws.cell(row=4, column=1, value="Переписки пока нет.").font = Font(italic=True, color="666666")


def _partners_sheet(wb: Workbook, full: dict[str, Any]) -> None:
    ws = _sheet(wb, "Партнёры", "Партнёрская программа: ссылки, приглашённые клиенты и баллы", [
        ("Партнёр", 22), ("Telegram", 16), ("Телефон", 15), ("Telegram ID", 13), ("Код ссылки", 13),
        ("Партнёр с", 15), ("Пришло клиентов", 10), ("Засчитано заказов", 10),
        ("Начислено", 11), ("Списано", 10), ("Остаток баллов", 11), ("Приглашённые клиенты", 40),
    ])
    invited: dict[int, list[str]] = {}
    for u in full["users"]:
        if u.get("referred_by"):
            name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or f"ID {u['telegram_id']}"
            invited.setdefault(int(u["referred_by"]), []).append(f"{name} {_nick(u.get('username'))}".strip() + f" — с {_at(u.get('referred_at'))}")
    rows = []
    for p in full.get("partners", []):
        name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or "—"
        rows.append([
            name, _nick(p.get("username")), (p.get("courier_phone") or p.get("last_phone") or ""), int(p["user_id"]),
            p.get("partner_code") or "", _at(p.get("approved_at")),
            int(p.get("invited") or 0), int(p.get("rewarded_orders") or 0),
            int(p.get("earned") or 0), int(p.get("written_off") or 0), int(p.get("balance") or 0),
            "\n".join(invited.get(int(p["user_id"]), [])),
        ])
    _rows(ws, rows)
    ws.cell(row=2, column=1, value=(
        "Баллы начисляются за каждый баллон (новый / на замену — по своей ставке) в доставленных заказах "
        "клиентов, пришедших по ссылке партнёра. Сумма заказа и подъём на этаж не важны."
    )).font = Font(italic=True, color="666666", size=9)
