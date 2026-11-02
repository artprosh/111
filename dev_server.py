"""Локальный dev-сервер Mini App — для проверки и правок БЕЗ Telegram.

Запуск:  python dev_server.py            (порт 8090, http://127.0.0.1:8090/)
         python dev_server.py --port 9000 --reset

Чем отличается от рабочего режима (main.py + webapp/server.py):
- только стандартная библиотека Python: не нужны aiogram/aiohttp и токен бота;
- та же логика API (webapp/core.py) и та же база (db.py), но отдельный файл
  data/dev.sqlite3 с демо-товарами, демо-курьером и QR-заглушками;
- вход без Telegram: вверху страницы полоса DEV — выбор роли
  (покупатель / курьер / админ), заголовок X-Dev-User;
- всё, что бот отправил бы в Telegram, пишется в лог и доступно на
  /api/dev/events — так видно, кому и что ушло бы;
- курс USDT — фиксированный ДЕМО-курс (не реальный), см. DEV_USDT_RATE.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from db import Database  # noqa: E402
from webapp.auth import InitDataError, TelegramUser, validate_init_data  # noqa: E402
from webapp.core import Api, ApiError  # noqa: E402
from webapp.version import render_index, version_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dev_server")

STATIC = ROOT / "webapp" / "static"
DEV_BOT_TOKEN = "123456:DEV-TOKEN-NOT-REAL"
DEV_USDT_RATE = 95.0  # ДЕМО-значение для локальной проверки, не реальный курс
ADMIN = TelegramUser(3003, "Админ", "Тестовый", "dev_admin")
DEV_USERS = [
    (TelegramUser(1001, "Анна", "Покупатель", "anna_dev"), "🛍 Покупатель"),
    (TelegramUser(2002, "Пётр", "Курьер", "petr_courier"), "🚴 Курьер"),
    (ADMIN, "🛠 Админ"),
    (TelegramUser(1002, "Борис", None, None), "🛍 Покупатель 2"),
]

QR_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges">
<rect width="29" height="29" fill="#fff"/>{cells}
<text x="14.5" y="16" font-size="3" text-anchor="middle" fill="#e11d48" font-family="sans-serif">DEV QR {label}</text></svg>"""


def fake_qr(label: str) -> bytes:
    import hashlib

    bits = hashlib.sha256(label.encode()).digest() * 4
    cells = []
    for y in range(29):
        for x in range(29):
            finder = any(
                x0 <= x < x0 + 7 and y0 <= y < y0 + 7 and (x in (x0, x0 + 6) or y in (y0, y0 + 6) or (x0 + 2 <= x <= x0 + 4 and y0 + 2 <= y <= y0 + 4))
                for x0, y0 in ((0, 0), (22, 0), (0, 22))
            )
            in_finder_zone = any(x0 - 1 <= x <= x0 + 7 and y0 - 1 <= y <= y0 + 7 for x0, y0 in ((0, 0), (22, 0), (0, 22)))
            on = finder or (not in_finder_zone and bits[(y * 29 + x) % len(bits)] & (1 << ((x + y) % 8)))
            if on and not (11 <= y <= 17 and 5 <= x <= 23):
                cells.append(f'<rect x="{x}" y="{y}" width="1" height="1" fill="#111"/>')
    return QR_SVG.format(cells="".join(cells), label=label).encode()


class LogBridge:
    """Вместо сообщений в Telegram — запись событий (их видно на /api/dev/events)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.proofs = 0
        self.images: dict[str, tuple[bytes, str]] = {}

    def log(self, kind: str, **data) -> None:
        with self.lock:
            self.events.append({"event": kind, **data})
        logger.info("TELEGRAM (не отправлено, dev): %s %s", kind, data)

    async def dispatch_new_order(self, order_id): self.log("dispatch_new_order", order_id=order_id)
    async def customer_cancelled(self, order_id): self.log("customer_cancelled", order_id=order_id)
    async def customer_received(self, order_id): self.log("customer_received", order_id=order_id)
    async def courier_delivered(self, order_id, prev_status): self.log("courier_delivered", order_id=order_id, prev=prev_status)
    async def courier_not_delivered(self, order_id, reason): self.log("courier_not_delivered", order_id=order_id, reason=reason)
    async def courier_location(self, order_id, lat, lon, first): self.log("courier_location", order_id=order_id, lat=lat, lon=lon, first=first)
    async def admin_status(self, order_id, prev, status): self.log("admin_status", order_id=order_id, status=status, prev=(prev or {}).get("status"))
    async def admin_assigned(self, order_id, prev): self.log("admin_assigned", order_id=order_id)
    async def order_changed(self, order_id, text): self.log("order_changed", order_id=order_id, text=text)


    async def call_admin(self, user, text): self.log("call_admin", user=user.id, text=text)
    async def support_message(self, user, text, has_photo): self.log("support_message", user=user.id, text=text, photo=has_photo)
    async def support_reply(self, user_id): self.log("support_reply", user_id=user_id)
    async def order_chat(self, order_id, to_courier, text, has_photo): self.log("order_chat", order_id=order_id, to_courier=to_courier, text=text)
    async def courier_arrived(self, order_id): self.log("courier_arrived", order_id=order_id)

    async def upload_image(self, data, mime, caption):
        self.proofs += 1
        file_id = f"dev-img-{self.proofs}"
        self.images[file_id] = (data, mime)
        self.log("upload_image", bytes=len(data), mime=mime, caption=caption)
        return file_id

    async def send_document(self, chat_id, data, filename, caption):
        self.log("send_document", chat_id=chat_id, filename=filename, bytes=len(data), caption=caption)

    async def get_crypto_rates(self, codes):
        demo = {"USDT": 95.0, "USDC": 95.0, "BTC": 9_500_000.0, "ETH": 330_000.0, "TON": 520.0,
                "TRX": 23.0, "LTC": 8_000.0, "SOL": 17_000.0, "BNB": 60_000.0}
        return {c: demo[c] for c in codes if c in demo}

    async def get_crypto_rates_now(self, codes):
        # «Курс на момент выгрузки» в dev: демо-курс USDT чуть выше, чем при заказе.
        rates = await self.get_crypto_rates(codes)
        if "USDT" in rates:
            rates["USDT"] = 100.0
        return rates, time.time()

    async def send_payment_photo(self, order_id, data, mime, caption):
        self.proofs += 1
        file_id = f"dev-payphoto-{self.proofs}"
        self.images[file_id] = (data, mime)
        self.log("send_payment_photo", order_id=order_id, bytes=len(data), caption=caption)
        return file_id

    async def test_log_group(self):
        self.log("test_log_group")
        return True
    async def customer_review(self, order_id, rating, comment): self.log("customer_review", order_id=order_id, rating=rating, comment=comment)

    async def customer_blocked(self, user_id, name, blocked, reason): self.log("customer_blocked", user_id=user_id, blocked=blocked, reason=reason)

    async def courier_released(self, order_id, prev): self.log("courier_released", order_id=order_id, courier=prev.get("courier_name"))

    async def referral_declined(self, name, kind, mode): self.log("referral_declined", program=kind, mode=mode)

    async def partner_link(self, code): return f"https://t.me/RelaxGas_bot?start={code}"
    async def bot_link(self): return "https://t.me/RelaxGas_bot"
    async def courier_live_instruction(self, telegram_id): self.log("courier_live_instruction", telegram_id=telegram_id)

    async def partner_event(self, text): self.log("partner_event", text=text)

    async def bonus_event(self, text): self.log("bonus_event", text=text)
    async def bonus_gift(self, recipient_id, text): self.log("bonus_gift", recipient_id=recipient_id, text=text)
    async def admin_alert(self, text): self.log("admin_alert", text=text)
    async def courier_message(self, telegram_id, text): self.log("courier_message", telegram_id=telegram_id, text=text)
    async def dispatch_link(self, telegram_id, url): self.log("dispatch_link", telegram_id=telegram_id, url=url)
    async def dispatch_login(self, telegram_id, session): self.log("dispatch_login", telegram_id=telegram_id, session_id=session["id"])

    async def referral_request(self, user_id, name, contact, is_courier, kind, comment=None): self.log("referral_request", user_id=user_id, is_courier=is_courier, program=kind, comment=comment)

    async def order_disputed(self, order_id): self.log("order_disputed", order_id=order_id)

    async def dispute_returned(self, order_id, prev): self.log("dispute_returned", order_id=order_id)

    async def dispute_closed(self, order_id, prev): self.log("dispute_closed", order_id=order_id)

    async def upload_payment_proof(self, data, mime, caption):
        self.proofs += 1
        self.log("upload_payment_proof", bytes=len(data), mime=mime, caption=caption)
        return f"dev-proof-{self.proofs}", f"ref-{self.proofs}"

    async def finalize_payment_proof(self, ref, caption): self.log("finalize_payment_proof", ref=ref, caption=caption)

    async def get_usdt_rate(self):
        return DEV_USDT_RATE

    async def fetch_file(self, file_id):
        if file_id in self.images:
            return self.images[file_id]
        if file_id.startswith("dev-qr-"):
            return fake_qr(file_id[7:].upper()), "image/svg+xml"
        return None


def seed(db: Database) -> None:
    if db.get_products(include_inactive=True):
        return
    p1 = db.add_product(
        "Баллон 8 л", "Пищевой газ, полный баллон.\nХватает на вечер компании.", 2500, 1900, None, None
    )
    db.add_product("Баллон 16 л", "Большой баллон для долгой вечеринки.", 4200, 3500, None, None)
    db.add_product("Шарики (50 шт.)", "Яркие шарики, разные цвета.", 450, None, None, None)
    db.add_product("Насадка-глушитель", "Тихая подача газа.", 700, None, None, None)
    db.upsert_user(2002, "petr_courier", "Пётр", "Курьер")
    db.add_courier("Пётр", "+7 900 111-22-33", 2002, "petr_courier")
    db.add_courier("Иван (без Telegram ID)", "+7 900 444-55-66", None, None)
    db.set_payment_qr("sbp", "dev-qr-sbp")
    db.set_payment_qr("crypto", "dev-qr-crypto")
    # QR для оплаты при курьере (новая схема): видят курьеры в «Принять оплату».
    db.add_payment_qr_code("sbp", "СБП — Сбер", None, None, "dev-qr-sbp")
    db.add_payment_qr_code("crypto", "USDT · TRC-20 (Tron)", "USDT", "TRC-20 (Tron)", "dev-qr-usdt")
    db.add_payment_qr_code("crypto", "BTC · Bitcoin", "BTC", "Bitcoin", "dev-qr-btc")
    db.set_floor_lift_price(150)  # демо-цена услуги «Подъём на этаж»
    logger.info("Демо-данные созданы (товар №%s и др.)", p1)


def make_handler(api: Api, bridge: LogBridge):
    dev_ids = {u.id: u for u, _ in DEV_USERS}

    class Handler(BaseHTTPRequestHandler):
        server_version = "RelaxGasDev/1.0"

        def log_message(self, fmt, *args):  # тише стандартного вывода
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def _user(self) -> TelegramUser | None:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("tma "):
                return validate_init_data(auth[4:], DEV_BOT_TOKEN)
            raw = self.headers.get("X-Dev-User")
            if raw and raw.isdigit() and int(raw) in dev_ids:
                return dev_ids[int(raw)]
            return None

        def _static(self, path: str) -> None:
            target = (STATIC / path).resolve()
            if STATIC not in target.parents or not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _dispatch(self) -> None:
            parts = urlsplit(self.path)
            path, query = parts.path, dict(parse_qsl(parts.query))
            if path == "/" and self.command in ("GET", "HEAD"):
                return self._send(200, render_index(STATIC).encode(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/export/"):
                bits = path.split("/")
                found = api.get_export(bits[2]) if len(bits) == 4 else None
                if not found:
                    return self._send(404, "Ссылка устарела".encode(), "text/plain; charset=utf-8")
                data, filename = found
                return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  {"Content-Disposition": f'attachment; filename="{filename}"'})
            if path == "/healthz":
                return self._send(200, version_text().encode(), "text/plain; charset=utf-8")
            if path == "/api/dev/info":
                return self._json(200, {"dev": True, "users": [{"id": u.id, "label": label} for u, label in DEV_USERS]})
            if path == "/api/dev/events":
                return self._json(200, {"events": bridge.events})
            if path.startswith("/api/media/"):
                bits = path.split("/")
                if len(bits) != 5:
                    return self._send(404, b"", "text/plain")
                file_id = api.media_file_id(bits[3], bits[4])
                result = asyncio.run(bridge.fetch_file(file_id)) if file_id else None
                if not result:
                    return self._send(404, b"", "text/plain")
                return self._send(200, result[0], result[1])
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            try:
                user = self._user()
            except InitDataError as exc:
                return self._json(401, {"error": f"Не удалось проверить вход: {exc}"})
            if not user:
                return self._json(401, {"error": "Откройте приложение из Telegram."})
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length > 12 * 1024 * 1024:
                return self._json(413, {"error": "Слишком большой запрос."})
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "Некорректный JSON."})
            try:
                result = asyncio.run(api.handle(self.command, path, user, body, query))
            except ApiError as exc:
                return self._json(exc.status, {"error": exc.message})
            except Exception:
                logger.exception("Ошибка в %s %s", self.command, path)
                return self._json(500, {"error": "Внутренняя ошибка. Попробуйте ещё раз."})
            return self._json(200, result)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _dispatch

    return Handler


def build(db_path: Path, reset: bool = False) -> tuple[Api, LogBridge, Database]:
    if reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    seed(db)
    bridge = LogBridge()
    api = Api(db, ADMIN.id, bridge)
    # Своя база адресов — в dev из маленького образца (tools/sample_addresses.geojsonseq).
    from webapp.geodb import AddressDB  # добавляет tools/ в путь импорта
    from build_address_db import build as build_address_db

    geo_path = db_path.parent / f"{db_path.stem}_addresses.sqlite"
    if reset or not geo_path.exists():
        build_address_db(ROOT / "tools" / "sample_addresses.geojsonseq", geo_path, "dev sample")
    api.geo = AddressDB(geo_path)
    # dev-сервер выполняет каждый запрос в отдельном asyncio.run — фоновые
    # задачи там не доживут, поэтому уведомления ждём, как раньше.
    api.background = False
    return api, bridge, db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--db", default=str(ROOT / "data" / "dev.sqlite3"))
    parser.add_argument("--reset", action="store_true", help="удалить dev-базу и создать демо-данные заново")
    args = parser.parse_args()
    api, bridge, db = build(Path(args.db), args.reset)
    if not any(p["name"] == "Шарики (5 шт.)" for p in db.get_products(include_inactive=True)):
        # 🎁 Демо бонуса-подарка (28.10.2026, только ручной dev-сервер, не автотесты): к каждому баллону 8 л —
        # «Шарики (5 шт.)» (товар каталога, есть у курьера).
        gift = db.add_product("Шарики (5 шт.)", "Маленькая пачка шариков.", 100, None, None, None)
        db.set_product_bonus(1, gift, 1, "Пачка из 5 шариков к каждому баллону.", True, True)
    api.public_url = f"http://{args.host}:{args.port}"  # ссылки «🖥 Карта для ПК» (сама карта /dispatch — только в рабочем сервере)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(api, bridge))
    logger.info("Dev-сервер Mini App: http://%s:%s/  (база %s)", args.host, args.port, args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
