"""Тесты Mini App: проверка initData и сценарии API на реальной SQLite.

Запуск (из корня репозитория, нужна только стандартная библиотека):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations
from datetime import datetime, timezone

import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dev_server  # noqa: E402
from webapp.auth import InitDataError, TelegramUser, sign_init_data, validate_init_data  # noqa: E402
from webapp.core import ApiError  # noqa: E402

TOKEN = "123456:TEST"
PNG_1PX = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()

CUSTOMER = TelegramUser(1001, "Анна", "Покупатель", "anna_dev")
CUSTOMER2 = TelegramUser(1002, "Борис")
COURIER = TelegramUser(2002, "Пётр", "Курьер", "petr_courier")
ADMIN = dev_server.ADMIN


def courier_on_shift(db):
    """Курьер Пётр на смене, с запасом всех товаров (28.10.2026: заказ отдают только
    курьеру на смене и только если хватает товаров)."""
    db.start_courier_shift(COURIER.id, ["center", "north", "east", "south", "west"],
                           {p["id"]: 999 for p in db.stock_catalog()})


def admin_take(call, order_id, courier=None):
    """28.10.2026: курьеры сами заказы не берут — заказ назначает администратор (раньше — «Взять заказ»)."""
    cid = next(c["id"] for c in call(ADMIN, "GET", "/api/admin/couriers")["couriers"]
               if c["telegram_id"] == (courier or COURIER).id)
    return call(ADMIN, "POST", f"/api/admin/orders/{order_id}/assign", {"courier_id": cid})


def arrive(call, order_id):
    """«🚪 Я на месте» перед «Заказ доставлен» (кнопка «Доставлен» — только после неё)."""
    try:
        call(COURIER, "POST", f"/api/courier/orders/{order_id}/arrived")
    except ApiError as exc:
        if exc.status != 429:  # уже отмечено минуту назад — нормально
            raise


class AuthTests(unittest.TestCase):
    def fields(self, **extra):
        data = {
            "auth_date": str(int(time.time())),
            "query_id": "AAH",
            "user": json.dumps({"id": 42, "first_name": "Ира", "username": "ira"}, ensure_ascii=False),
        }
        data.update(extra)
        return data

    def test_valid(self):
        user = validate_init_data(sign_init_data(self.fields(), TOKEN), TOKEN)
        self.assertEqual(user.id, 42)
        self.assertEqual(user.display_name, "Ира (@ira)")

    def test_signature_field_is_part_of_check_string(self):
        user = validate_init_data(sign_init_data(self.fields(signature="abc"), TOKEN), TOKEN)
        self.assertEqual(user.id, 42)

    def test_tampered(self):
        signed = sign_init_data(self.fields(), TOKEN).replace("%D0%98%D1%80%D0%B0", "%D0%9E%D0%BB%D1%8F")
        with self.assertRaises(InitDataError):
            validate_init_data(signed, TOKEN)

    def test_wrong_token(self):
        with self.assertRaises(InitDataError):
            validate_init_data(sign_init_data(self.fields(), TOKEN), "999:OTHER")

    def test_expired(self):
        old = self.fields(auth_date=str(int(time.time()) - 3 * 24 * 3600))
        with self.assertRaises(InitDataError):
            validate_init_data(sign_init_data(old, TOKEN), TOKEN)

    def test_missing_hash(self):
        with self.assertRaises(InitDataError):
            validate_init_data("auth_date=1&user=%7B%7D", TOKEN)


class ApiFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "t.sqlite3", reset=True)
        courier_on_shift(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, user, method, path, body=None, **query):
        return asyncio.run(self.api.handle(method, path, user, body or {}, {k: str(v) for k, v in query.items()}))

    def events(self):
        return [e["event"] for e in self.bridge.events]

    def add_to_cart(self, user=CUSTOMER, product_id=1, variant="new", qty=2):
        return self.call(user, "POST", "/api/cart", {"product_id": product_id, "variant": variant, "quantity": qty})

    def cash_order(self, user=CUSTOMER):
        self.add_to_cart(user)
        return self.call(
            user, "POST", "/api/orders",
            {"payment_method": "cash_on_delivery", "phone": "+7 900 000-00-00", "address": "Москва, ул. Тестовая, д. 1, кв. 2"},
        )["order"]

    def test_product_bonus_gift_item_stock_and_scope(self):
        """28.10.2026: бонус — товар каталога (подарок) за каждую штуку, только к «Новому»; в заказе строкой
        за 0 ₽; списывается с остатка курьера; не хватает подарка — заказ курьеру не назначить."""
        gid = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Шарики (5 шт.)", "description": "Пачка", "price": 100})["product"]["id"]
        with self.assertRaises(ApiError):  # сам к себе
            self.call(ADMIN, "PATCH", "/api/admin/products/1", {"bonus_product_id": 1})
        with self.assertRaises(ApiError):  # ни к чему не применяется
            self.call(ADMIN, "PATCH", "/api/admin/products/1", {"bonus_product_id": gid, "bonus_new": False, "bonus_exchange": False})
        p = self.call(ADMIN, "PATCH", "/api/admin/products/1", {"bonus_product_id": gid, "bonus_qty": 1, "bonus_desc": "Шарики разных цветов",
                                                                "bonus_new": True, "bonus_exchange": False})["product"]
        self.assertEqual((p["bonus_text"], p["bonus_product_id"], p["bonus_new"], p["bonus_exchange"]),
                         ("В подарок: Шарики (5 шт.)", gid, True, False))
        self.add_to_cart(product_id=1, variant="new", qty=2)
        self.add_to_cart(product_id=1, variant="exchange", qty=1)
        cart = self.call(CUSTOMER, "GET", "/api/cart")
        self.assertEqual(cart["gifts"], [{"product_id": gid, "name": "Шарики (5 шт.)", "quantity": 2}])  # 2 новых → 2 подарка
        self.assertEqual({i["variant"]: i["bonus_text"] for i in cart["items"]}, {"new": "В подарок: Шарики (5 шт.)", "exchange": None})
        order = self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "+7 900 000-00-00",
                                                             "address": "Москва, ул. Тестовая, д. 1, кв. 2"})["order"]
        gifts = [(i["name"], i["quantity"], i["unit_price"]) for i in order["items"] if i["gift"]]
        self.assertEqual(gifts, [("Шарики (5 шт.)", 2, 0)])
        self.assertEqual(order["total"], 2 * 2500 + 1900)  # подарок бесплатно
        # у курьера 1 подарок, а нужно 2 — назначить нельзя
        self.db.start_courier_shift(COURIER.id, [], {1: 5, gid: 1})
        with self.assertRaises(ApiError) as ctx:
            admin_take(self.call, order["id"])
        self.assertIn("Шарики (5 шт.)", str(ctx.exception))
        self.db.start_courier_shift(COURIER.id, [], {1: 5, gid: 5})
        admin_take(self.call, order["id"])
        arrive(self.call, order["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        now = self.db.courier_stock(next(c for c in self.db.get_couriers() if c["telegram_id"] == COURIER.id))["now"]
        self.assertEqual((now["1"], now[str(gid)]), (2, 3))  # 5 − 3 баллона, 5 − 2 подарка
        # переименование подарка обновляет подпись; убрать бонус — пустой товар
        self.call(ADMIN, "PATCH", f"/api/admin/products/{gid}", {"name": "Шарики мини"})
        self.assertEqual(self.call(ADMIN, "PATCH", "/api/admin/products/1", {"bonus_qty": 1})["product"]["bonus_text"], "В подарок: Шарики мини")
        p = self.call(ADMIN, "PATCH", "/api/admin/products/1", {"bonus_product_id": None})["product"]
        self.assertEqual((p["bonus_text"], p["bonus_product_id"], p["bonus_desc"]), (None, None, ""))

    # ---------------------------------------------------------------- customer
    def test_me_roles(self):
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/me")["roles"], {"admin": False, "courier": False})
        self.assertTrue(self.call(COURIER, "GET", "/api/me")["roles"]["courier"])
        self.assertTrue(self.call(ADMIN, "GET", "/api/me")["roles"]["admin"])

    def test_cart_and_exchange_rules(self):
        cart = self.add_to_cart(qty=3)
        self.assertEqual(cart["total"], 3 * 2500)
        cart = self.add_to_cart(variant="exchange", qty=1)
        self.assertEqual(cart["total"], 3 * 2500 + 1900)
        self.assertTrue(cart["has_exchange"])
        with self.assertRaises(ApiError) as ctx:  # у шариков нет «Замены»
            self.add_to_cart(product_id=3, variant="exchange", qty=1)
        self.assertEqual(ctx.exception.status, 409)
        cart = self.add_to_cart(variant="new", qty=0)
        self.assertEqual(cart["total"], 1900)

    def test_cash_order_flow_to_rating(self):
        order = self.cash_order()
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["total"], 5000)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/cart")["items"], [])
        self.assertIn("dispatch_new_order", self.events())
        saved = self.db.get_user(CUSTOMER.id)
        self.assertEqual(saved["last_phone"], "8 900 000 00 00")  # формат 8 916 123 45 67

        # 28.10.2026: курьеры сами заказы не берут — «Свободных» нет, назначает админ
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders", scope="available")["orders"], [])
        with self.assertRaises(ApiError):  # «Взять заказ» больше нет
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/take")
        self.assertEqual(self.api.order_json(self.db.get_order(order["id"]), "courier")["customer"]["phone"], "8 900 000 00 00")

        taken = admin_take(self.call, order['id'])["order"]
        self.assertEqual(taken["status"], "accepted")
        self.assertIn("admin_assigned", self.events())
        with self.assertRaises(ApiError):  # второй раз не берётся
            self.call(TelegramUser(9, "X"), "POST", f"/api/courier/orders/{order['id']}/take")

        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/location", {"lat": 55.75, "lon": 37.61})
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/location", {"lat": 55.76, "lon": 37.62})
        locs = [e for e in self.bridge.events if e["event"] == "courier_location"]
        self.assertEqual([e["first"] for e in locs], [True, False])
        active = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"]
        self.assertEqual(active[0]["courier_location"], {"lat": 55.76, "lon": 37.62})
        self.assertFalse(active[0]["can_confirm"])  # закрывает только курьер
        self.assertNotIn("customer", active[0])  # покупателю не отдаём служебные поля

        with self.assertRaises(ApiError) as ctx:  # без оценки покупателя закрыть нельзя
            arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.db.get_order(order["id"])["status"], "accepted")
        done = arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 4})["order"]
        self.assertEqual(done["status"], "delivered")
        history = self.call(CUSTOMER, "GET", "/api/orders", scope="history")
        self.assertEqual(history["purchase_count"], 1)
        self.assertTrue(history["orders"][0]["can_rate"])
        self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/rate", {"rating": 5})
        self.assertFalse(self.call(CUSTOMER, "GET", "/api/orders", scope="history")["orders"][0]["can_rate"])
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/rate", {"rating": 4})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders", scope="current")["completed"], 1)

    def test_customer_cannot_touch_foreign_order(self):
        order = self.cash_order()
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER2, "POST", f"/api/orders/{order['id']}/cancel")
        self.assertEqual(ctx.exception.status, 404)
        self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/cancel")
        self.assertEqual(self.db.get_order(order["id"])["status"], "cancelled")

    def test_geo_order(self):
        self.add_to_cart()
        order = self.call(
            CUSTOMER, "POST", "/api/orders",
            {"payment_method": "cash_on_delivery", "phone": "+79000000000", "lat": 55.7, "lon": 37.6, "address": "у беседки"},
        )["order"]
        row = self.db.get_order(order["id"])
        self.assertTrue(row["delivery_address"].startswith("📍 Геопозиция клиента (координаты 55.700000, 37.600000)"))
        self.assertEqual(row["location_lat"], 55.7)
        self.assertIsNone(self.db.get_user(CUSTOMER.id)["last_address"])  # как в боте: адрес-точку не запоминаем

    def test_courier_transfer_between_couriers(self):
        """28.10.2026 «🔄 Обмен»: курьер А отдаёт курьеру Б полные и пустые; только курьеру на смене;
        30 секунд можно отменить; потом — админу (главная + бот); после одобрения остатки меняются у обоих."""
        IVAN = TelegramUser(2003, "Иван", "Курьер")
        self.db.add_courier("Иван", None, IVAN.id)
        ivan_id = next(c["id"] for c in self.db.get_couriers() if c["telegram_id"] == IVAN.id)
        size = self.db.empty_sizes()[0]
        self.call(COURIER, "POST", "/api/courier/empties", {"empty": {size: 3}})
        pid = self.db.stock_catalog()[0]["id"]

        def age(seconds=31):  # заявке «прошло» 31 секунда
            from datetime import datetime, timedelta, timezone
            with self.db.connection() as conn:
                conn.execute("UPDATE courier_transfers SET created_at = ?",
                             ((datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds"),))

        # Иван не на смене — его нет в списке, передать ему нельзя
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/transfer")["couriers"], [])
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/transfer", {"to_id": ivan_id, "items": {str(pid): 1}})
        self.db.start_courier_shift(IVAN.id, [], {pid: 2})
        data = self.call(COURIER, "GET", "/api/courier/transfer")
        self.assertEqual([c["name"] for c in data["couriers"]], ["Иван"])
        self.assertEqual(data["empties"], {size: 3})
        # больше, чем есть, — нельзя
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", "/api/courier/transfer", {"to_id": ivan_id, "empties": {size: 4}})
        self.assertIn("Столько нет", str(ctx.exception))
        # отправил и отменил в первые 30 секунд
        r = self.call(COURIER, "POST", "/api/courier/transfer", {"to_id": ivan_id, "items": {str(pid): 5}, "empties": {size: 2}})
        self.assertGreater(r["pending"]["cancel_left"], 0)
        self.assertNotIn("transfer", [a["kind"] for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]])
        with self.assertRaises(ApiError):  # пока идут 30 секунд — админ не решает
            self.call(ADMIN, "POST", f"/api/admin/transfers/{r['pending']['id']}", {"approve": True})
        self.assertIsNone(self.call(COURIER, "POST", "/api/courier/transfer/cancel")["pending"])
        # снова; через 30 секунд отменить нельзя, админу — уведомление (главная и бот, один раз)
        r = self.call(COURIER, "POST", "/api/courier/transfer", {"to_id": ivan_id, "items": {str(pid): 5}, "empties": {size: 2}})
        age()
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/transfer/cancel")
        alerts = [a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "transfer"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Пётр → Иван", alerts[0]["title"])
        self.call(ADMIN, "GET", "/api/admin/alerts")
        self.assertEqual(self.events().count("admin_alert"), 1)
        before_petr = self.call(COURIER, "GET", "/api/courier/shift")
        self.call(ADMIN, "POST", f"/api/admin/transfers/{alerts[0]['id']}", {"approve": True})
        after_petr = self.call(COURIER, "GET", "/api/courier/shift")
        ivan = self.call(IVAN, "GET", "/api/courier/shift")
        self.assertEqual(after_petr["stock"]["now"][str(pid)], before_petr["stock"]["now"][str(pid)] - 5)
        self.assertEqual(ivan["stock"]["now"][str(pid)], 2 + 5)
        self.assertEqual((after_petr["empties"][size], ivan["empties"][size]), (1, 2))
        self.assertEqual(self.db.courier_empties_split(ivan_id)["transfer"][size], 2)
        with self.db.connection() as conn:
            kinds = [r["kind"] for r in conn.execute("SELECT kind FROM courier_events WHERE kind LIKE 'transfer_%'")]
        self.assertEqual(sorted(kinds), ["transfer_in", "transfer_out"])
        texts = [e.get("text", "") for e in self.bridge.events if e["event"] == "courier_message"]
        self.assertTrue(any("Вам передал Пётр" in t for t in texts))
        # отказ админа — остатки не меняются
        r = self.call(COURIER, "POST", "/api/courier/transfer", {"to_id": ivan_id, "empties": {size: 1}})
        age()
        self.call(ADMIN, "POST", f"/api/admin/transfers/{r['pending']['id']}", {"approve": False})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["empties"][size], 1)
        self.assertIsNone(self.call(COURIER, "GET", "/api/courier/transfer")["pending"])

    def test_order_has_no_descent(self):
        """28.10.2026, 17:07: в обычном заказе спуска нет — при «Замене» покупатель за спуск не платит;
        descent_count в обычном заказе игнорируется, спуск — только в «Возврате баллона»."""
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"}
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 100, "return": 1500, "descent": 400})
        self.add_to_cart(variant="exchange", qty=2)  # 2 × 1900
        self.assertNotIn("exchange_balloons", self.call(CUSTOMER, "GET", "/api/cart"))
        order = self.call(CUSTOMER, "POST", "/api/orders", {**body, "descent_count": 2})["order"]
        self.assertEqual(order["total"], 2 * 1900)
        self.assertFalse(order.get("descent") and order["descent"].get("count"))

    def test_floor_lift_cash_order(self):
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 150})
        self.add_to_cart()  # 2 × 2500 = 5000
        info = self.call(CUSTOMER, "GET", "/api/checkout")
        self.assertEqual(info["floor_lift_price"], 150)
        order = self.call(
            CUSTOMER, "POST", "/api/orders",
            {"payment_method": "cash_on_delivery", "phone": "+79000000000",
             "address": "Москва, ул. Тестовая, д. 1, кв. 2", "floor_lift": True},
        )["order"]
        # «Нужен подъём»: 5000 + 150 (фиксированная цена, этажи не учитываются) = 5150
        self.assertEqual(order["total"], 5150)
        self.assertEqual(order["floor_lift"], {"needed": True, "floors": 1, "price": 150, "total": 150})
        courier_view = self.api.order_json(self.db.get_order(order["id"]), "courier")
        self.assertEqual(courier_view["floor_lift"]["total"], 150)
        # смена цены не меняет уже оформленный заказ
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 300})
        row = self.db.get_order(order["id"])
        self.assertEqual((row["total"], row["floor_lift_floors"], row["floor_lift_price"]), (5150, 1, 150))
        # «Заберу сам» — поле пустое, сумма только за товары
        self.add_to_cart(CUSTOMER2)
        own = self.call(
            CUSTOMER2, "POST", "/api/orders",
            {"payment_method": "cash_on_delivery", "phone": "+79000000001",
             "address": "Москва, ул. Тестовая, д. 1, кв. 3", "floor_lift": False},
        )["order"]
        self.assertIsNone(own["floor_lift"])
        self.assertEqual(own["total"], 5000)
        # старая версия приложения присылает число этажей — считается как «нужен» один раз
        self.add_to_cart()
        legacy = self.call(
            CUSTOMER, "POST", "/api/orders",
            {"payment_method": "cash_on_delivery", "phone": "+79000000000",
             "address": "Москва, ул. Тестовая, д. 1, кв. 2", "floor_lift_floors": 7},
        )["order"]
        self.assertEqual(legacy["total"], 5000 + 300)
        # 28.10.2026: цена — за подъём одного баллона; подъёмов не больше, чем баллонов в заказе
        self.add_to_cart()  # 2 баллона
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/cart")["balloons"], 2)
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000",
                "address": "Москва, ул. Тестовая, д. 1, кв. 2", "floor_lift_count": 3}
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", body)
        self.assertIn("не может быть больше", ctx.exception.message)
        two = self.call(CUSTOMER, "POST", "/api/orders", {**body, "floor_lift_count": 2})["order"]
        self.assertEqual((two["total"], two["floor_lift"]["floors"], two["floor_lift"]["total"]), (5000 + 600, 2, 600))

    def test_floor_lift_disabled_and_permissions(self):
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 0})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/checkout")["floor_lift_price"], 0)
        self.add_to_cart()
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000",
                "address": "Москва, ул. Тестовая, д. 1, кв. 2", "floor_lift": True}
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", body)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/cart")["count"], 2)  # корзина не тронута
        for bad in (-1, "abc", 100_001):
            with self.assertRaises(ApiError):
                self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": bad})
        for user, method, body_ in ((CUSTOMER, "GET", None), (COURIER, "PUT", {"floor_lift_price": 1})):
            with self.assertRaises(ApiError) as ctx:
                self.call(user, method, "/api/admin/settings", body_)
            self.assertEqual(ctx.exception.status, 403)
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 50})
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/settings")["floor_lift_price"], 50)

    # ---------------------------------------------------------------- «Я на месте», оценки, отзывы
    def take(self, order):
        return admin_take(self.call, order['id'])["order"]

    def test_courier_arrived_notifies_customer(self):
        order = self.cash_order()
        with self.assertRaises(ApiError):  # пока заказ не взят — нельзя
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/arrived")
        self.take(order)
        with self.assertRaises(ApiError) as ctx:  # чужой курьер
            self.call(TelegramUser(9, "X"), "POST", f"/api/courier/orders/{order['id']}/arrived")
        self.assertEqual(ctx.exception.status, 403)
        res = self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/arrived")
        self.assertTrue(res["notified"])
        self.assertIn("courier_arrived", self.events())
        mine = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"][0]
        self.assertIsNotNone(mine["courier_arrived_at"])
        badge = self.call(CUSTOMER, "GET", "/api/badges")["orders"][str(order["id"])]
        self.assertEqual(badge["arrived_at"], mine["courier_arrived_at"])
        with self.assertRaises(ApiError) as ctx:  # повтор раньше чем через минуту
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/arrived")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.events().count("courier_arrived"), 1)

    def test_customer_cannot_close_order(self):
        """«Получил заказ» у покупателя убран: заказ закрывает только курьер."""
        order = self.cash_order()
        self.take(order)
        active = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"][0]
        self.assertFalse(active["can_confirm"])
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/received", {"rating": 5})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.db.get_order(order["id"])["status"], "accepted")
        done = arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})["order"]
        self.assertEqual(done["status"], "delivered")

    def test_rating_required_until_rated(self):
        """После «Доставлен» покупатель обязан оценить курьера: требование
        держится при каждом запуске и в опросе, пока оценки нет."""
        o1 = self.cash_order()
        self.take(o1)
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["pending_rating"])
        arrive(self.call, o1['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o1['id']}/delivered", {"rating": 3})
        o2 = self.cash_order()
        self.take(o2)
        arrive(self.call, o2['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o2['id']}/delivered", {"rating": 4})
        for _ in range(3):  # не «один раз»: пока не оценил — требуется
            self.assertEqual(self.call(CUSTOMER, "GET", "/api/me")["pending_rating"]["order_id"], o1["id"])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["pending_rating"]["order_id"], o1["id"])
        self.call(CUSTOMER, "POST", f"/api/orders/{o1['id']}/rate", {"rating": 5})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/me")["pending_rating"]["order_id"], o2["id"])
        self.call(CUSTOMER, "POST", f"/api/orders/{o2['id']}/rate", {"rating": 4})
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["pending_rating"])
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/badges")["pending_rating"])
        self.assertIsNone(self.call(CUSTOMER2, "GET", "/api/me")["pending_rating"])

    def test_old_delivered_orders_do_not_block(self):
        """Заказы, доставленные до обновления (rating_required = 0), оценки не требуют."""
        order = self.cash_order()
        self.take(order)
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 3})
        with self.db.connection() as conn:
            conn.execute("UPDATE orders SET rating_required = 0 WHERE id = ?", (order["id"],))
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["pending_rating"])

    def test_customer_reviews_visible_only_to_staff(self):
        order = self.cash_order()
        self.take(order)
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/customer-note", {"text": "Домофон не работает, звонить"})
        with self.assertRaises(ApiError):  # пустой комментарий
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/customer-note", {"text": " "})
        with self.assertRaises(ApiError) as ctx:  # покупатель не может оставлять/читать
            self.call(CUSTOMER, "POST", f"/api/courier/orders/{order['id']}/customer-note", {"text": "я хороший"})
        self.assertEqual(ctx.exception.status, 403)
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered",
                  {"rating": 2, "comment": "Долго не выходил"})
        self.assertIn("customer_review", self.events())
        # второй заказ того же покупателя: курьеры и админ видят историю отзывов
        o2 = self.cash_order()
        free = self.api.order_json(self.db.get_order(o2["id"]), "courier")
        rev = free["customer_reviews"]
        self.assertEqual((rev["rating"], rev["rating_count"]), (2.0, 1))
        self.assertEqual([n["comment"] for n in rev["notes"]], ["Долго не выходил", "Домофон не работает, звонить"])
        adm = self.call(ADMIN, "GET", "/api/admin/orders", status="active")["orders"]
        self.assertEqual(next(o for o in adm if o["id"] == o2["id"])["customer_reviews"]["rating_count"], 1)
        # покупателю отзывы не отдаются нигде
        dump = json.dumps([
            self.call(CUSTOMER, "GET", "/api/orders", scope="active"),
            self.call(CUSTOMER, "GET", "/api/orders", scope="history"),
            self.call(CUSTOMER, "GET", "/api/me"),
            self.call(CUSTOMER, "GET", "/api/badges"),
        ], ensure_ascii=False)
        self.assertNotIn("customer_reviews", dump)
        self.assertNotIn("Долго не выходил", dump)
        self.assertNotIn("Домофон", dump)
        # повторно оценить покупателя по тому же заказу нельзя
        self.assertFalse(self.db.add_customer_review(order["id"], CUSTOMER.id, None, None, 5, None))

    def test_blacklist_blocks_orders_until_removed(self):
        first = self.cash_order()  # покупатель появляется в базе; активный заказ остаётся
        taken = self.cash_order()
        self.take(taken)  # заказ уже у курьера
        res = self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/block", {"blocked": True, "reason": "грубил курьеру"})
        self.assertTrue(res["blocked"])
        self.assertIn("customer_blocked", self.events())
        # активные заказы аннулированы — те же уведомления, что при отмене админом
        self.assertEqual(sorted(res["cancelled"]), sorted([first["id"], taken["id"]]))
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"], [])
        self.assertEqual(self.events().count("admin_status"), 2)
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders", scope="current")["orders"], [])
        # оформить заказ нельзя — ни наличными, ни при курьере
        self.add_to_cart()
        for method in ("cash_on_delivery", "sbp"):
            with self.assertRaises(ApiError) as ctx:
                self.call(CUSTOMER, "POST", "/api/orders",
                          {"payment_method": method, "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"})
            self.assertEqual(ctx.exception.status, 403)
            self.assertIn("недоступно", ctx.exception.message)
        self.assertIn("недоступно", self.call(CUSTOMER, "GET", "/api/checkout")["blocked_text"])
        self.assertEqual(len(self.call(CUSTOMER, "GET", "/api/cart")["items"]), 1)  # каталог и корзина работают
        # админ видит метку и причину; курьер — нет
        anna = next(c for c in self.call(ADMIN, "GET", "/api/admin/customers")["customers"] if c["telegram_id"] == CUSTOMER.id)
        self.assertEqual((anna["blocked"], anna["blocked_reason"]), (True, "грубил курьеру"))
        adm = next(o for o in self.call(ADMIN, "GET", "/api/admin/orders", scope="all")["orders"] if o["id"] == first["id"])
        self.assertTrue(adm["customer"]["blocked"])
        self.assertEqual(adm["status"], "cancelled")
        other = self.cash_order(CUSTOMER2)
        cour = self.api.order_json(self.db.get_order(other["id"]), "courier")
        self.assertNotIn("blocked", cour["customer"])
        # блокировать может только админ, админа — нельзя
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "PUT", f"/api/admin/customers/{CUSTOMER.id}/block", {"blocked": True})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PUT", f"/api/admin/customers/{ADMIN.id}/block", {"blocked": True})
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "PUT", "/api/admin/customers/999999/block", {"blocked": True})
        self.assertEqual(ctx.exception.status, 404)
        # убрали из ЧС — снова можно
        self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/block", {"blocked": False})
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/checkout")["blocked_text"])
        order = self.call(CUSTOMER, "POST", "/api/orders",
                          {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"})["order"]
        self.assertEqual(order["status"], "new")
        self.assertFalse(self.db.get_users()[0]["blocked_reason"])

    # ---------------------------------------------------------------- «взял по ошибке» и «спорные заказы»
    def test_courier_released_order_goes_back_to_all_couriers(self):
        order = self.cash_order()
        cur = self.take(order)
        self.assertTrue(cur["can_release"])
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/location", {"lat": 55.7, "lon": 37.6})
        res = self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/release")
        self.assertTrue(res["ok"])
        self.assertIn("courier_released", self.events())
        row = self.db.get_order(order["id"])
        self.assertEqual((row["status"], row["courier_id"], row["courier_last_lat"]), ("new", None, None))
        free = [o["id"] for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"] if o["status"] == "new"]
        self.assertIn(order["id"], free)  # снова ждёт назначения
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders", scope="current")["orders"], [])
        mine = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"][0]
        self.assertEqual((mine["status"], mine["courier"]), ("new", None))
        # его можно снова взять — и после «Я на месте» вернуть уже нельзя
        again = self.take(order)
        self.assertEqual(again["status"], "accepted")
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/arrived")
        cur = self.call(COURIER, "GET", "/api/courier/orders", scope="current")["orders"][0]
        self.assertFalse(cur["can_release"])
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/release")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ApiError):  # чужой заказ вернуть нельзя
            self.call(CUSTOMER, "POST", f"/api/courier/orders/{order['id']}/release")

    def test_no_contact_dispute_return_and_close(self):
        order = self.cash_order()
        self.take(order)
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/arrived")
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/no-contact")
        self.assertIn("order_disputed", self.events())
        self.assertEqual(self.db.get_order(order["id"])["status"], "disputed")
        self.assertTrue(self.db.get_order(order["id"])["disputed_at"])
        # у курьера заказа больше нет, у покупателя — висит в активных с надписью
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders", scope="current")["orders"], [])
        mine = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"][0]
        self.assertEqual(mine["status_label"], "Ждём связи с вами")
        self.assertIn("администрация скоро с вами свяжется", mine["status_note"])
        self.assertNotIn("Спорн", json.dumps(self.call(CUSTOMER, "GET", "/api/orders", scope="active"), ensure_ascii=False))
        # у админа — только в «Спорных», не в активных заказах
        self.assertNotIn(order["id"], [o["id"] for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"]])
        d = self.call(ADMIN, "GET", "/api/admin/disputes")["orders"]
        self.assertEqual([o["id"] for o in d], [order["id"]])
        self.assertEqual(d[0]["courier"]["name"], "Пётр")
        self.assertTrue(d[0]["customer"]["phone"] and d[0]["items"] and d[0]["disputed_at"])
        with self.assertRaises(ApiError) as ctx:  # «Спорные» — только админу
            self.call(COURIER, "GET", "/api/admin/disputes")
        self.assertEqual(ctx.exception.status, 403)
        # «🔁 Вернуть в работу»: снова у всех курьеров, следы прошлого курьера сброшены
        left = self.call(ADMIN, "POST", f"/api/admin/disputes/{order['id']}/return")["orders"]
        self.assertEqual(left, [])
        self.assertIn("dispute_returned", self.events())
        row = self.db.get_order(order["id"])
        self.assertEqual((row["status"], row["courier_id"], row["courier_arrived_at"], row["disputed_at"]), ("new", None, None, None))
        with self.assertRaises(ApiError) as ctx:  # уже не спорный
            self.call(ADMIN, "POST", f"/api/admin/disputes/{order['id']}/close")
        self.assertEqual(ctx.exception.status, 409)
        # доставили после возврата — в Excel заказ ровно один раз
        self.take(order)
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        lines = self.db.get_sales_lines(None, None)["lines"]
        self.assertEqual([l["order_no"] for l in lines].count(order["id"]), 1)

    def test_dispute_close_keeps_history_and_stays_out_of_export(self):
        order = self.cash_order()
        self.take(order)
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/no-contact")
        self.call(ADMIN, "POST", f"/api/admin/disputes/{order['id']}/close")
        self.assertIn("dispute_closed", self.events())
        self.assertEqual(self.db.get_order(order["id"])["status"], "disputed_closed")
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"], [])
        hist = self.call(CUSTOMER, "GET", "/api/orders", scope="history")["orders"]
        self.assertEqual(next(o for o in hist if o["id"] == order["id"])["status_label"], "Закрыт")
        adm = self.call(ADMIN, "GET", "/api/admin/history", status="disputed")["orders"]
        self.assertEqual([(o["id"], o["status_label"]) for o in adm], [(order["id"], "Спорный — закрыт")])
        self.assertEqual(self.db.get_sales_lines(None, None)["lines"], [])  # в Excel не попадает
        # закрытый заказ больше нельзя менять
        self.assertIsNone(self.db.update_order_status(order["id"], "new"))
        with self.assertRaises(ApiError):
            admin_take(self.call, order['id'])

    def test_blacklist_cancels_disputed_order_too(self):
        order = self.cash_order()
        self.take(order)
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/no-contact")
        res = self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/block", {"blocked": True})
        self.assertEqual(res["cancelled"], [order["id"]])
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/disputes")["orders"], [])

    def test_referral_requests(self):
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/referral"), {"admin": False, "requests": {}})
        ref = self.call(ADMIN, "GET", "/api/referral")
        self.assertEqual((ref["admin"], ref["requests"], ref["partners"]), (True, [], []))
        self.assertEqual([(r["name"], r["points"]) for r in ref["rewards"]], [("Баллон 8 л", 0), ("Баллон 16 л", 0)])
        with self.assertRaises(ApiError):  # программы лояльности для клиентов больше нет
            self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "loyalty"})
        for bad in ({"kind": "partner"}, {"kind": "partner", "comment": "   "}, {"kind": "partner", "comment": "x" * 301}):
            with self.assertRaises(ApiError):  # без комментария (или слишком длинный) — заявку не отправить
                self.call(CUSTOMER, "POST", "/api/referral/apply", bad)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/referral"), {"admin": False, "requests": {}})
        res = self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.assertEqual(set(res["requests"]), {"partner"})
        self.assertEqual(next(e for e in self.bridge.events if e["event"] == "referral_request")["comment"], "Кодовое слово")
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"comment": "Ещё раз"})  # по умолчанию — партнёрская; повтор без дубля
        self.call(COURIER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.assertEqual(self.events().count("referral_request"), 2)
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["referral"]["partner"], 2)
        self.assertNotIn("admin", self.call(CUSTOMER, "GET", "/api/badges"))
        reqs = self.call(ADMIN, "GET", "/api/referral")["requests"]
        self.assertEqual(sorted(r["user_id"] for r in reqs), sorted([CUSTOMER.id, COURIER.id]))
        petr = next(r for r in reqs if r["user_id"] == COURIER.id)
        anna = next(r for r in reqs if r["user_id"] == CUSTOMER.id)
        self.assertEqual(anna["comment"], "Кодовое слово")  # админ видит комментарий
        self.assertTrue(petr["is_courier"])  # курьер помечен
        self.assertEqual(petr["courier_name"], "Пётр")
        self.assertTrue(petr["phone"])
        self.assertFalse(anna["is_courier"])
        self.assertEqual(anna["username"], "anna_dev")
        # старые заявки на лояльность в списке админа не показываются
        self.db.add_referral_request(CUSTOMER.id, "loyalty")
        self.assertEqual(len(self.call(ADMIN, "GET", "/api/referral")["requests"]), 2)
        # админ в режиме покупателя видит экран покупателя и может подать заявку (проверка)
        self.assertEqual(self.call(ADMIN, "GET", "/api/referral", view="customer"), {"admin": False, "requests": {}})
        res = self.call(ADMIN, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.assertEqual(res["requests"]["partner"]["status"], "pending")

    # ---------------------------------------------------------------- партнёрская программа
    def partner_order(self, user, items, lift=False, deliver=True):
        for item in items:
            product_id, qty, variant = (*item, "new")[:3]
            self.call(user, "POST", "/api/cart", {"product_id": product_id, "variant": variant, "quantity": qty})
        o = self.call(user, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "+79000000002",
                      "address": "Москва, ул. Новая, д. 5", "floor_lift": lift})["order"]
        if deliver:
            self.take(o)
            arrive(self.call, o['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        return o

    def test_partner_programme_link_binding_and_points(self):
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 3000})
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        with self.assertRaises(ApiError) as ctx:  # одобряет только админ
            self.call(COURIER, "POST", f"/api/admin/referral/{req['id']}/approve")
        self.assertEqual(ctx.exception.status, 403)
        res = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")  # ставки у партнёра больше не задаются
        self.assertEqual(res["requests"], [])  # из заявок ушла в партнёры
        partner = res["partners"][0]
        self.assertEqual((partner["user_id"], partner["balance"]), (CUSTOMER.id, 0))
        self.assertNotIn("reward_new", partner)
        code = partner["code"]
        self.assertRegex(code, r"^p_[A-Z2-9]{7}$")
        self.assertEqual(partner["link"], f"https://t.me/RelaxGas_bot?start={code}")
        # демо-баллоны 8 л и 16 л: объёма нет в таблице по умолчанию → 0, пока админ не задаст
        mine = self.call(CUSTOMER, "GET", "/api/referral")["partner"]
        self.assertEqual((mine["code"], mine["points"], mine["invited"], mine["rewards"]), (code, 0, 0, []))
        self.call(ADMIN, "PATCH", "/api/admin/products/1", {"partner_reward": "200"})
        self.assertEqual([(r["name"], r["points"]) for r in self.call(CUSTOMER, "GET", "/api/referral")["partner"]["rewards"]],
                         [("Баллон 8 л", 200)])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/me")["partner"], {"points": 0, "link_notice": False})
        self.assertIsNone(self.call(COURIER, "GET", "/api/me")["partner"])
        # привязка по ссылке (бот, /start p_…): только новый клиент, один раз
        self.db.upsert_user(CUSTOMER2.id, None, "Борис", None)
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, "p_WRONG00"), "bad_code")
        self.assertEqual(self.db.bind_referral(CUSTOMER.id, code), "self")
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "bound")
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "already")
        self.cash_order(COURIER)  # у курьера уже был заказ — он не «новый»
        self.assertEqual(self.db.bind_referral(COURIER.id, code), "not_new")
        # 5 баллонов 8 л: 4 новых + 1 на замену — одна ставка → 5 × 200 = 1000
        self.partner_order(CUSTOMER2, [(1, 4, "new"), (1, 1, "exchange")])
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1000)
        hist = self.call(CUSTOMER, "GET", "/api/referral")["partner"]["history"]
        self.assertEqual(hist[0]["reason"], "Баллон 8 л: 5 × 200")
        # сумма заказа и подъём не важны: 1 баллон с подъёмом → +200
        self.partner_order(CUSTOMER2, [(1, 1)], lift=True)
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1200)
        # отменённый заказ не считается
        cancelled = self.partner_order(CUSTOMER2, [(2, 2)], deliver=False)
        self.call(CUSTOMER2, "POST", f"/api/orders/{cancelled['id']}/cancel")
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1200)
        # заказы самого партнёра баллы не дают
        self.partner_order(CUSTOMER, [(2, 2)])
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1200)
        # 16 л без ставки — 0; шарики не баллон — 0 даже если прислать ставку
        self.partner_order(CUSTOMER2, [(2, 1), (3, 2)])
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1200)
        self.call(ADMIN, "PATCH", "/api/admin/products/3", {"partner_reward": "999"})
        shariki = next(p for p in self.call(ADMIN, "GET", "/api/admin/products")["products"] if p["id"] == 3)
        self.assertEqual((shariki["is_balloon"], shariki["partner_reward"]), (False, 0))
        # новая ставка — для следующих заказов, уже начисленное не меняется
        self.call(ADMIN, "PATCH", "/api/admin/products/2", {"partner_reward": "300"})
        self.partner_order(CUSTOMER2, [(2, 2, "exchange")])  # 2 × 300
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1800)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["partner"], {"points": 1800, "link_notice": False})
        mine = self.call(CUSTOMER, "GET", "/api/referral")["partner"]
        self.assertEqual((mine["invited"], [h["points"] for h in mine["history"]]), (1, [600, 200, 1000]))
        # списание: не больше остатка
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "POST", f"/api/admin/partners/{CUSTOMER.id}/writeoff", {"points": 1801})
        self.assertEqual(ctx.exception.status, 409)
        res = self.call(ADMIN, "POST", f"/api/admin/partners/{CUSTOMER.id}/writeoff", {"points": 100, "reason": "выдано скидкой"})
        p = res["partners"][0]
        self.assertEqual((p["balance"], p["earned"], p["written_off"], p["invited"], p["rewarded_orders"]), (1700, 1800, 100, 1, 3))
        self.assertIn("partner_event", self.events())
        # сброс истории заказов: баллы остаются, новый заказ №1 снова даёт баллы
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1700)
        again = self.partner_order(CUSTOMER2, [(2, 2)])
        self.assertEqual(again["id"], 1)
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 2300)
        self.assertEqual(self.db.accrue_partner_points(again["id"]), 0)  # второй раз за тот же заказ — нет
        # в полной выгрузке — лист «Партнёры» и «пришёл по ссылке» у клиента
        res = self.call(ADMIN, "POST", "/api/admin/export", {"password": "AdminGas", "deliver": "download"})
        data, _ = self.api.get_export(res["url"].split("/")[2])
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(data))
        head = [c.value for c in wb["Партнёры"][3]]
        row = dict(zip(head, [c.value for c in wb["Партнёры"][4]]))
        self.assertEqual((row["Telegram ID"], row["Код ссылки"], row["Остаток баллов"], row["Пришло клиентов"]), (CUSTOMER.id, code, 2300, 1))
        self.assertNotIn("Баллов за новый баллон", row)
        self.assertIn("Борис", row["Приглашённые клиенты"])
        head = [c.value for c in wb["Клиенты"][3]]
        boris = next(dict(zip(head, [c.value for c in r])) for r in wb["Клиенты"].iter_rows(min_row=4) if r[3].value == CUSTOMER2.id)
        self.assertEqual(boris["Пришёл по ссылке партнёра"], "Анна Покупатель")

    def test_partner_link_refresh_notice_and_clients(self):
        """«Выслать всем партнёрам новую ссылку»: окно у партнёра, пока не подтвердит; список покупателей."""
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        code = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")["partners"][0]["code"]
        self.assertFalse(self.call(CUSTOMER, "GET", "/api/me")["partner"]["link_notice"])
        with self.assertRaises(ApiError) as ctx:  # обновляет ссылку только админ
            self.call(COURIER, "POST", "/api/admin/partners/link-refresh")
        self.assertEqual(ctx.exception.status, 403)
        res = self.call(ADMIN, "POST", "/api/admin/partners/link-refresh")
        self.assertEqual((res["ok"], res["partners"]), (True, 1))
        self.assertTrue(self.call(CUSTOMER, "GET", "/api/me")["partner"]["link_notice"])
        self.assertTrue(self.call(CUSTOMER, "GET", "/api/referral")["partner"]["link_notice"])
        # клиенты партнёра: с username — @username, без — имя и ID
        self.db.upsert_user(CUSTOMER2.id, "boris_b", "Борис", None)
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "bound")
        self.db.upsert_user(1003, None, "Вера", "Без")
        self.assertEqual(self.db.bind_referral(1003, code), "bound")
        out = self.call(CUSTOMER, "GET", "/api/partner/clients")
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["text"].split("\n"), ["@boris_b", "Вера Без (ID 1003, без username)"])
        with self.assertRaises(ApiError) as ctx:  # не партнёр — отказ
            self.call(COURIER, "GET", "/api/partner/clients")
        self.assertEqual(ctx.exception.status, 403)
        # окно не гаснет само: только подтверждение партнёра
        self.assertTrue(self.call(CUSTOMER, "GET", "/api/me")["partner"]["link_notice"])
        self.call(CUSTOMER, "POST", "/api/partner/link-ack")
        self.assertFalse(self.call(CUSTOMER, "GET", "/api/me")["partner"]["link_notice"])
        self.call(ADMIN, "POST", "/api/admin/partners/link-refresh")  # новое обновление — окно снова
        self.assertTrue(self.call(CUSTOMER, "GET", "/api/me")["partner"]["link_notice"])

    def test_partner_personal_program(self):
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        code = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")["partners"][0]["code"]
        self.db.upsert_user(CUSTOMER2.id, None, "Борис", None)
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "bound")
        self.call(ADMIN, "PATCH", "/api/admin/products/1", {"partner_reward": "200"})   # общая: 8 л — 200, 16 л — 0
        with self.assertRaises(ApiError):  # шарики — не баллон
            self.call(ADMIN, "PUT", f"/api/admin/partners/{CUSTOMER.id}/rewards", {"rewards": {"3": 50}})
        with self.assertRaises(ApiError):
            self.call(COURIER, "PUT", f"/api/admin/partners/{CUSTOMER.id}/rewards", {"rewards": {"1": 50}})
        res = self.call(ADMIN, "PUT", f"/api/admin/partners/{CUSTOMER.id}/rewards", {"rewards": {"1": "250", "2": "400"}})
        p = res["partners"][0]
        self.assertEqual([(r["name"], r["points"], r["general"]) for r in p["personal"]],
                         [("Баллон 8 л", 250, 200), ("Баллон 16 л", 400, 0)])
        mine = self.call(CUSTOMER, "GET", "/api/referral")["partner"]["rewards"]
        self.assertEqual([(r["name"], r["points"]) for r in mine], [("Баллон 8 л", 250), ("Баллон 16 л", 400)])
        self.partner_order(CUSTOMER2, [(1, 2), (2, 1, "exchange")])  # 2 × 250 + 1 × 400
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 900)
        # «Всё по общей программе»
        self.call(ADMIN, "PUT", f"/api/admin/partners/{CUSTOMER.id}/rewards", {"rewards": {"1": "", "2": ""}})
        self.partner_order(CUSTOMER2, [(1, 1), (2, 1)])  # 200 + 0
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1100)
        actions = [a["action"] for a in self.db.get_admin_actions()]
        self.assertEqual(actions.count("Партнёр: личная программа"), 2)

    def test_partner_pays_goods_with_points(self):
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        code = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")["partners"][0]["code"]
        self.db.upsert_user(CUSTOMER2.id, None, "Борис", None)
        self.db.bind_referral(CUSTOMER2.id, code)
        self.call(ADMIN, "PATCH", "/api/admin/products/1", {"partner_reward": "1000"})
        self.partner_order(CUSTOMER2, [(1, 3)])  # 3 × 1000 = 3000 баллов
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 500})
        self.assertIsNone(self.call(CUSTOMER2, "GET", "/api/checkout")["partner_points"])  # не партнёр — блока нет
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})  # 2 500 ₽
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/checkout")["partner_points"], 3000)
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"}
        with self.assertRaises(ApiError) as ctx:  # бонусы + баллы — не больше суммы товаров минус 1 ₽
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": 500, "points_spend": 2000})
        self.assertIn("1999", ctx.exception.message)
        with self.assertRaises(ApiError):  # больше, чем есть
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "points_spend": 3001})
        o = self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": 500, "points_spend": 1500})["order"]
        self.assertEqual((o["total"], o["bonus_spent"], o["points_spent"]), (500, 500, 1500))
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 1500)
        hist = self.call(CUSTOMER, "GET", "/api/referral")["partner"]["history"]
        self.assertEqual((hist[0]["kind"], hist[0]["points"]), ("spend", -1500))
        self.call(CUSTOMER, "POST", f"/api/orders/{o['id']}/cancel")  # отмена — баллы и бонусы вернулись
        self.assertEqual((self.db.partner_points_balance(CUSTOMER.id), self.db.bonus_balance(CUSTOMER.id)), (3000, 500))
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        o2 = self.call(CUSTOMER, "POST", "/api/orders", {**body, "points_spend": 2499})["order"]
        self.assertEqual(o2["total"], 1)
        self.take(o2)
        arrive(self.call, o2["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o2['id']}/delivered", {"rating": 5})
        self.assertEqual(self.db.partner_points_balance(CUSTOMER.id), 501)
        res = self.call(ADMIN, "POST", "/api/admin/export", {"password": "AdminGas", "deliver": "download"})
        data, _ = self.api.get_export(res["url"].split("/")[2])
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(data))
        head = [c.value for c in wb["Все заказы"][6]]
        row = next(r for r in wb["Все заказы"].iter_rows(min_row=7, values_only=True) if r[head.index("Заказ №")] == o2["id"])
        self.assertEqual((row[head.index("Баллами, ₽")], row[head.index("Итого, ₽")]), (2499, 1))
        sales = [c.value for c in wb["Продажи"][ExportTests.HEADER_ROW]]
        types = [r[sales.index("Тип")] for r in wb["Продажи"].iter_rows(min_row=ExportTests.HEADER_ROW + 1, values_only=True)]
        self.assertIn("Баллы", types)

    def test_admin_sets_reward_per_balloon(self):
        p = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 1000})["product"]
        self.assertEqual((p["is_balloon"], p["partner_reward"], p["partner_reward_custom"]), (True, 150, False))
        p = self.call(ADMIN, "PATCH", f"/api/admin/products/{p['id']}", {"partner_reward": "180"})["product"]
        self.assertEqual((p["partner_reward"], p["partner_reward_custom"]), (180, True))
        p = self.call(ADMIN, "PATCH", f"/api/admin/products/{p['id']}", {"partner_reward": ""})["product"]
        self.assertEqual((p["partner_reward"], p["partner_reward_custom"]), (150, False))
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PATCH", f"/api/admin/products/{p['id']}", {"partner_reward": "-5"})

    def test_referral_v1_requests_migrate_to_loyalty(self):
        import sqlite3 as _sqlite3
        self.cash_order()
        with self.db.connection() as conn:  # таблица первой версии: одна заявка на человека, без вида
            conn.executescript(f"""
                DROP TABLE referral_requests;
                CREATE TABLE referral_requests (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL);
                INSERT INTO referral_requests (user_id, created_at) VALUES ({CUSTOMER.id}, '2026-10-20T05:49:00+00:00');
            """)
        self.db.initialize()
        mine = self.db.get_user_referral_requests(CUSTOMER.id)
        self.assertEqual(list(mine), ["loyalty"])
        self.assertEqual((mine["loyalty"]["status"], mine["loyalty"]["created_at"]), ("pending", "2026-10-20T05:49:00+00:00"))
        self.assertEqual(self.db.add_referral_request(CUSTOMER.id, "partner"), "created")
        self.assertEqual(self.db.add_referral_request(CUSTOMER.id, "loyalty"), "exists")

    def test_referral_decline_for_7_days_and_forever(self):
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.call(COURIER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        reqs = {r["user_id"]: r for r in self.call(ADMIN, "GET", "/api/referral")["requests"]}
        anna, petr = reqs[CUSTOMER.id]["id"], reqs[COURIER.id]["id"]
        # ⏳ на 7 дней: из списка админа пропадает, повторно подать пока нельзя
        left = self.call(ADMIN, "POST", f"/api/admin/referral/{anna}/decline", {"mode": "temp"})["requests"]
        self.assertEqual([r["user_id"] for r in left], [COURIER.id])
        self.assertIn("referral_declined", self.events())
        mine = self.call(CUSTOMER, "GET", "/api/referral")["requests"]
        self.assertEqual(mine["partner"]["status"], "declined_temp")
        self.assertTrue(mine["partner"]["decline_until"] > self.db.now())
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ApiError):  # повторно отклонить уже обработанную нельзя
            self.call(ADMIN, "POST", f"/api/admin/referral/{anna}/decline", {"mode": "forever"})
        # прошло 7 дней — можно подать снова, заявка снова у админа
        with self.db.connection() as conn:
            conn.execute("UPDATE referral_requests SET decline_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (anna,))
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/referral")["requests"]["partner"]["status"], "can_reapply")
        before = self.events().count("referral_request")
        res = self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        self.assertEqual(res["requests"]["partner"]["status"], "pending")
        self.assertEqual(self.events().count("referral_request"), before + 1)
        # ✖️ «просто отклонить»: из списка пропадает, можно сразу подать снова
        self.call(ADMIN, "POST", f"/api/admin/referral/{anna}/decline", {"mode": "dismiss"})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/referral")["requests"]["partner"]["status"], "can_reapply")
        self.assertEqual(self.call(CUSTOMER, "POST", "/api/referral/apply", {"comment": "Снова"})["requests"]["partner"]["status"], "pending")
        # ⛔ навсегда: подать снова нельзя никогда
        self.call(ADMIN, "POST", f"/api/admin/referral/{petr}/decline", {"mode": "forever"})
        self.assertEqual(self.call(COURIER, "GET", "/api/referral")["requests"]["partner"]["status"], "declined_forever")
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        with self.assertRaises(ApiError):
            self.call(ADMIN, "POST", f"/api/admin/referral/{anna}/decline", {"mode": "nope"})
        with self.assertRaises(ApiError) as ctx:  # отклонять может только админ
            self.call(COURIER, "POST", f"/api/admin/referral/{anna}/decline", {"mode": "forever"})
        self.assertEqual(ctx.exception.status, 403)

    def test_app_mode_is_remembered(self):
        self.assertIsNone(self.call(COURIER, "GET", "/api/me")["mode"])  # первый вход — спросим
        self.call(COURIER, "PUT", "/api/me/mode", {"mode": "courier"})
        self.assertEqual(self.call(COURIER, "GET", "/api/me")["mode"], "courier")  # следующий вход — сразу курьер
        self.call(COURIER, "PUT", "/api/me/mode", {"mode": "customer"})
        self.assertEqual(self.call(COURIER, "GET", "/api/me")["mode"], "customer")
        self.call(ADMIN, "PUT", "/api/me/mode", {"mode": "admin"})
        self.assertEqual(self.call(ADMIN, "GET", "/api/me")["mode"], "admin")
        for bad, user in (({"mode": "admin"}, COURIER), ({"mode": "courier"}, CUSTOMER), ({"mode": "boss"}, ADMIN)):
            with self.assertRaises(ApiError):  # чужую роль не сохранить
                self.call(user, "PUT", "/api/me/mode", bad)
        # курьера убрали из курьеров — сохранённый режим больше не действует
        self.call(COURIER, "PUT", "/api/me/mode", {"mode": "courier"})
        with self.db.connection() as conn:
            conn.execute("UPDATE couriers SET is_active = 0 WHERE telegram_id = ?", (COURIER.id,))
        self.assertIsNone(self.call(COURIER, "GET", "/api/me")["mode"])

    def test_courier_history_has_no_total_sum(self):
        order = self.cash_order()
        self.take(order)
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        hist = self.call(COURIER, "GET", "/api/courier/history")
        self.assertEqual(hist["total"], 1)
        self.assertNotIn("delivered_sum", hist)
        self.assertIn("delivered_sum", self.call(ADMIN, "GET", "/api/admin/history"))  # у админа сумма остаётся

    def test_card_shows_last_three_reviews_and_all_on_request(self):
        self.cash_order()  # покупатель появляется в базе
        for i in range(1, 6):  # 5 комментариев от двух курьеров
            self.assertTrue(self.db.add_customer_review(None, CUSTOMER.id, None, "Пётр" if i % 2 else "Иван", None, f"отзыв {i}"))
        order = next(o for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"])
        rev = self.api.order_json(self.db.get_order(order["id"]), "courier")["customer_reviews"]
        self.assertEqual([n["comment"] for n in rev["notes"]], ["отзыв 5", "отзыв 4", "отзыв 3"])  # последние три
        self.assertEqual((rev["total"], rev["user_id"]), (5, CUSTOMER.id))
        for who in (COURIER, ADMIN):  # «Посмотреть все отзывы»
            full = self.call(who, "GET", f"/api/staff/customers/{CUSTOMER.id}/reviews")
            self.assertEqual([n["comment"] for n in full["reviews"]], [f"отзыв {i}" for i in range(5, 0, -1)])
            self.assertEqual({n["courier_name"] for n in full["reviews"]}, {"Пётр", "Иван"})
        with self.assertRaises(ApiError) as ctx:  # покупателю — нельзя
            self.call(CUSTOMER, "GET", f"/api/staff/customers/{CUSTOMER.id}/reviews")
        self.assertEqual(ctx.exception.status, 403)

    # ---------------------------------------------------------------- функции классической админки
    def test_admin_catalog_photo(self):
        st = self.call(ADMIN, "PUT", "/api/admin/catalog-photo", {"image": PNG_1PX})
        self.assertIsNotNone(st["catalog_photo"])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/catalog")["banner"], st["catalog_photo"])
        self.assertIsNone(self.call(ADMIN, "PUT", "/api/admin/catalog-photo", {"image": None})["catalog_photo"])
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "PUT", "/api/admin/catalog-photo", {"image": None})
        self.assertEqual(ctx.exception.status, 403)
    def test_admin_test_log(self):
        self.assertTrue(self.call(ADMIN, "POST", "/api/admin/test-log")["ok"])
        self.assertIn("test_log_group", self.events())

    def test_admin_wipes_need_password(self):
        order = self.cash_order()
        for path in ("/api/admin/wipe-orders", "/api/admin/wipe-products"):
            for bad in ({}, {"password": "delete"}, {"password": "123"}):
                with self.assertRaises(ApiError) as ctx:
                    self.call(ADMIN, "POST", path, bad)
                self.assertEqual(ctx.exception.status, 403)
            with self.assertRaises(ApiError):
                self.call(COURIER, "POST", path, {"password": "AdminGas"})
        self.assertIsNotNone(self.db.get_order(order["id"]))
        self.assertEqual(self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})["deleted"], 1)
        self.assertIsNone(self.db.get_order(order["id"]))
        self.assertGreater(self.call(ADMIN, "POST", "/api/admin/wipe-products", {"password": "AdminGas"})["deleted"], 0)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/catalog")["products"], [])

    def test_admin_purge_product_and_delete_courier(self):
        self.add_to_cart(product_id=2)
        self.call(ADMIN, "POST", "/api/admin/products/2/hide")
        self.call(ADMIN, "DELETE", "/api/admin/products/2")
        self.assertIsNone(self.db.get_product(2))
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "DELETE", "/api/admin/products/2")
        self.assertEqual(ctx.exception.status, 404)
        courier_id = next(c["id"] for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        order = self.cash_order()
        self.take(order)
        with self.assertRaises(ApiError) as ctx:  # заказ в работе — удалять нельзя
            self.call(ADMIN, "DELETE", f"/api/admin/couriers/{courier_id}")
        self.assertEqual(ctx.exception.status, 409)
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        self.call(ADMIN, "DELETE", f"/api/admin/couriers/{courier_id}")
        self.assertNotIn(courier_id, [c["id"] for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"]])
        self.assertIsNotNone(self.db.get_order(order["id"]))  # заказ остался в истории

    def test_admin_customers_rating(self):
        self.cash_order()
        customers = self.call(ADMIN, "GET", "/api/admin/customers")["customers"]
        anna = next(c for c in customers if c["telegram_id"] == CUSTOMER.id)
        self.assertEqual(anna["purchases"], 0)
        res = self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/rating", {"rating": 4, "note": "Постоянный клиент"})
        self.assertEqual((res["admin_rating"], res["admin_note"]), (4, "Постоянный клиент"))
        anna = next(c for c in self.call(ADMIN, "GET", "/api/admin/customers")["customers"] if c["telegram_id"] == CUSTOMER.id)
        self.assertEqual(anna["admin_rating"], 4)
        for bad in ({"rating": 0}, {"rating": 6}, {"rating": 3, "note": "x" * 501}):
            with self.assertRaises(ApiError):
                self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/rating", bad)
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "PUT", "/api/admin/customers/999999/rating", {"rating": 3})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "GET", "/api/admin/customers")
        self.assertEqual(ctx.exception.status, 403)

    # ---------------------------------------------------------------- оплата при курьере (СБП / криптовалюта)
    def pod_order(self, method, user=CUSTOMER, qty=1, qr_id=None):
        self.add_to_cart(user, qty=qty)
        body = {"payment_method": method, "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"}
        if qr_id is not None:
            body["payment_qr_id"] = qr_id
        return self.call(user, "POST", "/api/orders", body)["order"]

    def crypto_option(self, currency):
        return next(o for o in self.call(CUSTOMER, "GET", "/api/checkout")["crypto_options"] if o["currency"] == currency)

    def test_payment_methods_follow_admin_qr_codes(self):
        methods = [m["id"] for m in self.call(CUSTOMER, "GET", "/api/me")["payment_methods"]]
        self.assertEqual(methods, ["cash_on_delivery", "sbp", "crypto"])  # демо-QR из dev_server
        labels = [m["label"] for m in self.call(CUSTOMER, "GET", "/api/checkout")["payment_methods"]]
        self.assertEqual(labels, ["💵 Оплатить наличными", "📱 Оплатить по СБП", "💎 Оплатить криптовалютой"])
        self.assertIn("в присутствии курьера", self.call(CUSTOMER, "GET", "/api/checkout")["pay_on_delivery_warning"])
        for c in self.call(ADMIN, "GET", "/api/admin/payment-qr")["codes"]:
            if c["kind"] == "sbp":
                self.call(ADMIN, "DELETE", f"/api/admin/payment-qr/{c['id']}")
        self.assertNotIn("sbp", [m["id"] for m in self.call(CUSTOMER, "GET", "/api/me")["payment_methods"]])
        self.add_to_cart()
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "sbp", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1"})
        self.assertEqual(ctx.exception.status, 409)

    def test_admin_payment_qr_crud(self):
        # Криптовалюта: названия нет — оно собирается из монеты и сети (сеть — из списка).
        res = self.call(ADMIN, "POST", "/api/admin/payment-qr",
                        {"kind": "crypto", "title": "игнорируется", "currency": "usdt", "network": "BEP-20 (BNB Smart Chain)", "image": PNG_1PX})
        usdt = next(c for c in res["codes"] if c["title"] == "USDT · BEP-20 (BNB Smart Chain)")
        self.assertEqual((usdt["currency"], usdt["network"]), ("USDT", "BEP-20 (BNB Smart Chain)"))
        self.assertIn("TRC-20 (Tron)", next(c for c in res["currencies"] if c["code"] == "USDT")["networks"])
        # сеть не указана — берётся первая из списка монеты
        res = self.call(ADMIN, "POST", "/api/admin/payment-qr", {"kind": "crypto", "currency": "TON", "image": PNG_1PX})
        ton = next(c for c in res["codes"] if c["currency"] == "TON")
        self.assertEqual(ton["title"], "TON · TON")
        self.assertTrue(ton["image"].startswith(f"/api/media/payqr/{ton['id']}?v="))
        self.assertEqual(self.api.media_file_id("payqr", str(ton["id"]))[:8], "dev-img-")
        # СБП: название необязательно
        res = self.call(ADMIN, "POST", "/api/admin/payment-qr", {"kind": "sbp", "image": PNG_1PX})
        self.assertIn("СБП", [c["title"] for c in res["codes"] if c["kind"] == "sbp"])
        for bad in ({"kind": "card", "image": PNG_1PX},
                    {"kind": "crypto", "currency": "DOGE", "image": PNG_1PX},
                    {"kind": "crypto", "currency": "BTC", "network": "Ehtereum", "image": PNG_1PX},
                    {"kind": "sbp", "image": "nope"}):
            with self.assertRaises(ApiError):
                self.call(ADMIN, "POST", "/api/admin/payment-qr", bad)
        self.call(ADMIN, "DELETE", f"/api/admin/payment-qr/{ton['id']}")
        self.assertIsNone(self.api.media_file_id("payqr", str(ton["id"])))
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", "/api/admin/payment-qr", {"kind": "sbp", "image": PNG_1PX})
        self.assertEqual(ctx.exception.status, 403)
    def test_sbp_order_paid_at_courier(self):
        order = self.pod_order("sbp")
        self.assertTrue(order["pay_on_delivery"])
        self.assertIn("dispatch_new_order", self.events())  # сразу уходит курьерам, без скриншота
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/orders")["orders"], [])  # сами не берут
        self.assertFalse(self.api.order_json(self.db.get_order(order["id"]), "courier")["paid"])
        qr = self.call(COURIER, "GET", "/api/courier/payment-qr")
        self.assertEqual(qr["orders"], [])  # пока заказ не взят — кнопки неактивны
        self.take(order)
        qr = self.call(COURIER, "GET", "/api/courier/payment-qr")
        self.assertEqual(qr["orders"], [{"order_id": order["id"], "method": "sbp", "confirmed": False}])
        sbp = next(c for c in qr["codes"] if c["kind"] == "sbp")
        btc = next(c for c in qr["codes"] if c["currency"] == "BTC")
        screen = self.call(COURIER, "GET", f"/api/courier/orders/{order['id']}/pay/{sbp['id']}")
        self.assertEqual((screen["total_rub"], screen["amount"]), (2500, None))
        self.assertEqual(order["payment_qr"]["id"], sbp["id"])  # СБП — первый QR СБП из админки
        # «❗ Изменить валюту ❗»: курьер может показать любой QR, даже другой вид
        screen = self.call(COURIER, "GET", f"/api/courier/orders/{order['id']}/pay/{btc['id']}")
        self.assertEqual(screen["qr"]["currency"], "BTC")
        with self.assertRaises(ApiError) as ctx:  # «Доставлен» — только после подтверждения оплаты
            arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/payment-confirm", {"photo": "nope"})
        res = self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/payment-confirm",
                        {"photo": PNG_1PX, "qr_title": "СБП — Сбер"})
        self.assertTrue(res["order"]["payment_confirmed"])
        self.assertTrue(res["order"]["paid"])
        photo = [e for e in self.bridge.events if e["event"] == "send_payment_photo"][0]
        self.assertIn(f"заказу №{order['id']}", photo["caption"])
        self.assertIn("2 500 ₽", photo["caption"])
        done = arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})["order"]
        self.assertEqual(done["status"], "delivered")

    def test_crypto_order_quotes_fixed_at_checkout(self):
        order = self.pod_order("crypto", qty=2, qr_id=self.crypto_option("USDT")["id"])  # 5000 ₽
        quotes = self.call(ADMIN, "GET", "/api/admin/orders")["orders"][0]["crypto_quotes"]
        # 5000 / 95 = 52.631… → вверх до сотых = 52.64 USDT; 5000 / 9 500 000 = 0.000526315… → 0.00052632 BTC
        self.assertEqual(quotes, {"USDT": {"amount": "52.64", "rate": 95.0}, "BTC": {"amount": "0.00052632", "rate": 9500000.0}})
        self.assertNotIn("crypto_quotes", json.dumps(self.call(CUSTOMER, "GET", "/api/orders", scope="active")))
        self.take(order)
        usdt = next(c for c in self.call(COURIER, "GET", "/api/courier/payment-qr")["codes"] if c["currency"] == "USDT")
        screen = self.call(COURIER, "GET", f"/api/courier/orders/{order['id']}/pay/{usdt['id']}")
        self.assertEqual((screen["amount"], screen["rate"], screen["total_rub"]), ("52.64", 95.0, 5000))
        # кошелёк в монете, которой не было при оформлении, — курс считается при открытии QR
        self.call(ADMIN, "POST", "/api/admin/payment-qr", {"kind": "crypto", "title": "ETH", "currency": "ETH", "image": PNG_1PX})
        eth = next(c for c in self.call(COURIER, "GET", "/api/courier/payment-qr")["codes"] if c["currency"] == "ETH")
        screen = self.call(COURIER, "GET", f"/api/courier/orders/{order['id']}/pay/{eth['id']}")
        self.assertEqual(screen["amount"], "0.015152")  # 5000 / 330000 = 0.0151515… → вверх
        self.assertIn("курс на момент открытия", screen["rate_note"])

    def test_customer_chooses_coin_and_courier_can_change_it(self):
        options = self.call(CUSTOMER, "GET", "/api/checkout")["crypto_options"]
        usdt = next(o for o in options if o["currency"] == "USDT")
        btc = next(o for o in options if o["currency"] == "BTC")
        self.assertEqual(usdt["label"], "💵 USDT · TRC-20 (Tron)")  # смайлик, валюта, сеть
        self.assertTrue(btc["label"].startswith("🟠 BTC"))
        self.assertEqual((usdt["rate"], usdt["decimals"]), (95.0, 2))
        # несколько кошельков — без выбора валюты заказ не оформить
        self.add_to_cart()
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders",
                      {"payment_method": "crypto", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders",
                      {"payment_method": "crypto", "payment_qr_id": 99999, "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"})
        self.assertEqual(ctx.exception.status, 409)
        order = self.pod_order("crypto", qty=1, qr_id=usdt["id"])  # 2 500 ₽ (корзина уже с 1 шт.)
        mine = self.call(CUSTOMER, "GET", "/api/orders", scope="active")["orders"][0]
        self.assertEqual(mine["payment_qr"]["label"], "💵 USDT · TRC-20 (Tron)")
        self.assertEqual(mine["payment_qr"]["currency"], "USDT")
        self.assertEqual(mine["payment_qr"]["amount"], order["payment_qr"]["amount"])
        self.take(order)
        cur = self.call(COURIER, "GET", "/api/courier/orders", scope="current")["orders"][0]
        self.assertEqual(cur["payment_qr"]["id"], usdt["id"])
        self.assertTrue(cur["payment_qr"]["active"])
        # курьер меняет валюту на BTC — оплата подтверждается, в канале видна замена
        screen = self.call(COURIER, "GET", f"/api/courier/orders/{order['id']}/pay/{btc['id']}")
        self.assertIsNotNone(screen["amount"])
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/payment-confirm", {"photo": PNG_1PX, "qr_id": btc["id"]})
        caption = [e for e in self.bridge.events if e["event"] == "send_payment_photo"][-1]["caption"]
        self.assertIn("QR: 🟠 BTC", caption)
        self.assertIn("❗ Курьер изменил валюту: покупатель выбрал 💵 USDT · TRC-20 (Tron)", caption)
        self.assertIn(" BTC (курс 1 BTC", caption)

    def test_validation(self):
        self.add_to_cart()
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "1", "address": "Москва, ул. Тестовая, 1"})
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "коротко"})

    # ---------------------------------------------------------------- courier / admin
    def test_old_not_delivered_orders_still_managed_by_admin(self):
        order = self.cash_order()
        admin_take(self.call, order['id'])
        # кнопки «❌ Заказ не доставлен» у курьера больше нет — маршрута тоже
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/not-delivered", {"reason": "x"})
        self.assertIn(ctx.exception.status, (404, 405))
        # старые заказы в статусе «Не доставлен» остаются, админ ими управляет как раньше
        self.assertEqual(self.db.update_order_status(order["id"], "not_delivered")["status"], "not_delivered")
        with self.assertRaises(ApiError):  # уже не у курьера в работе
            arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered")
        back = self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "new"})["order"]
        self.assertEqual(back["status"], "new")
        self.assertIsNone(back["courier"])
        self.assertEqual(self.db.get_order(order["id"])["courier_id"], None)

    def test_badges_are_per_user_and_follow_order(self):
        before = {u: self.call(u, "GET", "/api/badges") for u in (CUSTOMER, COURIER, ADMIN)}
        self.assertNotIn("courier", before[CUSTOMER])
        self.assertNotIn("admin", before[CUSTOMER])
        self.assertNotIn("admin", before[COURIER])
        self.assertIn("courier", before[COURIER])
        self.assertIn("admin", before[ADMIN])
        order = self.cash_order()
        cust = self.call(CUSTOMER, "GET", "/api/badges")
        self.assertEqual(cust["orders_active"], before[CUSTOMER]["orders_active"] + 1)
        self.assertEqual(cust["orders"][str(order["id"])]["status"], "new")
        self.assertEqual(cust["cart"], 0)
        cour = self.call(COURIER, "GET", "/api/badges")["courier"]
        self.assertEqual(cour["available"], 0)  # 28.10.2026: «Свободных» у курьеров нет
        adm = self.call(ADMIN, "GET", "/api/badges")["admin"]
        self.assertEqual(adm["new"], before[ADMIN]["admin"]["new"] + 1)
        admin_take(self.call, order['id'])
        cour = self.call(COURIER, "GET", "/api/badges")["courier"]
        self.assertEqual(cour["available"], before[COURIER]["courier"]["available"])
        self.assertEqual(cour["current"], before[COURIER]["courier"]["current"] + 1)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["orders"][str(order["id"])]["status"], "accepted")
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/no-contact")
        adm = self.call(ADMIN, "GET", "/api/badges")["admin"]
        self.assertEqual(adm["disputed"], before[ADMIN]["admin"]["disputed"] + 1)
        self.assertEqual(adm["attention"], adm["new"] + adm["not_delivered"])

    def test_history_admin_and_courier(self):
        o1 = self.cash_order()
        admin_take(self.call, o1['id'])
        arrive(self.call, o1['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o1['id']}/delivered", {"rating": 5})
        o2 = self.cash_order()
        self.call(CUSTOMER, "POST", f"/api/orders/{o2['id']}/cancel")
        # курьер видит только свои доставленные
        h = self.call(COURIER, "GET", "/api/courier/history")
        ids = [o["id"] for o in h["orders"]]
        self.assertIn(o1["id"], ids)
        self.assertNotIn(o2["id"], ids)
        mine = next(o for o in h["orders"] if o["id"] == o1["id"])
        self.assertTrue(mine["address"])
        self.assertTrue(mine["items"])
        self.assertNotIn("delivered_sum", h)  # общую сумму курьеру не показываем
        # админ: кто отвёз, куда, что; фильтры
        a = self.call(ADMIN, "GET", "/api/admin/history", status="delivered")
        row = next(o for o in a["orders"] if o["id"] == o1["id"])
        self.assertIsNotNone(row["courier"])
        self.assertTrue(row["address"])
        self.assertTrue(row["items"])
        cancelled = self.call(ADMIN, "GET", "/api/admin/history", status="cancelled")
        self.assertIn(o2["id"], [o["id"] for o in cancelled["orders"]])
        by_courier = self.call(ADMIN, "GET", "/api/admin/history", status="all", courier=row["courier"]["id"])
        self.assertTrue(all(o["courier"] and o["courier"]["id"] == row["courier"]["id"] for o in by_courier["orders"]))
        with self.assertRaises(ApiError):
            self.call(ADMIN, "GET", "/api/admin/history", status="bogus")
        for user in (CUSTOMER, COURIER):
            with self.assertRaises(ApiError):
                self.call(user, "GET", "/api/admin/history")
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "GET", "/api/courier/history")

    def test_admin_assign_and_permissions(self):
        order = self.cash_order()
        for user in (CUSTOMER, COURIER):
            with self.assertRaises(ApiError) as ctx:
                self.call(user, "GET", "/api/admin/orders")
            self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "GET", "/api/courier/orders")
        self.assertEqual(ctx.exception.status, 403)
        data = self.call(ADMIN, "GET", "/api/admin/orders", scope="open")
        off = next(c for c in data["couriers"] if c["status"] == "offline")
        with self.assertRaises(ApiError) as ctx:  # 28.10.2026: не на смене — назначить нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/assign", {"courier_id": off["id"]})
        self.assertIn("не на смене", ctx.exception.message)
        courier_id = next(c for c in data["couriers"] if c["status"] == "online")["id"]
        assigned = self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/assign", {"courier_id": courier_id})["order"]
        self.assertEqual(assigned["status"], "accepted")
        with self.assertRaises(ApiError):  # «В пути» без курьера нельзя — а с курьером можно
            self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "bogus"})
        self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "in_transit"})
        self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "delivered"})
        self.assertEqual(self.db.get_order(order["id"])["status"], "delivered")

    def test_admin_products_and_theme(self):
        created = self.call(ADMIN, "POST", "/api/admin/products", {
            "name": "Новый товар", "description": "Описание", "price": "100", "price_exchange": "", "photo": PNG_1PX,
        })["product"]
        self.assertIsNone(created["price_exchange"])
        self.assertTrue(created["photo"].startswith("data:image/png"))
        self.add_to_cart(variant="exchange", qty=1)
        res = self.call(ADMIN, "PATCH", "/api/admin/products/1", {"price_exchange": ""})
        self.assertEqual(res["removed_cart_rows"], 1)
        self.call(ADMIN, "POST", "/api/admin/products/2/hide")
        ids = [p["id"] for p in self.call(CUSTOMER, "GET", "/api/catalog")["products"]]
        self.assertNotIn(2, ids)
        self.call(ADMIN, "POST", "/api/admin/products/2/restore")

        self.call(ADMIN, "POST", "/api/admin/dev/unlock", {"password": "devpass1"})
        theme = self.call(ADMIN, "PUT", "/api/admin/theme", {"theme": {"accent_color": "#22c55e", "button_style": "pill"}, "dev_password": "devpass1"})["theme"]
        self.assertEqual(theme["accent_color"], "#22c55e")
        self.assertEqual(theme["bg_color"], "#0f172a")  # остальное не затёрто
        for bad in ({"accent_color": "red"}, {"button_fill": "neon"}, {"menu_order": ["cart"]}):
            with self.assertRaises(ApiError):
                self.call(ADMIN, "PUT", "/api/admin/theme", {"theme": bad})

    def test_admin_couriers(self):
        res = self.call(ADMIN, "POST", "/api/admin/couriers", {"name": "Олег", "telegram_id": "5555", "username": "@oleg"})
        self.call(ADMIN, "POST", f"/api/admin/couriers/{res['id']}/active", {"active": False})
        couriers = {c["name"]: c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"]}
        self.assertFalse(couriers["Олег"]["is_active"])
        self.assertEqual(couriers["Олег"]["username"], "oleg")
        with self.assertRaises(ApiError):  # тот же Telegram ID
            self.call(ADMIN, "POST", "/api/admin/couriers", {"name": "Дубль", "telegram_id": "5555"})

    def test_unknown_route(self):
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "GET", "/api/nope")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "DELETE", "/api/me")
        self.assertEqual(ctx.exception.status, 405)


if __name__ == "__main__":
    unittest.main()


class VersionTests(unittest.TestCase):
    def test_index_assets_are_versioned(self):
        from webapp.version import assets_hash, render_index, version_text

        html = render_index()
        v = assets_hash()
        self.assertIn(f'"/static/app.js?v={v}"', html)
        self.assertIn(f'"/static/style.css?v={v}"', html)
        self.assertIn('<meta name="app-version"', html)
        self.assertTrue(version_text().startswith("ok\n"))


class ExportTests(unittest.TestCase):
    """Выгрузка в Excel: границы суток 12:00–12:00 МСК, только доставленные,
    услуга «Подъём на этаж», архив при сбросе истории, пароль AdminGas."""

    setUp = ApiFlowTests.setUp
    tearDown = ApiFlowTests.tearDown
    call = ApiFlowTests.call
    events = ApiFlowTests.events
    add_to_cart = ApiFlowTests.add_to_cart

    def deliver_at(self, created_utc: str, qty=1, lift=False, deliver=True):
        self.add_to_cart(qty=qty)
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000",
                "address": "Москва, ул. Тестовая, д. 1, кв. 2", "floor_lift": lift}
        order = self.call(CUSTOMER, "POST", "/api/orders", body)["order"]
        with self.db.connection() as conn:
            conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (created_utc, order["id"]))
        if deliver:
            admin_take(self.call, order['id'])
            arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        return order

    def export(self, **body):
        body.setdefault("password", "AdminGas")
        res = self.call(ADMIN, "POST", "/api/admin/export", body)
        token = res["url"].split("/")[2]
        data, name = self.api.get_export(token)
        from openpyxl import load_workbook
        import io
        self.wb = load_workbook(io.BytesIO(data))
        return res, self.wb["Продажи"]

    HEADER_ROW = 8

    def test_download_and_reset_table2(self):
        """«Скачать и обнулить»: пароль, копия в чат, новый период; таблица №1 не обнуляется и отмечает периоды."""
        from openpyxl import load_workbook
        import io
        self.deliver_at("2026-10-01T10:00:00+00:00")
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "POST", "/api/admin/export", {"kind": "reset", "deliver": "download", "password": "x"})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.db.export_resets(), [])
        res = self.call(ADMIN, "POST", "/api/admin/export", {"kind": "reset", "deliver": "download", "password": "AdminGas"})
        self.assertTrue(res["summary"]["backup_sent"])
        self.assertEqual(res["summary"]["deliveries"], 1)
        self.assertEqual(len(self.db.export_resets()), 1)
        self.assertEqual(len(self.db.get_sales_lines(None, None)["lines"]), len(self.db.get_sales_lines(None, None, (None, None))["lines"]))
        # новый заказ после сброса (время доставки хранится с точностью до секунды)
        import time as _t
        _t.sleep(1.1)
        self.deliver_at("2099-01-01T10:00:00+00:00")
        res2 = self.call(ADMIN, "POST", "/api/admin/export", {"kind": "reset", "deliver": "download", "password": "AdminGas"})
        self.assertEqual(res2["summary"]["deliveries"], 1)
        # таблица №2 после сброса пуста по заказам, но клиент и курьер остаются
        wb2 = load_workbook(io.BytesIO(self.api.get_export(res2["url"].split("/")[2])[0]))
        self.assertNotIn("Сбросы", wb2.sheetnames)
        # таблица №1 — всё за всё время + листы периодов
        res1, _ = self.export(kind="all", deliver="download")
        self.assertEqual(res1["summary"]["deliveries"], 2)
        self.assertIn("Сбросы", self.wb.sheetnames)
        ws = self.wb["Все заказы"]
        head = [c.value for c in ws[6]]
        col = head.index("Период")
        labels = [r[col].value for r in ws.iter_rows(min_row=7) if r[col].value]
        self.assertTrue(any(x.startswith("Период 1") for x in labels))
        self.assertTrue(any(x.startswith("Период 3") for x in labels))
        self.assertGreaterEqual(len(self.wb["Клиенты"]["A"]), 4)

    def table(self, ws):
        """Строки таблицы как словари {заголовок: значение}."""
        head = [c.value for c in ws[self.HEADER_ROW]]
        out = []
        for r in ws.iter_rows(min_row=self.HEADER_ROW + 1):
            values = [c.value for c in r]
            if values[0] is None and values[3] is None:
                continue
            out.append(dict(zip(head, values)))
        return out

    def col(self, ws, name, header_row=None):
        head = [c.value for c in ws[header_row or self.HEADER_ROW]]
        return head.index(name) + 1

    def test_export_is_always_all_orders_with_totals(self):
        from datetime import date, time as dtime
        from openpyxl.utils import get_column_letter

        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 300})
        self.deliver_at("2026-10-16T08:59:59+00:00")              # 11:59 МСК
        self.deliver_at("2026-10-16T09:00:00+00:00", qty=2)       # 12:00 МСК
        self.deliver_at("2026-10-16T20:30:00+00:00", lift=True)   # 23:30 МСК + услуга
        self.deliver_at("2026-10-17T08:59:00+00:00", deliver=False)  # не доставлен — не попадает
        # суточной выгрузки больше нет: даже kind=daily отдаёт всё с первого заказа
        res, ws = self.export(kind="daily", day="2026-10-16", deliver="download")
        self.assertIn("полная выгрузка с первого заказа (с 16.10.2026)", ws["A1"].value)
        rows = [r for r in self.table(ws) if r["Заказ №"]]
        # Дата и время — настоящие значения Excel (фильтр по дате работает по дням/месяцам).
        self.assertEqual([(r["Дата"].date(), r["Время"], r["Тип"], r["Товар / услуга"], r["Вариант"], r["Цена за шт., ₽"],
                           r["Кол-во"], r["Сумма, ₽"], r["Оплата"]) for r in rows], [
            (date(2026, 10, 16), dtime(11, 59), "Товар", "Баллон 8 л", "новый", 2500, 1, 2500, "Наличные"),
            (date(2026, 10, 16), dtime(12, 0), "Товар", "Баллон 8 л", "новый", 2500, 2, 5000, "Наличные"),
            (date(2026, 10, 16), dtime(23, 30), "Товар", "Баллон 8 л", "новый", 2500, 1, 2500, "Наличные"),
            (date(2026, 10, 16), dtime(23, 30), "Подъём", "Подъём на этаж", None, 300, None, 300, "Наличные"),
        ])
        # Доставка и подъём: да/нет (числа 1/0) — в первой строке каждого заказа
        self.assertEqual([(r["Доставка"], r["Подъём"]) for r in rows], [(1, 0), (1, 0), (1, 1), (None, None)])
        self.assertEqual(ws.cell(row=9, column=self.col(ws, "Подъём")).number_format, '"да";"да";"нет"')
        self.assertEqual((rows[3]["Этажей подъёма"], rows[0]["Месяц"], rows[0]["День недели"]), (1, "2026-10 октябрь", "пт"))
        self.assertEqual((rows[0]["Покупатель"], rows[0]["Telegram"], rows[0]["Telegram ID"], rows[0]["Курьер"]),
                         ("Анна Покупатель (@anna_dev)", "@anna_dev", CUSTOMER.id, "Пётр"))
        # Итоги над таблицей: по выборке (формула для фильтра) и всё (готовые числа).
        L = lambda name: get_column_letter(self.col(ws, name))  # noqa: E731
        self.assertEqual(ws[f"{L('Кол-во')}5"].value, f"=SUBTOTAL(109,{L('Кол-во')}9:{L('Кол-во')}12)")
        self.assertEqual(ws[f"{L('Доставка')}5"].value, f"=SUBTOTAL(109,{L('Доставка')}9:{L('Доставка')}12)")
        total = {name: ws[f"{L(name)}6"].value for name in ("Кол-во", "Новых, шт", "На замену, шт", "Сумма, ₽", "Доставка", "Подъём")}
        self.assertEqual(total, {"Кол-во": 4, "Новых, шт": 4, "На замену, шт": 0, "Сумма, ₽": 10300, "Доставка": 3, "Подъём": 1})
        self.assertEqual(ws.auto_filter.ref, f"A8:{get_column_letter(ws.max_column)}12")
        self.assertEqual(res["summary"], {"lines": 4, "quantity": 4, "deliveries": 3, "lifts": 1, "total": 10300,
                                          "wipes": 0, "unfinished": 1})
        self.assertTrue(any("не завершено" in str(c.value) for r in ws.iter_rows() for c in r if c.value))
        self.assertIn("Оплат криптовалютой в этом периоде нет", ws["A3"].value)

    def test_all_export_keeps_wiped_history_marked_and_counted(self):
        self.deliver_at("2026-10-10T10:00:00+00:00", qty=3)
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        self.deliver_at("2026-10-12T10:00:00+00:00")
        res, ws = self.export(kind="all", deliver="download")
        rows = self.table(ws)
        archived = [r for r in rows if r["Сброшен"] == "сброшен"]
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0]["Товар / услуга"], archived[0]["Кол-во"], archived[0]["Сумма, ₽"]), ("Баллон 8 л", 3, 7500))
        self.assertIn("Сброшен из истории", archived[0]["Примечание"])
        self.assertTrue(any(r["Товар / услуга"] == "⚠️ Сброс истории заказов" for r in rows))
        # ИТОГО считает и сброшенные, и текущие
        self.assertEqual((ws.cell(row=6, column=self.col(ws, "Кол-во")).value,
                          ws.cell(row=6, column=self.col(ws, "Сумма, ₽")).value,
                          ws.cell(row=6, column=self.col(ws, "Доставка")).value), (4, 10000, 2))
        self.assertEqual((archived[0]["Вариант"], archived[0]["Цена за шт., ₽"], archived[0]["Доставка"]), ("новый", 2500, 1))
        self.assertEqual(res["summary"]["quantity"], 4)
        self.assertEqual(res["summary"]["total"], 10000)
        self.assertIn("с первого заказа (с 10.10.2026)", ws["A1"].value)

    def test_full_export_sheets_orders_clients_couriers(self):
        from openpyxl.utils import get_column_letter
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 300})
        o1 = self.deliver_at("2026-10-10T10:00:00+00:00", qty=2, lift=True)   # доставлен, оценки
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        o2 = self.deliver_at("2026-10-12T10:00:00+00:00")
        self.call(CUSTOMER, "POST", f"/api/orders/{o2['id']}/rate", {"rating": 4})
        o3 = self.deliver_at("2026-10-13T10:00:00+00:00", deliver=False)
        self.call(CUSTOMER, "POST", f"/api/orders/{o3['id']}/cancel")
        res, ws = self.export(kind="all", deliver="download")
        self.assertEqual(self.wb.sheetnames, ["Продажи", "Все заказы", "Клиенты", "Курьеры", "Курьеры по дням", "Курьеры — доставки", "Партнёры", "Администраторы", "Чаты курьер–покупатель", "Крипта"])
        def table(name, header_row=3):
            sh = self.wb[name]
            head = [c.value for c in sh[header_row]]
            return [dict(zip(head, [c.value for c in r])) for r in sh.iter_rows(min_row=header_row + 1) if r[0].value is not None]
        allo = table("Все заказы", 6)
        self.assertEqual([(r["Заказ №"], r["Статус"]) for r in allo], [(o1["id"], "Доставлен"), (o2["id"], "Доставлен"), (o3["id"], "Отменён")])
        first = allo[0]
        self.assertEqual(first["Сброшен"], 1)
        self.assertRegex(first["Дата сброса"], r"^\d\d\.\d\d\.\d{4} \d\d:\d\d$")
        # одно значение в ячейке: доставка/подъём да-нет, баллоны, деньги по отдельности
        self.assertEqual([(r["Доставка"], r["Подъём"], r["Новых, шт"], r["На замену, шт"], r["Товары, ₽"], r["Подъём, ₽"], r["Итого, ₽"])
                          for r in allo], [(1, 1, 2, 0, 5000, 300, 5300), (1, 0, 1, 0, 2500, 0, 2500), (0, 0, 1, 0, 2500, 0, 2500)])
        self.assertEqual((first["Этажей подъёма"], allo[1]["Сброшен"], allo[1]["Пришёл от партнёра"]), (1, 0, None))
        sh = self.wb["Все заказы"]
        self.assertEqual(sh["A4"].value, "ИТОГО ПО ВЫБОРКЕ (фильтр)")
        head = [c.value for c in sh[6]]
        dl = get_column_letter(head.index("Доставка") + 1)
        self.assertEqual(sh[f"{dl}4"].value, f"=SUBTOTAL(109,{dl}7:{dl}9)")
        self.assertEqual(sh[f"{dl}5"].value, 2)
        self.assertEqual(sh[f"{get_column_letter(head.index('Подъём') + 1)}5"].value, 1)
        self.assertEqual(sh[f"{get_column_letter(head.index('Заказ №') + 1)}4"].value.startswith("=SUBTOTAL(103,"), True)
        self.assertEqual(sh.auto_filter.ref, f"A6:{get_column_letter(len(head))}9")
        self.assertEqual((first["Покупатель"], first["Telegram"], first["Телефон"]), ("Анна Покупатель (@anna_dev)", "@anna_dev", "8 900 000 00 00"))
        self.assertIn("Москва, ул. Тестовая", first["Адрес"])
        self.assertIn("Баллон 8 л × 2", first["Что заказано"])
        self.assertIn("Подъём на этаж", first["Что заказано"])
        self.assertEqual((first["Курьер"], first["Оценка покупателю"]), ("Пётр", 5))  # сохранено при сбросе
        self.assertEqual(allo[1]["Оценка курьеру"], 4)
        clients = table("Клиенты")
        anna = next(r for r in clients if r["Telegram ID"] == CUSTOMER.id)
        self.assertEqual((anna["Telegram"], anna["Заказов"], anna["Доставлено"], anna["Отменено / не доставлено"]), ("@anna_dev", 3, 2, 1))
        self.assertEqual(anna["Сумма покупок, ₽"], 5000 + 300 + 2500)
        self.assertEqual((anna["Новых баллонов"], anna["Баллонов на замену"], anna["Подъёмов"]), (3, 0, 1))
        self.assertIn("8 900 000 00 00", anna["Телефоны"])
        self.assertIn("Баллон 8 л × 3", anna["Что заказывал (доставлено)"])
        self.assertEqual(anna["Рейтинг от курьеров"], "5.0 (2 оценки)")
        couriers = table("Курьеры")
        petr = next(r for r in couriers if r["Курьер"] == "Пётр")
        self.assertEqual((petr["Telegram"], petr["Доставлено заказов"]), ("@petr_courier", 2))
        self.assertEqual((petr["Новых баллонов"], petr["Подъёмов"]), (3, 1))
        self.assertTrue(petr["Рейтинг"].startswith("4.0 ") or petr["Рейтинг"].endswith("оценка)"))
        # день доставки — по времени закрытия заказа (в тесте оба закрыты сегодня)
        from webapp.export import msk as _msk
        today = _msk(datetime.now(timezone.utc).isoformat()).date()
        self.assertEqual(petr["Рабочих дней"], 1)
        days = table("Курьеры по дням", 6)
        self.assertEqual([(d["Курьер"], d["Дата"].date(), d["Доставок"], d["Баллонов всего"], d["Подъёмов"])
                          for d in days], [("Пётр", today, 2, 3, 1)])
        self.assertEqual((days[0]["Сумма, ₽"], days[0]["Наличными, ₽"], days[0]["СБП, ₽"]), (7800, 7800, 0))
        self.assertIn("Москва, ул. Тестовая", days[0]["Адреса за день"])
        self.assertEqual(self.wb["Курьеры по дням"]["A4"].value, "ИТОГО ПО ВЫБОРКЕ (фильтр)")
        trips = table("Курьеры — доставки", 6)
        self.assertEqual(len(trips), 2)
        lifted = next(t for t in trips if t["Подъём"] == 1)  # o1 (из сброшенной истории)
        plain = next(t for t in trips if t["Подъём"] == 0)   # o2
        self.assertIn("Баллон 8 л × 2", lifted["Что отвёз"])
        self.assertNotIn("Подъём", lifted["Что отвёз"])
        self.assertEqual((plain["Сумма, ₽"], plain["Оценка от покупателя"]), (2500, 4))
        from webapp.export import rating_text
        self.assertEqual([rating_text(5, 1), rating_text(4.75, 4), rating_text(3.24, 25), rating_text(None, 0)],
                         ["5.0 (1 оценка)", "4.8 (4 оценки)", "3.2 (25 оценок)", "нет оценок"])

    def test_admins_sheet_logs_who_changed_bonuses(self):
        from webapp.auth import TelegramUser
        admin2 = TelegramUser(4004, "Ольга", None, "olga_admin")
        self.api.admin_ids = frozenset({ADMIN.id, admin2.id, 5005})  # 5005 — в ADMIN_ID, но ещё не заходил
        self.call(CUSTOMER, "GET", "/api/me")
        self.call(admin2, "GET", "/api/me")
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 500, "reason": "акция"})
        self.call(admin2, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": -200, "reason": "ошибка"})
        self.call(admin2, "PUT", f"/api/admin/customers/{CUSTOMER.id}/bonus-percent", {"percent": "3"})
        admins = self.call(ADMIN, "GET", "/api/admin/admins")["admins"]
        self.assertEqual([(a["telegram_id"], a["name"], a["actions"], a["me"]) for a in admins],
                         [(ADMIN.id, "Админ Тестовый", 1, True), (4004, "Ольга", 2, False), (5005, None, 0, False)])
        self.export()
        ws = self.wb["Администраторы"]
        self.assertIn("Администраторы", self.wb.sheetnames)
        head = [c.value for c in ws[3]]
        listed = {r[0]: dict(zip(head, r)) for r in ws.iter_rows(min_row=4, max_row=6, values_only=True)}
        self.assertEqual((listed[4004]["Начислил бонусов"], listed[4004]["Списал бонусов"], listed[4004]["Всего действий"]), (0, 200, 2))
        self.assertEqual((listed[ADMIN.id]["Начислил бонусов"], listed[5005]["Администратор"]), (500, "ещё не заходил в приложение"))
        hr = 4 + 3 + 3
        log_head = [c.value for c in ws[hr]]
        log = [dict(zip(log_head, r)) for r in ws.iter_rows(min_row=hr + 1, values_only=True) if r[0] is not None]
        self.assertEqual([(x["Администратор"], x["Действие"], x["Бонусы / баллы / ₽"], x["Остаток после"], x["Причина / детали"]) for x in log],
                         [("Ольга (@olga_admin)", "Бонусы: процент", None, None, "3 %"),
                          ("Ольга (@olga_admin)", "Бонусы: списание", -200, 300, "ошибка"),
                          ("Админ Тестовый (@dev_admin)", "Бонусы: начисление", 500, 500, "акция")])
        self.assertEqual(log[1]["Telegram ID клиента"], CUSTOMER.id)

    def test_crypto_payments_in_export(self):
        codes = self.call(ADMIN, "GET", "/api/admin/payment-qr")["codes"]
        usdt = next(c for c in codes if c["currency"] == "USDT")
        btc = next(c for c in codes if c["currency"] == "BTC")
        self.add_to_cart(qty=1)  # 2 500 ₽
        order = self.call(CUSTOMER, "POST", "/api/orders", {
            "payment_method": "crypto", "payment_qr_id": usdt["id"], "phone": "+79000000000",
            "address": "Москва, ул. Тестовая, д. 1, кв. 2"})["order"]
        admin_take(self.call, order['id'])
        self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/payment-confirm", {"photo": PNG_1PX, "qr_id": usdt["id"]})
        arrive(self.call, order['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{order['id']}/delivered", {"rating": 5})
        # второй заказ: покупатель выбрал USDT, курьер принял в BTC — в выгрузке BTC
        self.add_to_cart(qty=2)  # 5 000 ₽
        o2 = self.call(CUSTOMER, "POST", "/api/orders", {
            "payment_method": "crypto", "payment_qr_id": usdt["id"], "phone": "+79000000000",
            "address": "Москва, ул. Тестовая, д. 1, кв. 2"})["order"]
        admin_take(self.call, o2['id'])
        self.call(COURIER, "POST", f"/api/courier/orders/{o2['id']}/payment-confirm", {"photo": PNG_1PX, "qr_id": btc["id"]})
        arrive(self.call, o2['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o2['id']}/delivered", {"rating": 5})
        res, ws = self.export(kind="all", deliver="download")
        rows = [r for r in self.table(ws) if isinstance(r["Кол-во"], int)]
        first = next(r for r in rows if r["Заказ №"] == order["id"])
        # 2500 / 95 = 26.315… → 26.32 USDT; по курсу выгрузки (dev: 100 ₽) = 2 632 ₽
        self.assertEqual((first["Оплата"], first["Монета"], first["Сеть"]), ("Крипта", "USDT", "TRC-20 (Tron)"))
        self.assertEqual((first["Сумма в крипте"], first["Курс при заказе"], first["Курс на выгрузку"],
                          first["По курсу выгрузки, ₽"]), (26.32, 95.0, 100.0, 2632))
        second = next(r for r in rows if r["Заказ №"] == o2["id"])
        self.assertEqual((second["Монета"], second["Сеть"]), ("BTC", "Bitcoin"))
        self.assertEqual(second["Сумма в крипте"], 0.00052632)  # 5000 / 9 500 000 вверх
        self.assertIn("1 USDT = 100.00 ₽", ws["A3"].value)
        crypto = self.wb["Крипта"]
        texts = [str(c.value) for r in crypto.iter_rows() for c in r if c.value is not None]
        self.assertIn("USDT · TRC-20 (Tron)", texts)
        self.assertIn("Итого по монетам", texts)
        # после сброса истории данные об оплате криптой сохраняются в архиве
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        res, ws = self.export(kind="all", deliver="download")
        arch = next(r for r in self.table(ws) if r["Заказ №"] == order["id"] and r["Сброшен"] == "сброшен")
        self.assertEqual((arch["Монета"], arch["Сеть"], arch["Сумма в крипте"]), ("USDT", "TRC-20 (Tron)", 26.32))

    def test_export_password_and_chat(self):
        for bad in ("", "admingas", "ADMINGAS", "DELETE"):
            with self.assertRaises(ApiError) as ctx:
                self.call(ADMIN, "POST", "/api/admin/export", {"kind": "all", "password": bad})
            self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", "/api/admin/export", {"kind": "all", "password": "AdminGas"})
        self.assertEqual(ctx.exception.status, 403)
        res = self.call(ADMIN, "POST", "/api/admin/export", {"kind": "all", "password": "AdminGas", "deliver": "chat"})
        self.assertTrue(res["sent"])
        self.assertIn("send_document", self.events())
        self.assertIsNone(self.api.get_export("nope"))


class MultiAdminTests(unittest.TestCase):
    """ADMIN_ID на Amvera: до 5 Telegram ID через запятую."""

    def load_config(self, admin_value):
        import importlib
        import os
        from unittest import mock

        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "ADMIN_ID": admin_value, "SQLITE_PATH": str(Path(tempfile.gettempdir()) / "x.db")}
        with mock.patch.dict(os.environ, env):
            sys.modules.pop("config", None)
            try:
                return importlib.import_module("config")
            finally:
                sys.modules.pop("config", None)

    def test_formats(self):
        for raw in ("111111111,222222222", "111111111, 222222222", " 111111111 ,222222222 ", "111111111,222222222,"):
            cfg = self.load_config(raw)
            self.assertEqual(cfg.ADMIN_IDS, (111111111, 222222222), raw)
            self.assertEqual(cfg.ADMIN_ID, 111111111)
        self.assertEqual(self.load_config("111111111").ADMIN_IDS, (111111111,))
        self.assertEqual(self.load_config("1,2,3,4,5").ADMIN_IDS, (1, 2, 3, 4, 5))
        self.assertEqual(self.load_config("7,7,8").ADMIN_IDS, (7, 8))  # повтор не считается

    def test_invalid(self):
        for raw in (",".join(str(i) for i in range(1, 22)), "111 222", "abc", " , "):  # 21 админ — уже много
            with self.assertRaises(RuntimeError, msg=raw):
                self.load_config(raw)

    def test_second_admin_has_full_rights(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        api, _bridge, _db = dev_server.build(Path(tmp.name) / "t.sqlite3", reset=True)
        second = TelegramUser(4004, "Второй", "Админ", "admin2")
        api.admin_ids = frozenset({ADMIN.id, second.id})
        call = lambda user, method, path, body=None: asyncio.run(api.handle(method, path, user, body or {}, {}))
        self.assertTrue(call(second, "GET", "/api/me")["roles"]["admin"])
        self.assertTrue(call(ADMIN, "GET", "/api/me")["roles"]["admin"])
        self.assertFalse(call(CUSTOMER, "GET", "/api/me")["roles"]["admin"])
        call(second, "GET", "/api/admin/customers")  # админ-раздел открывается
        with self.assertRaises(ApiError):  # другого админа заблокировать нельзя
            call(ADMIN, "PUT", f"/api/admin/customers/{second.id}/block", {"blocked": True})

    def test_api_accepts_tuple(self):
        from webapp.core import Api

        api = Api(None, (5, 6), None)
        self.assertTrue(api.is_admin(5) and api.is_admin(6))
        self.assertFalse(api.is_admin(7))
        self.assertEqual(api.admin_id, 5)

    def test_send_to_admins_reaches_everyone(self):
        import os
        from unittest import mock

        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "ADMIN_ID": "1,2,3", "SQLITE_PATH": str(Path(tempfile.gettempdir()) / "x.db")}
        with mock.patch.dict(os.environ, env):
            import main as m
        sent = []

        class FakeBot:
            async def send_message(self, chat_id, text, **kw):
                if chat_id == 2:
                    raise RuntimeError("blocked the bot")
                sent.append((chat_id, text))
                return chat_id

        with mock.patch.object(m, "ADMIN_IDS", (1, 2, 3)):
            first = asyncio.run(m.send_to_admins(FakeBot(), "send_message", "hi"))
            self.assertEqual(first, 1)
            self.assertEqual(sent, [(1, "hi"), (3, "hi")])  # сбой у №2 не мешает №3
            self.assertTrue(m.is_admin(3) and not m.is_admin(4))

        class DeadBot:
            async def send_message(self, chat_id, text, **kw):
                raise RuntimeError("down")

        with mock.patch.object(m, "ADMIN_IDS", (1, 2)), self.assertRaises(RuntimeError):
            asyncio.run(m.send_to_admins(DeadBot(), "send_message", "hi"))


class PartnerMigrationTests(unittest.TestCase):
    def test_old_per_order_rate_becomes_per_cylinder_rate(self):
        import sqlite3 as _sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.sqlite3"
        _api, _bridge, db = dev_server.build(path, reset=True)
        db.upsert_user(1001, "anna", "Анна", None)
        db.add_referral_request(1001, "partner")
        rid = db.get_referral_requests()[0]["id"]
        db.approve_partner(rid, 1, 1)
        with _sqlite3.connect(path) as conn:  # как было до обновления: только reward_points = 200
            conn.execute("UPDATE referral_requests SET reward_points = 200, reward_new = NULL, reward_exchange = NULL")
        from db import Database
        partner = Database(path).get_partner(1001)
        self.assertEqual((partner["reward_new"], partner["reward_exchange"]), (200, 200))


class PaymentPhotoGroupTests(unittest.TestCase):
    def test_only_sbp_and_crypto_photos_go_to_group(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        api, bridge, db = dev_server.build(Path(tmp.name) / "t.sqlite3", reset=True)
        courier_on_shift(db)
        call = lambda user, method, path, body=None: asyncio.run(api.handle(method, path, user, body or {}, {}))  # noqa: E731
        sent = []
        orig = bridge.send_payment_photo

        async def spy(order_id, data, mime, caption):
            sent.append(order_id)
            return await orig(order_id, data, mime, caption)

        bridge.send_payment_photo = spy
        ids = {}
        for method in ("cash_on_delivery", "sbp"):
            call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
            o = call(CUSTOMER, "POST", "/api/orders", {"payment_method": method, "phone": "+79000000000",
                                                       "address": "Москва, ул. Тестовая, д. 1"})["order"]
            admin_take(call, o['id'])
            ids[method] = o["id"]
        with self.assertRaises(ApiError) as ctx:  # наличные — фото в группу не отправляется
            call(COURIER, "POST", f"/api/courier/orders/{ids['cash_on_delivery']}/payment-confirm", {"photo": PNG_1PX})
        self.assertEqual(ctx.exception.status, 409)
        call(COURIER, "POST", f"/api/courier/orders/{ids['sbp']}/payment-confirm", {"photo": PNG_1PX})
        self.assertEqual(sent, [ids["sbp"]])


class CustomerBonusTests(unittest.TestCase):
    """Бонусы покупателей: 2 % (или свой процент) от товаров по исходным ценам,
    за доставленный заказ от 6 000 ₽; списание при оформлении 1 бонус = 1 ₽."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "t.sqlite3", reset=True)
        courier_on_shift(self.db)
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 300})
        self.call(CUSTOMER, "GET", "/api/me")  # покупатель появляется в базе при первом входе

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, user, method, path, body=None, **query):
        return asyncio.run(self.api.handle(method, path, user, body or {}, {k: str(v) for k, v in query.items()}))

    def order(self, items, spend=0, lift=False, method="cash_on_delivery", deliver=True, user=CUSTOMER):
        for product_id, qty, *variant in items:
            self.call(user, "POST", "/api/cart", {"product_id": product_id, "variant": (variant or ["new"])[0], "quantity": qty})
        body = {"payment_method": method, "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1",
                "floor_lift": lift, "bonus_spend": spend}
        if method == "crypto":
            body["payment_qr_id"] = next(c for c in self.call(ADMIN, "GET", "/api/admin/payment-qr")["codes"] if c["currency"] == "USDT")["id"]
        o = self.call(user, "POST", "/api/orders", body)["order"]
        if deliver:
            admin_take(self.call, o['id'])
            arrive(self.call, o['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        return o

    def balance(self, user=CUSTOMER):
        return self.call(user, "GET", "/api/bonus")["balance"]

    def test_accrual_threshold_percent_and_rounding(self):
        me = self.call(CUSTOMER, "GET", "/api/me")["bonus"]
        self.assertEqual(me, {"balance": 0, "percent": 2.0, "min_items_total": 6000, "transfer_fee_percent": 10, "transfer_min": 10})
        self.order([(2, 2)], lift=True)            # 8 400 ₽ товаров (+ подъём 300 не считается) → 168
        self.assertEqual(self.balance(), 168)
        self.order([(2, 1), (3, 4)])               # ровно 6 000 ₽ — порог включительно → 120
        self.assertEqual(self.balance(), 288)
        self.order([(1, 2)], lift=True)            # 5 000 ₽ + подъём 300 — меньше 6 000 → 0
        self.assertEqual(self.balance(), 288)
        not_done = self.order([(2, 2)], deliver=False)   # не доставлен — бонусов нет
        self.assertEqual(self.balance(), 288)
        self.call(CUSTOMER, "POST", f"/api/orders/{not_done['id']}/cancel")
        # индивидуальный процент: 3.5 % от 8 400 = 294
        res = self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/bonus-percent", {"percent": "3,5"})
        self.assertEqual((res["bonus_percent"], res["effective_percent"]), (3.5, 3.5))
        self.order([(2, 2)])
        self.assertEqual(self.balance(), 288 + 294)
        # округление вниз: 3.5 % от 6 450 = 225.75 → 225
        self.order([(2, 1), (1, 1), (3, 1)])  # 4 200 + 2 500 + 450 = 7 150 → 250.25 → 250
        self.assertEqual(self.balance(), 288 + 294 + 250)
        self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/bonus-percent", {"percent": None})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/me")["bonus"]["percent"], 2.0)
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PUT", f"/api/admin/customers/{CUSTOMER.id}/bonus-percent", {"percent": 101})
        hist = self.call(CUSTOMER, "GET", "/api/bonus")["history"]
        self.assertEqual([h["amount"] for h in hist], [250, 294, 120, 168])
        self.assertEqual((hist[-1]["kind"], hist[-1]["order_total"], hist[-1]["percent"]), ("accrual", 8400, 2.0))

    def test_spend_limits_refund_and_courier_sum(self):
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 3000, "reason": "подарок"})
        self.assertEqual(self.balance(), 3000)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})  # 2 500
        body = {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1",
                "floor_lift": True}
        with self.assertRaises(ApiError) as ctx:  # больше суммы товаров минус 1 ₽
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": 2500})
        self.assertIn("не больше 2499", ctx.exception.message)
        with self.assertRaises(ApiError):  # отрицательное — нельзя
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": -5})
        o = self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": 2499})["order"]
        self.assertEqual((o["total"], o["bonus_spent"]), (1 + 300, 2499))  # подъём — деньгами
        self.assertEqual(self.balance(), 501)
        courier_view = self.api.order_json(self.db.get_order(o["id"]), "courier")
        self.assertEqual((courier_view["total"], courier_view["bonus_spent"]), (301, 2499))
        # отмена покупателем — бонусы возвращаются
        self.call(CUSTOMER, "POST", f"/api/orders/{o['id']}/cancel")
        self.assertEqual(self.balance(), 3000)
        # больше, чем есть
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 2, "variant": "new", "quantity": 2})  # 8 400
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "bonus_spend": 3001})
        self.assertIn("У вас 3000 бонусов", ctx.exception.message)
        # бонусы — от суммы после списания: 2 % от (8 400 − 1 000) = 148
        o = self.call(CUSTOMER, "POST", "/api/orders", {**body, "floor_lift": False, "bonus_spend": 1000})["order"]
        self.assertEqual(o["total"], 7400)
        admin_take(self.call, o['id'])
        arrive(self.call, o['id']) or self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        self.assertEqual(self.balance(), 3000 - 1000 + 148)
        # отмена админом — тоже возврат
        o = self.order([(1, 1)], spend=100, deliver=False)
        self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/status", {"status": "cancelled"})
        self.assertEqual(self.balance(), 2148)

    def test_crypto_quote_uses_discounted_total(self):
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 500})
        o = self.order([(1, 1)], spend=500, method="crypto", deliver=False)  # 2 500 − 500 = 2 000
        self.assertEqual(o["total"], 2000)
        quotes = crypto_loads(self.db.get_order(o["id"])["crypto_quotes"])
        self.assertAlmostEqual(float(quotes["USDT"]["amount"]), 2000 / 95, places=1)

    def test_disputed_orders_burn_bonuses(self):
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 1000})
        for finish in ("close", "cancel"):
            o = self.order([(1, 1)], spend=200, deliver=False)
            admin_take(self.call, o['id'])
            self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/arrived")
            self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/no-contact")
            if finish == "close":
                self.call(ADMIN, "POST", f"/api/admin/disputes/{o['id']}/close")
            else:
                self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/status", {"status": "cancelled"})
        self.assertEqual(self.balance(), 600)  # оба раза по 200 сгорели
        # «Вернуть в работу» и потом отмена покупателем до курьера — возврат
        o = self.order([(1, 1)], spend=100, deliver=False)
        admin_take(self.call, o['id'])
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/arrived")
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/no-contact")
        self.call(ADMIN, "POST", f"/api/admin/disputes/{o['id']}/return")
        self.call(CUSTOMER, "POST", f"/api/orders/{o['id']}/cancel")
        self.assertEqual(self.balance(), 600)

    def test_manual_adjust_admin_only_and_history(self):
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 100})
        self.assertEqual(ctx.exception.status, 403)
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 500, "reason": "акция"})
        with self.assertRaises(ApiError) as ctx:  # списать больше остатка нельзя
            self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": -501})
        self.assertEqual(ctx.exception.status, 409)
        res = self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": -200, "reason": "ошибка"})
        self.assertEqual(res["bonus_balance"], 300)
        hist = self.call(ADMIN, "GET", f"/api/admin/customers/{CUSTOMER.id}/bonus")
        self.assertEqual((hist["balance"], [(h["amount"], h["reason"]) for h in hist["history"]]),
                         (300, [(-200, "ошибка"), (500, "акция")]))
        self.assertEqual(self.bridge.events[-1]["event"], "bonus_event")
        customers = self.call(ADMIN, "GET", "/api/admin/customers")
        anna = next(c for c in customers["customers"] if c["telegram_id"] == CUSTOMER.id)
        self.assertEqual((anna["bonus_balance"], anna["bonus_percent"], customers["bonus_default_percent"]), (300, None, 2.0))
        # сброс истории заказов — бонусы остаются
        self.order([(2, 2)])
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        self.assertEqual(self.balance(), 300 + 168)


def crypto_loads(text):
    from webapp import crypto
    return crypto.loads(text)


class BonusExportTests(CustomerBonusTests):
    """Бонусы в выгрузке: строка «Оплата бонусами» в «Продажах», столбцы в «Все заказы» и «Клиенты»."""

    def test_export_shows_spent_and_accrued(self):
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 1000})
        o1 = self.order([(2, 2)], spend=400)                 # 8 400 − 400 = 8 000, начислено 2 % от 8 000 = 160
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        o2 = self.order([(1, 1)], spend=100, lift=True)      # 2 500 + 300 − 100 = 2 700, без начисления
        res = self.call(ADMIN, "POST", "/api/admin/export", {"password": "AdminGas", "deliver": "download"})
        data, _ = self.api.get_export(res["url"].split("/")[2])
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(data))

        def table(name, header_row):
            sh = wb[name]
            head = [c.value for c in sh[header_row]]
            return [dict(zip(head, [c.value for c in r])) for r in sh.iter_rows(min_row=header_row + 1)
                    if r[0].value is not None and r[4].value is not None]

        sales = table("Продажи", 8)
        bonus_rows = [(r["Заказ №"], r["Тип"], r["Сумма, ₽"], r["Кол-во"], r["Сброшен"]) for r in sales if r["Тип"] == "Бонусы"]
        self.assertEqual(sorted(bonus_rows, key=lambda b: b[2]),
                         [(o1["id"], "Бонусы", -400, None, "сброшен"), (o2["id"], "Бонусы", -100, None, None)])
        self.assertEqual(sum(r["Сумма, ₽"] for r in sales if r["Тип"]), 8000 + 2700)  # реально полученные деньги
        self.assertEqual(res["summary"]["quantity"], 3)  # бонусы и подъём — не баллоны
        orders = table("Все заказы", 6)
        got = [(r["Заказ №"], r["Товары, ₽"], r["Подъём, ₽"], r["Бонусами, ₽"], r["Итого, ₽"], r["Начислено бонусов"], r["Сброшен"])
               for r in orders]
        self.assertEqual(sorted(got, key=lambda g: -g[-1]),
                         [(o1["id"], 8400, 0, 400, 8000, 160, 1), (o2["id"], 2500, 300, 100, 2700, 0, 0)])
        anna = next(r for r in table("Клиенты", 3) if r["Telegram ID"] == CUSTOMER.id)
        self.assertEqual((anna["Бонусы, %"], anna["Остаток бонусов"], anna["Начислено бонусов"], anna["Потрачено бонусов"]),
                         (2.0, 1000 - 400 + 160 - 100, 1000 + 160, 500))


class ArrivalAndHistoryTests(CustomerBonusTests):
    def test_delivered_only_after_arrived(self):
        o = self.order([(1, 1)], deliver=False)
        admin_take(self.call, o['id'])
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("Я на месте", ctx.exception.message)
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/arrived")
        done = self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})["order"]
        self.assertEqual(done["status"], "delivered")

    def test_admin_history_has_full_details_and_referral(self):
        self.call(CUSTOMER2, "GET", "/api/me")
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "Кодовое слово"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        code = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")["partners"][0]["code"]
        self.call(ADMIN, "PATCH", "/api/admin/products/2", {"partner_reward": "200"})
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "bound")
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER2.id}/bonus", {"amount": 300})
        o = self.order([(2, 2)], spend=300, user=CUSTOMER2)   # 8 400 − 300 = 8 100 ₽: +162 бонуса, партнёру 2 × 200
        own = self.order([(1, 1)])                            # обычный заказ без партнёра
        hist = {h["id"]: h for h in self.call(ADMIN, "GET", "/api/admin/history", status="delivered")["orders"]}
        x = hist[o["id"]]["extra"]
        self.assertEqual(x["referral"]["id"], CUSTOMER.id)
        self.assertEqual((x["referral"]["points"], x["bonus_accrued"], x["customer_rating"]), (400, 162, 5))
        self.assertEqual((hist[o["id"]]["bonus_spent"], hist[o["id"]]["total"]), (300, 8100))
        self.assertIsNone(hist[own["id"]]["extra"]["referral"])


class SwitchesAndAnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "t.sqlite3", reset=True)
        courier_on_shift(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, user, method, path, body=None):
        return asyncio.run(self.api.handle(method, path, user, body or {}, {}))

    def methods(self):
        return [m["id"] for m in self.call(CUSTOMER, "GET", "/api/me")["payment_methods"]]

    def test_payment_switches_for_everyone(self):
        self.assertEqual(self.methods(), ["cash_on_delivery", "sbp", "crypto"])
        with self.assertRaises(ApiError) as ctx:  # только админ
            self.call(COURIER, "PUT", "/api/admin/payment-switch", {"method": "sbp", "enabled": False})
        self.assertEqual(ctx.exception.status, 403)
        res = self.call(ADMIN, "PUT", "/api/admin/payment-switch", {"method": "crypto", "enabled": False})
        self.assertEqual(res["payment_switches"], {"sbp": True, "crypto": False})
        self.assertEqual(self.methods(), ["cash_on_delivery", "sbp"])
        self.call(ADMIN, "PUT", "/api/admin/payment-switch", {"method": "sbp", "enabled": False})
        self.assertEqual(self.methods(), ["cash_on_delivery"])
        # оформить выключенным способом нельзя и в обход экрана
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "sbp", "phone": "+79000000000",
                                                        "address": "Москва, ул. Тестовая, д. 1"})
        self.assertEqual(ctx.exception.status, 409)
        self.call(ADMIN, "PUT", "/api/admin/payment-switch", {"method": "sbp", "enabled": True})
        self.assertEqual(self.methods(), ["cash_on_delivery", "sbp"])
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/settings")["payment_switches"], {"sbp": True, "crypto": False})
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PUT", "/api/admin/payment-switch", {"method": "cash_on_delivery", "enabled": False})

    def test_announcement_shown_to_everyone_only_when_set(self):
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["announcement"])
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "PUT", "/api/admin/announcement", {"text": "взлом"})
        text = "Внимание, магазин может корректно не работать, ведутся технические работы"
        self.call(ADMIN, "PUT", "/api/admin/announcement", {"text": f"  {text}  "})
        for user in (CUSTOMER, CUSTOMER2, COURIER):
            self.assertEqual(self.call(user, "GET", "/api/me")["announcement"]["text"], text)
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["announcement"]["text"], text)
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PUT", "/api/admin/announcement", {"text": "x" * 501})
        self.call(ADMIN, "PUT", "/api/admin/announcement", {"text": ""})
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["announcement"])


class SearchAndGreetingTests(CustomerBonusTests):
    def test_order_search_by_number_id_name_phone(self):
        self.call(CUSTOMER2, "GET", "/api/me")
        a = self.order([(1, 1)])
        b = self.order([(1, 1)], user=CUSTOMER2)
        hist = lambda q: [o["id"] for o in self.call(ADMIN, "GET", "/api/admin/history", status="all", q=q)["orders"]]  # noqa: E731
        self.assertEqual(hist(str(b["id"])), [b["id"]])
        self.assertEqual(hist(f"№{a['id']}"), [a["id"]])
        self.assertEqual(hist(f"{CUSTOMER2.id}_{b['id']}"), [b["id"]])
        self.assertEqual(hist(f"{CUSTOMER.id}_{b['id']}"), [])
        self.assertEqual(hist("@anna_dev"), [a["id"]])
        self.assertEqual(hist("борис"), [b["id"]])
        self.assertEqual(sorted(hist("+7 (900) 000-00")), sorted([a["id"], b["id"]]))
        self.assertEqual(sorted(hist("")), sorted([a["id"], b["id"]]))
        customers = self.call(ADMIN, "GET", "/api/admin/customers")["customers"]
        anna = next(c for c in customers if c["telegram_id"] == CUSTOMER.id)
        self.assertEqual(anna["phones"], ["8 900 000 00 00"])

    def test_home_greeting_editable(self):
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["greeting"])
        st = self.call(ADMIN, "GET", "/api/admin/settings")
        self.assertIn("Закажите газ", st["home_greeting_default"])
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "PUT", "/api/admin/home-greeting", {"text": "x"})
        self.call(ADMIN, "PUT", "/api/admin/home-greeting", {"text": " Тут вы можете заказать газ "})
        self.assertEqual(self.call(CUSTOMER2, "GET", "/api/me")["greeting"], "Тут вы можете заказать газ")
        with self.assertRaises(ApiError):
            self.call(ADMIN, "PUT", "/api/admin/home-greeting", {"text": "x" * 301})
        self.call(ADMIN, "PUT", "/api/admin/home-greeting", {"text": ""})
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/me")["greeting"])


class OrderNumberTests(CustomerBonusTests):
    def test_number_is_customer_id_and_global_sequence(self):
        self.call(CUSTOMER2, "GET", "/api/me")
        a = self.order([(1, 1)])
        b = self.order([(1, 1)], user=CUSTOMER2)
        self.assertEqual(a["number"], f"{CUSTOMER.id}_{a['id']}")
        self.assertEqual(b["number"], f"{CUSTOMER2.id}_{b['id']}")
        self.assertEqual(b["id"], a["id"] + 1)  # нумерация общая, а не своя у каждого
        mine = self.call(CUSTOMER, "GET", "/api/orders", scope="history")["orders"]
        self.assertEqual(mine[0]["number"], f"{CUSTOMER.id}_{a['id']}")
        # после сброса истории — снова с _1, а в выгрузке видны оба
        self.call(ADMIN, "POST", "/api/admin/wipe-orders", {"password": "AdminGas"})
        c = self.order([(1, 1)])
        self.assertEqual(c["number"], f"{CUSTOMER.id}_1")
        res = self.call(ADMIN, "POST", "/api/admin/export", {"password": "AdminGas", "deliver": "download"})
        data, _ = self.api.get_export(res["url"].split("/")[2])
        from openpyxl import load_workbook
        import io
        sh = load_workbook(io.BytesIO(data))["Все заказы"]
        head = [x.value for x in sh[6]]
        col = head.index("Номер заказа")
        numbers = [r[col].value for r in sh.iter_rows(min_row=7) if r[0].value is not None]
        self.assertEqual(sorted(numbers), sorted([f"{CUSTOMER.id}_1", f"{CUSTOMER.id}_1", f"{CUSTOMER2.id}_2"]))


class CustomerStatusLabelTests(unittest.TestCase):
    def test_new_order_shows_searching_courier(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        api, _b, _db = dev_server.build(Path(tmp.name) / "t.sqlite3", reset=True)
        call = lambda u, m, p, b=None: asyncio.run(api.handle(m, p, u, b or {}, {}))  # noqa: E731
        call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        o = call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "+79000000000",
                                                   "address": "Москва, ул. Тестовая, д. 1"})["order"]
        self.assertEqual(o["status_label"], "Ищем курьера")
        active = asyncio.run(api.handle("GET", "/api/orders", CUSTOMER, {}, {"scope": "active"}))["orders"]
        self.assertEqual(active[0]["status_label"], "Ищем курьера")
        admin_view = call(ADMIN, "GET", "/api/admin/orders")["orders"][0]
        self.assertEqual(admin_view["status_label"], "Активен")  # у админа и курьера — как было


class BonusAfterSpendTests(CustomerBonusTests):
    def test_base_after_spend_but_not_below_threshold(self):
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 2000})
        start = self.balance()
        # 4 200 + 5 × 450 = 6 450 ₽, списано 500 → 5 950 меньше порога → считаем от 6 000 = 120
        self.order([(2, 1), (3, 5)], spend=500)
        self.assertEqual(self.balance(), start - 500 + 120)
        # 10 900 ₽ (8 400 + 2 500) − 1 000 = 9 900 → 198
        before = self.balance()
        self.order([(2, 2), (1, 1)], spend=1000)
        self.assertEqual(self.balance(), before - 1000 + 198)
        hist = self.call(CUSTOMER, "GET", "/api/bonus")["history"]
        self.assertEqual((hist[0]["amount"], hist[0]["order_total"]), (198, 9900))
        # меньше 6 000 по исходным ценам — бонусов нет, даже без списания
        before = self.balance()
        self.order([(1, 2)])
        self.assertEqual(self.balance(), before)


class AddressCheckoutTests(CustomerBonusTests):
    """Своя база адресов OSM (в тестах — образец tools/sample_addresses.geojsonseq)."""

    def lesnaya(self):
        return next(x for x in self.call(CUSTOMER, "GET", "/api/address/suggest", q="лесн")["suggestions"]
                    if x["city"] == "Москва")

    def body(self, **parts):
        return {"payment_method": "cash_on_delivery", "phone": "+79000000000", "address_parts": parts}

    def test_order_comment_and_deliver_time(self):
        """Комментарий к заказу и «доставка ко времени»: сегодня по Москве, шаг 15 минут, не раньше чем через 2 часа."""
        from datetime import datetime, timezone
        from webapp.core import delivery_time_slots, deliver_at_from_slot, msk_hhmm
        noon = datetime(2026, 10, 25, 9, 7, tzinfo=timezone.utc)  # 12:07 по Москве
        slots = delivery_time_slots(noon)
        self.assertEqual((slots[0], slots[1], slots[-1], len(slots)), ("14:15", "14:30", "23:45", 39))
        self.assertIsNone(deliver_at_from_slot("14:00", noon))   # меньше 2 часов
        self.assertIsNone(deliver_at_from_slot("13:20", noon))   # не по шагу 15 минут
        self.assertEqual(deliver_at_from_slot("18:30", noon), "2026-10-25T15:30:00+00:00")
        self.assertEqual(msk_hhmm("2026-10-25T15:30:00+00:00"), "18:30")
        self.assertEqual(delivery_time_slots(datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)), [])  # 22:00 — поздно
        self.assertEqual(delivery_time_slots(datetime(2026, 10, 25, 18, 40, tzinfo=timezone.utc)), ["23:45"])
        info = self.call(CUSTOMER, "GET", "/api/checkout")
        self.assertEqual((info["time_slots"], info["comment_max"]), (delivery_time_slots(), 300))
        if not info["time_slots"]:
            self.skipTest("на сегодня по Москве слотов уже нет")
        slot = info["time_slots"][-1]
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        with self.assertRaises(ApiError):  # время не из списка
            self.call(CUSTOMER, "POST", "/api/orders", {**self.body(line="Москва, Лесная улица", house="7"), "deliver_time": "03:07"})
        order = self.call(CUSTOMER, "POST", "/api/orders", {**self.body(line="Москва, Лесная улица", house="7"),
                                                            "deliver_time": slot, "comment": "  Позвоните за 10 минут  "})["order"]
        self.assertEqual((order["deliver_time"], order["comment"]), (slot, "Позвоните за 10 минут"))
        adm = {o["id"]: o for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"]}[order["id"]]
        self.assertEqual((adm["deliver_time"], adm["comment"]), (slot, "Позвоните за 10 минут"))
        # обычный заказ — без времени и комментария
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        plain = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, Лесная улица", house="7"))["order"]
        self.assertEqual((plain["deliver_time"], plain["comment"]), (None, None))
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        with self.assertRaises(ApiError):  # длиннее 300 символов
            self.call(CUSTOMER, "POST", "/api/orders", {**self.body(line="Москва, Лесная улица", house="7"), "comment": "x" * 301})

    def test_suggest_streets_moscow_first(self):
        res = self.call(CUSTOMER, "GET", "/api/address/suggest", q="ул лесная")
        self.assertTrue(res["enabled"])
        self.assertIn("OpenStreetMap", res["attribution"])
        self.assertEqual([(x["street"], x["city"], x["moscow"]) for x in res["suggestions"]],
                         [("Лесная улица", "Москва", True), ("Лесная улица", "Химки", False)])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/address/suggest", q="л")["suggestions"], [])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/address/suggest", q="москвин")["suggestions"][0]["city"], "Тула")

    def test_order_with_street_house_digits_and_saved(self):
        st = self.lesnaya()
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        o = self.call(CUSTOMER, "POST", "/api/orders", {**self.body(
            line=st["value"], street_id=st["id"], house="7", building="2", entrance="2", apartment="12",
            floor="7", intercom="12"), "customer_name": "Art"})["order"]
        self.assertEqual(o["address"], "Лесная улица, Москва, д. 7, корп./стр. 2, подъезд 2, кв. 12, этаж 7, домофон 12")
        self.assertEqual(o["customer_location"], {"lat": 55.7822, "lon": 37.5915})  # дом 7к2 из базы
        self.assertFalse(o["individual"])
        saved = self.call(CUSTOMER, "GET", "/api/checkout")["saved_addresses"][0]
        self.assertEqual((saved["house"], saved["building"], saved["street_id"]), ("7", "2", st["id"]))
        for field, bad in (("house", "7а"), ("apartment", "12б"), ("floor", "-1"), ("entrance", "II"), ("intercom", "12К")):
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
            with self.assertRaises(ApiError) as ctx:
                self.call(CUSTOMER, "POST", "/api/orders", self.body(**{"line": st["value"], "street_id": st["id"], "house": "7", field: bad}))
            self.assertIn("только цифры", ctx.exception.message)
        with self.assertRaises(ApiError) as ctx:  # без номера дома
            self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Лесная улица, Москва"))
        self.assertIn("номер дома", ctx.exception.message)

    def test_map_point_to_address(self):
        r = self.call(CUSTOMER, "GET", "/api/address/geolocate", lat="55.78205", lon="37.59105")
        self.assertEqual((r["address"], r["house"], r["moscow"]), ("Лесная улица, Москва", "7", True))
        far = self.call(CUSTOMER, "GET", "/api/address/geolocate", lat="55.80", lon="37.70")
        self.assertEqual((far["address"], far["moscow"], far["reason"]), (None, True, "empty"))
        tula = self.call(CUSTOMER, "GET", "/api/address/geolocate", lat="54.1931", lon="37.6171")
        self.assertEqual((tula["address"], tula["moscow"]), ("Советская улица, Тула", False))


class IndividualOrderTests(AddressCheckoutTests):
    HEADER_ROW = ExportTests.HEADER_ROW
    def place(self, **parts):
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        body = self.body(**{k: v for k, v in parts.items() if k not in ("point", "prepay_agreed")})
        if "point" in parts:
            body["point"] = parts["point"]
        if parts.get("prepay_agreed"):
            body["prepay_agreed"] = True  # 30.10.2026: «Да, готов» внести предоплату за доставку
        return self.call(CUSTOMER, "POST", "/api/orders", body)["order"]

    def test_moscow_regular_other_city_individual(self):
        st = self.lesnaya()
        m = self.place(line=st["value"], street_id=st["id"], house="7")
        self.assertFalse(m["individual"])
        self.assertEqual(m["status_label"], "Ищем курьера")
        tula = self.call(CUSTOMER, "GET", "/api/address/suggest", q="советская")["suggestions"][0]
        t = self.place(line=tula["value"], street_id=tula["id"], house="1")
        self.assertTrue(t["individual"])
        self.assertEqual(t["status_label"], "Ждёт администратора")
        p = self.place(line="дорога у леса", house="1", point={"lat": 54.2, "lon": 37.6})  # точка вне Москвы
        self.assertTrue(p["individual"])
        available = [o["id"] for o in self.call(COURIER, "GET", "/api/courier/orders")["orders"]]
        self.assertEqual(available, [])  # 28.10.2026: курьеры сами заказы не берут
        with self.assertRaises(ApiError):  # «Передать курьерам» больше нет — назначает админ
            self.call(ADMIN, "POST", f"/api/admin/orders/{t['id']}/reoffer")
        admin_take(self.call, t['id'])
        released = {o["id"]: o for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"]}[t["id"]]
        self.assertEqual((released["individual"], released["individual_any"]), (False, True))
        self.call(ADMIN, "POST", f"/api/admin/orders/{p['id']}/assign", {"courier_id": 1})
        self.assertEqual(self.db.get_order(p["id"])["individual"], 2)

    def test_outside_delivery_auto_price(self):
        """За пределами Москвы: 500 ₽ + ₽ × км по дорогам за границей Москвы — обычный заказ."""
        from webapp.routing import KREMLIN, RouteService, haversine_km
        dest = {"lat": 54.2, "lon": 37.6}  # точка вне Москвы
        calls = []

        async def fake(params):  # «маршрут» — прямая из 200 отрезков от Кремля до точки
            calls.append(params)
            self.assertEqual(params["start"], f"{KREMLIN[1]},{KREMLIN[0]}")
            n = 200
            return {"features": [{"geometry": {"coordinates": [
                [KREMLIN[1] + (dest["lon"] - KREMLIN[1]) * i / n, KREMLIN[0] + (dest["lat"] - KREMLIN[0]) * i / n]
                for i in range(n + 1)]}}]}

        self.assertEqual(self.db.outside_prices(), {"base": 500, "per_km": 50})  # цена владельца по умолчанию
        # цена за км 0 — авторасчёт выключен
        self.api.routing = RouteService(None, fetch=fake)
        self.call(ADMIN, "PUT", "/api/admin/outside-delivery", {"base": 500, "per_km": 0})
        q = self.call(CUSTOMER, "GET", "/api/delivery/quote", lat=dest["lat"], lon=dest["lon"])
        self.assertEqual(q, {"available": False})
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "PUT", "/api/admin/outside-delivery", {"base": 500, "per_km": 40})
        r = self.call(ADMIN, "PUT", "/api/admin/outside-delivery", {"base": 500, "per_km": 40})
        self.assertEqual(r, {"base": 500, "per_km": 40, "key": True})
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/settings")["outside"], {"base": 500, "per_km": 40, "key": True})
        q = self.call(CUSTOMER, "GET", "/api/delivery/quote", lat=dest["lat"], lon=dest["lon"])
        whole = haversine_km(KREMLIN[0], KREMLIN[1], dest["lat"], dest["lon"])
        self.assertTrue(q["available"])
        self.assertTrue(50 < q["km"] < whole, (q["km"], whole))  # только часть за Москвой
        self.assertEqual((q["fee"], q["base"], q["per_km"]), (500 + 40 * q["km"], 500, 40))
        # точка в Москве — не считаем
        self.assertFalse(self.call(CUSTOMER, "GET", "/api/delivery/quote", lat=55.75, lon=37.62)["available"])
        # заказ: доставка посчитана — обычный заказ, видят курьеры (регионы включены)
        courier_on_shift(self.db)
        with self.assertRaises(ApiError) as ctx:  # без согласия на предоплату заказ не создаётся
            self.place(line="дорога у леса", house="1", point=dest)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.extra["code"], "prepay_required")
        self.assertEqual(ctx.exception.extra["prepay"]["fee"], q["fee"])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/orders")["orders"], [])
        o = self.place(line="дорога у леса", house="1", point=dest, prepay_agreed=True)
        self.assertEqual((o["individual"], o["individual_any"], o["delivery_fee"], o["delivery_km"]),
                         (False, True, q["fee"], q["km"]))
        self.assertEqual((o["prepay_amount"], o["prepay_taken"], o["can_cancel"]), (q["fee"], False, False))
        with self.assertRaises(ApiError):  # покупатель отменить заказ с предоплатой не может
            self.call(CUSTOMER, "POST", f"/api/orders/{o['id']}/cancel")
        self.assertEqual(len(calls), 1)  # маршрут один раз, второй раз — из кеша
        self.assertIn(o["id"], [x["id"] for x in self.call(ADMIN, "GET", "/api/admin/orders")["orders"] if not x["individual"]])
        mine = {x["id"]: x for x in self.call(CUSTOMER, "GET", "/api/orders")["orders"]}[o["id"]]
        # 30.10.2026: доставка оплачена предоплатой курьеру — в «Итого» заказа не входит
        self.assertEqual(mine["total"], sum(i["unit_price"] * i["quantity"] for i in mine["items"]))

        # платная дорога: доплата к доставке, входит в доставку и в сумму заказа
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", f"/api/admin/orders/{o['id']}/delivery-toll", {"toll": 400})
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/delivery-toll", {"toll": 400})["order"]
        self.assertEqual((r["delivery_toll"], r["delivery_fee"], r["total"]), (400, q["fee"] + 400, o["total"] + 400))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/delivery-toll", {"toll": 250})["order"]
        self.assertEqual((r["delivery_fee"], r["total"]), (q["fee"] + 250, o["total"] + 250))  # не 400 + 250
        with self.assertRaises(ApiError):  # сумму предоплаты после согласия покупателя менять нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/delivery-fee", {"fee": 3000})
        seen = self.api.order_json(self.db.get_order(o["id"]), "courier")
        self.assertEqual((seen["delivery_fee"], seen["delivery_toll"]), (q["fee"] + 250, 250))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/delivery-toll", {"toll": 0})["order"]
        self.assertEqual((r["delivery_fee"], r["delivery_toll"], r["total"]), (q["fee"], 0, o["total"]))
        # 28.10.2026 (владелец): база — в кассу; км × цена за км (на момент расчёта, 40 ₽) — заработок курьера;
        # платная дорога — возвращается курьеру; баллоны — по обычной ставке; курьер сдаёт после вычета.
        self.call(ADMIN, "PUT", "/api/admin/outside-delivery", {"base": 1000, "per_km": 80})  # новая цена не влияет
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 1500, "return": 1500, "descent": 0})
        self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/delivery-toll", {"toll": 400})
        full = self.db.get_order(o["id"])
        balloons = sum(i["quantity"] for i in self.api.order_json(full, "admin")["items"] if "Баллон" in i["name"])
        admin_take(self.call, o["id"])
        for act in ("arrived", "delivered", "no-contact"):  # пока не нажато «Взял предоплату» — кнопки закрыты
            with self.assertRaises(ApiError) as ctx:
                self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/{act}", {"rating": 5})
            self.assertEqual(ctx.exception.status, 409)
        taken = self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/prepay-taken")["order"]
        self.assertTrue(taken["prepay_taken"])
        with self.assertRaises(ApiError):  # второй раз — нельзя (деньги в учёт идут один раз)
            self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/prepay-taken")
        arrive(self.call, o["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        km_earn = 40 * q["km"]  # цена за км на момент расчёта
        m = self.call(COURIER, "GET", "/api/courier/shift")["money"]
        self.assertEqual((m["earn"]["km"]["cash"], m["counts"]["km"], m["earn"]["balloons"]["cash"], m["toll"]["cash"]),
                         (km_earn, q["km"], 1500 * balloons, 400))
        self.assertEqual(m["earned"], 1500 * balloons + km_earn)
        # наличные: предоплата за доставку (взял до выезда) + оплата заказа при вручении
        self.assertEqual((m["cash"], m["keep"]), (full["total"] + q["fee"], m["earned"] + 400))
        self.assertEqual((m["kassa"], m["shop_owes"]), (full["total"] + q["fee"] - m["earned"] - 400, 0))
        c = self.call(COURIER, "GET", "/api/courier/shift")["counters"]
        self.assertEqual(c["cash"] + c["returns"] - c["keep"], m["kassa"])
        # заказ по СБП: заработок с пометкой — выплатит магазин, в кассу ничего не вычитается
        petr = next(x for x in self.db.get_couriers() if x["telegram_id"] == COURIER.id)
        self.db.courier_event(int(petr["id"]), "delivered", None, earned=1500, cash=0, keep=0,
                              data={"payment": "sbp", "balloons": 1, "rate": 1500, "pay": {"balloons": 1500}})
        m2 = self.call(COURIER, "GET", "/api/courier/shift")["money"]
        self.assertEqual((m2["earn"]["balloons"]["noncash"], m2["shop_owes"], m2["kassa"]), (1500, 1500, m["kassa"]))
        moscow = self.place(line="Москва, Лесная улица", house="7")
        with self.assertRaises(ApiError):  # московскому заказу — нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{moscow['id']}/delivery-toll", {"toll": 100})

        # сервис маршрутов не ответил — как раньше, индивидуальный
        async def broken(params):
            raise RuntimeError("нет сети")
        self.api.routing = RouteService(None, fetch=broken)
        o2 = self.place(line="дорога у леса", house="1", point={"lat": 54.3, "lon": 37.7})
        self.assertEqual((o2["individual"], o2["delivery_fee"]), (True, 0))
        self.api.routing = None  # ключа нет
        o3 = self.place(line="дорога у леса", house="1", point={"lat": 54.4, "lon": 37.7})
        self.assertTrue(o3["individual"])

    def test_prepay_admin_cancel_reverses_courier_cash(self):
        """Админ отменяет заказ с предоплатой: красное предупреждение, затем предоплата списывается у курьера."""
        from webapp.routing import KREMLIN, RouteService
        dest = {"lat": 54.2, "lon": 37.6}

        async def fake(params):
            n = 200
            return {"features": [{"geometry": {"coordinates": [
                [KREMLIN[1] + (dest["lon"] - KREMLIN[1]) * i / n, KREMLIN[0] + (dest["lat"] - KREMLIN[0]) * i / n]
                for i in range(n + 1)]}}]}
        self.api.routing = RouteService(None, fetch=fake)
        courier_on_shift(self.db)
        o = self.place(line="дорога у леса", house="1", point=dest, prepay_agreed=True)
        admin_take(self.call, o["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/prepay-taken")
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["money"]["cash"], o["prepay_amount"])
        with self.assertRaises(ApiError) as ctx:  # без подтверждения — предупреждение с данными клиента
            self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/status", {"status": "cancelled"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.extra["code"], "prepay_cancel")
        self.assertEqual(ctx.exception.extra["prepay"], o["prepay_amount"])
        self.assertEqual(ctx.exception.extra["customer"]["user_id"], CUSTOMER.id)
        self.assertEqual(self.db.get_order(o["id"])["status"], "accepted")
        res = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/status", {"status": "cancelled", "confirm_prepay": True})
        self.assertEqual(res["order"]["status"], "cancelled")
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["money"]["cash"], 0)  # предоплата списана у курьера

    def test_delivery_fee_release_courier_and_export(self):
        tula = self.call(CUSTOMER, "GET", "/api/address/suggest", q="советская")["suggestions"][0]
        t = self.place(line=tula["value"], street_id=tula["id"], house="1")
        base = t["total"]
        m = self.place(line=self.lesnaya()["value"], street_id=self.lesnaya()["id"], house="7")
        with self.assertRaises(ApiError):  # обычному (московскому) заказу доставку не назначить
            self.call(ADMIN, "POST", f"/api/admin/orders/{m['id']}/delivery-fee", {"fee": 500})
        with self.assertRaises(ApiError):
            self.call(ADMIN, "POST", f"/api/admin/orders/{t['id']}/delivery-fee", {"fee": -1})
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", f"/api/admin/orders/{t['id']}/delivery-fee", {"fee": 500})
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{t['id']}/delivery-fee", {"fee": 1000})
        self.assertEqual((r["order"]["delivery_fee"], r["order"]["total"], r["order"]["individual"]), (1000, base + 1000, True))
        self.assertTrue(self.api.order_json(self.db.get_order(t["id"]), "courier")["individual"])
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{t['id']}/delivery-fee", {"fee": 1500, "release": True})
        self.assertEqual((r["order"]["delivery_fee"], r["order"]["total"]), (1500, base + 1500))  # не 1000 + 1500
        self.assertEqual((r["order"]["individual"], r["order"]["individual_any"]), (False, True))
        seen = self.api.order_json(self.db.get_order(t["id"]), "courier")
        self.assertEqual((seen["delivery_fee"], seen["total"], seen["individual_any"]), (1500, base + 1500, True))
        mine = {o["id"]: o for o in self.call(CUSTOMER, "GET", "/api/orders")["orders"]}[t["id"]]
        self.assertEqual(mine["delivery_fee"], 1500)
        admin_take(self.call, t['id'])
        arrive(self.call, t["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{t['id']}/delivered", {"rating": 5})
        with self.assertRaises(ApiError):  # закрытому заказу — нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{t['id']}/delivery-fee", {"fee": 100})
        _, ws = ExportTests.export(self)
        d = [x for x in ExportTests.table(self, ws) if x.get("Тип") == "Доставка"]
        self.assertEqual(len(d), 1)
        self.assertIn(1500, d[0].values())
        self.assertFalse(d[0].get("Вариант"))
        all_ws = self.wb["Все заказы"]
        head = [c.value for c in all_ws[6]]
        row = next(r for r in all_ws.iter_rows(min_row=7, values_only=True)
                   if r[head.index("Номер заказа")] and str(r[head.index("Номер заказа")]).endswith(f"_{t['id']}"))
        self.assertEqual((row[head.index("Доставка, ₽")], row[head.index("Итого, ₽")]), (1500, base + 1500))

    def test_without_address_db_falls_back_to_word_moscow(self):
        self.api.geo = None
        self.assertFalse(self.place(line="Москва, Профсоюзная", house="15")["individual"])
        self.assertTrue(self.place(line="Химки, Ленинградское ш", house="1")["individual"])
        self.assertTrue(self.place(line="Тула, ул Москвина", house="3")["individual"])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/address/suggest", q="лесн"), {"enabled": False, "suggestions": []})

    def test_text_is_moscow(self):
        from webapp.geodb import text_is_moscow
        self.assertTrue(text_is_moscow("г Москва, ул Лесная"))
        self.assertFalse(text_is_moscow("Московская обл, г Химки"))


class CourierRegionTests(AddressCheckoutTests):
    HEADER_ROW = ExportTests.HEADER_ROW

    def fresh(self):
        with self.db.connection() as conn:
            conn.execute("UPDATE couriers SET shift_started_at = NULL, shift_regions = NULL, shift_stock = NULL")

    def place(self, street_q, city, house):
        st = next(x for x in self.call(CUSTOMER, "GET", "/api/address/suggest", q=street_q)["suggestions"] if x["city"] == city)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        return self.call(CUSTOMER, "POST", "/api/orders", self.body(line=st["value"], street_id=st["id"], house=house))["order"]

    def available(self):
        return [o["id"] for o in self.call(COURIER, "GET", "/api/courier/orders")["orders"]]

    def go_shift(self, regions, stock):
        """«В путь!» — заявка с товарами (ключ — номер товара) + подтверждение админа (26.10.2026)."""
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": regions, "stock": stock})
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True})
        return self.call(COURIER, "GET", "/api/courier/shift")

    def test_live_broadcast_admin_map_and_colors(self):
        """Трансляция в боте привязана к курьеру: точка — на карту админа и в его заказы (и назначенные позже)."""
        from datetime import datetime, timedelta, timezone
        self.fresh()
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        sh = self.go_shift([], {"1": 4})
        self.assertEqual(sh["live"]["active"], False)  # приложение покажет «Включите трансляцию»
        # кнопка «📡 Включить трансляцию»: инструкция в бот + ссылка на чат
        r = self.call(COURIER, "POST", "/api/courier/live")
        self.assertTrue(r["link"].startswith("https://t.me/"))
        self.assertIn("courier_live_instruction", [e["event"] for e in self.bridge.events])
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", "/api/courier/live")
        # заказ назначен до трансляции
        first = self.place("лесн", "Москва", "7")
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [first["id"]], "courier_id": petr["id"]})
        forever = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(timespec="seconds")
        self.assertIsNotNone(self.db.start_courier_live(COURIER.id, COURIER.id, 555, 55.75, 37.61, forever))
        self.assertIsNone(self.db.start_courier_live(999999, 1, 1, 1.0, 1.0, forever))  # не курьер
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["live"]["active"], sh["live"]["lat"]), (True, 55.75))
        self.assertEqual(self.db.get_order(first["id"])["courier_last_lat"], 55.75)
        # заказ назначен после включения — сразу получает текущую точку
        second = self.place("лесн", "Москва", "9")
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [second["id"]], "courier_id": petr["id"]})
        self.assertEqual(self.db.get_order(second["id"])["courier_last_lon"], 37.61)
        # обновление трансляции — курьеру и обоим заказам; покупатель видит точку
        self.db.update_courier_live(COURIER.id, 555, 55.70, 37.50)
        for oid in (first["id"], second["id"]):
            self.assertEqual(self.db.get_order(oid)["courier_last_lat"], 55.70)
        mine = {o["id"]: o for o in self.call(CUSTOMER, "GET", "/api/orders")["orders"]}
        self.assertEqual(mine[first["id"]]["courier_location"], {"lat": 55.70, "lon": 37.50})
        self.assertIsNone(self.db.update_courier_live(COURIER.id, 556, 1.0, 1.0))  # чужое сообщение
        # карта админа
        mp = self.call(ADMIN, "GET", "/api/admin/map")
        me = next(c for c in mp["couriers"] if c["id"] == petr["id"])
        self.assertEqual((me["status"], me["live"]["active"], me["live"]["lat"], sorted(o["id"] for o in me["orders"])),
                         ("online", True, 55.70, sorted([first["id"], second["id"]])))
        self.assertIn(me["color"], range(30))
        with self.assertRaises(ApiError):
            self.call(COURIER, "GET", "/api/admin/map")
        # курьер остановил трансляцию (обновление без live_period)
        self.db.update_courier_live(COURIER.id, 555, 55.71, 37.51, stopped=True)
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["live"]["active"], sh["live"]["lat"]), (False, 55.71))
        # срок трансляции вышел
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.db.start_courier_live(COURIER.id, COURIER.id, 600, 55.72, 37.52, past)
        self.assertFalse(self.call(COURIER, "GET", "/api/courier/shift")["live"]["active"])
        # 30 разных меток (15 цветов × кружок/квадрат) — до 30 курьеров без повторов
        ids = [self.db.add_courier(f"Курьер {i}", None, 8000 + i) for i in range(31)]
        with self.db.connection() as conn:
            colors = [r[0] for r in conn.execute("SELECT marker_idx FROM couriers ORDER BY id")]
        self.assertEqual(len(set(colors[:30])), 30)
        self.assertTrue(all(0 <= c < 30 for c in colors))
        self.assertEqual(len(ids), 31)

    def test_chats_relaxgas_and_order(self):
        """Чат с RelaxGas (ответ от имени магазина) и чат курьера с покупателем внутри заказа."""
        ev = lambda name: [e for e in self.bridge.events if e["event"] == name]
        # покупатель → RelaxGas: текст и фото
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", "/api/chat", {"text": "   "})
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        r = self.call(CUSTOMER, "POST", "/api/chat", {"text": "Когда привезёте?", "photo": png})
        self.assertEqual((len(r["messages"]), r["messages"][0]["mine"], r["messages"][0]["photo"]), (1, True, True))
        self.assertEqual(len(ev("support_message")), 1)
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["chats"], 1)
        th = self.call(ADMIN, "GET", "/api/admin/chats")["threads"]
        self.assertEqual((th[0]["user_id"], th[0]["unread"], th[0]["last"]), (CUSTOMER.id, 1, "Когда привезёте?"))
        with self.assertRaises(ApiError):
            self.call(COURIER, "GET", "/api/admin/chats")
        # админ отвечает от имени RelaxGas
        r = self.call(ADMIN, "POST", f"/api/admin/chats/{CUSTOMER.id}", {"text": "Через час"})
        self.assertEqual(r["messages"][-1]["admin_name"] is not None, True)  # админ видит, кто ответил
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["chats"], 0)
        self.call(ADMIN, "POST", f"/api/admin/chats/{CUSTOMER.id}", {"text": "Уточним время"})
        self.assertEqual(len(ev("support_reply")), 1)  # одно уведомление, пока не прочитал
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["chat_unread"], 2)
        mine = self.call(CUSTOMER, "GET", "/api/chat")["messages"]
        self.assertNotIn("admin_name", mine[-1])  # покупатель не видит, кто из админов
        self.assertEqual((mine[-1]["mine"], mine[-1]["from_admin"]), (False, True))
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["chat_unread"], 0)
        pid = mine[0]["id"]
        self.assertTrue(self.call(CUSTOMER, "GET", f"/api/chat/photo/{pid}")["src"].startswith("data:image/png"))
        with self.assertRaises(ApiError):  # чужое фото
            self.call(CUSTOMER2, "GET", f"/api/chat/photo/{pid}")
        # ✕ — админ удаляет чат: переписка и фото исчезают и у покупателя
        self.call(CUSTOMER2, "POST", "/api/chat", {"text": "Вопрос Бориса"})
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "DELETE", f"/api/admin/chats/{CUSTOMER.id}")
        left = self.call(ADMIN, "DELETE", f"/api/admin/chats/{CUSTOMER.id}")["threads"]
        self.assertEqual([t["user_id"] for t in left], [CUSTOMER2.id])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/chat")["messages"], [])
        with self.assertRaises(ApiError):
            self.call(ADMIN, "GET", f"/api/chat/photo/{pid}")
        with self.assertRaises(ApiError):  # второй раз — уже нечего удалять
            self.call(ADMIN, "DELETE", f"/api/admin/chats/{CUSTOMER.id}")

        # чат курьера и покупателя в заказе
        self.fresh()
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        order = self.place("лесн", "Москва", "7")
        with self.assertRaises(ApiError):  # курьера ещё нет — чат закрыт
            self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/chat", {"text": "Жду"})
        self.go_shift([], {"1": 5})
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [order["id"]], "courier_id": petr["id"]})
        r = self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/chat", {"text": "Код домофона 12К"})
        self.assertEqual((r["role"], r["messages"][0]["mine"], r["can_send"]), ("customer", True, True))
        self.assertEqual(self.call(COURIER, "GET", "/api/badges")["courier"]["chat"], {str(order["id"]): 1})
        self.assertEqual(ev("order_chat")[-1]["to_courier"], True)
        r = self.call(COURIER, "GET", f"/api/orders/{order['id']}/chat", **{"as": "courier"})
        self.assertEqual((r["role"], r["messages"][0]["mine"], r["messages"][0]["from_courier"]), ("courier", False, False))
        self.assertEqual(self.call(COURIER, "GET", "/api/badges")["courier"]["chat"], {})
        self.call(COURIER, "POST", f"/api/orders/{order['id']}/chat", {"text": "Буду через 10 минут", "as": "courier"})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["order_chat"], {str(order["id"]): 1})
        self.assertEqual(ev("order_chat")[-1]["to_courier"], False)
        with self.assertRaises(ApiError):  # чужой заказ
            self.call(CUSTOMER2, "GET", f"/api/orders/{order['id']}/chat")
        adm = self.call(ADMIN, "GET", f"/api/orders/{order['id']}/chat", **{"as": "admin"})
        self.assertEqual((adm["role"], len(adm["messages"]), adm["can_send"]), ("admin", 2, False))
        with self.assertRaises(ApiError):  # админ только читает
            self.call(ADMIN, "POST", f"/api/orders/{order['id']}/chat", {"text": "x", "as": "admin"})
        self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "cancelled"})
        # заказ закрыт — чат удалён из приложения у всех (и у админа), но остаётся в Excel
        for who, extra in ((CUSTOMER, {}), (COURIER, {"as": "courier"}), (ADMIN, {"as": "admin"})):
            with self.assertRaises(ApiError):
                self.call(who, "GET", f"/api/orders/{order['id']}/chat", **extra)
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", f"/api/orders/{order['id']}/chat", {"text": "ещё"})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/badges")["order_chat"], {})
        # 28.10.2026: в «Истории заказов» админ читает переписку закрытого заказа (только админ)
        h = next(o for o in self.call(ADMIN, "GET", "/api/admin/history", status="all")["orders"] if o["id"] == order["id"])
        self.assertEqual(h["chat_count"], 2)
        arch = self.call(ADMIN, "GET", f"/api/admin/orders/{order['id']}/chat")
        self.assertEqual([(m["who"], m["from_courier"], m["text"]) for m in arch["messages"]],
                         [("Анна Покупатель", False, "Код домофона 12К"), ("Пётр", True, "Буду через 10 минут")])
        for who in (CUSTOMER, COURIER):
            with self.assertRaises(ApiError):
                self.call(who, "GET", f"/api/admin/orders/{order['id']}/chat")
        ExportTests.export(self)
        ws = self.wb["Чаты курьер–покупатель"]
        got = [r for r in ws.iter_rows(min_row=4, values_only=True) if r[1] == order["id"]]
        self.assertEqual([(r[5], r[8], r[2]) for r in got],
                         [("Покупатель", "Код домофона 12К", "Отменён"), ("Курьер", "Буду через 10 минут", "Отменён")])
        self.assertEqual((got[0][6], got[0][0]), ("Пётр", f"{CUSTOMER.id}_{order['id']}"))
        # сброс истории: нумерация с №1 — старая переписка не попадает в новый заказ, но остаётся в Excel
        self.db.wipe_all_order_history()
        again = self.place("лесн", "Москва", "7")
        self.assertEqual(again["id"], order["id"])  # тот же номер
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [again["id"]], "courier_id": petr["id"]})
        self.assertEqual(self.call(CUSTOMER, "GET", f"/api/orders/{again['id']}/chat")["messages"], [])
        self.assertEqual({m["archived"] for m in self.db.get_full_export()["order_chats"]}, {1})

    def test_shift_hourly_question_break_and_statuses(self):
        """Смена без 12 часов: «Вы ещё на смене?» раз в час, перерыв, «закончил», автоофлайн."""
        self.fresh()
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        self.assertEqual(petr["shift"]["status"], "offline")  # «не выходил на смену»
        self.assertIsNone(petr["shift"]["ended_at"])
        sh = self.go_shift([], {"1": 4})
        self.assertEqual((sh["active"], sh["status"], sh["ask"], sh["busy"]), (True, "online", False, 0))
        self.assertFalse(self.call(COURIER, "GET", "/api/badges")["courier"]["shift_ask"])

        def ago(minutes):
            from datetime import datetime, timedelta, timezone
            t = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            with self.db.connection() as conn:
                conn.execute("UPDATE couriers SET shift_confirmed_at = ?", (t,))
            return t

        # прошёл час — вопрос (окно нельзя закрыть), 30 минут на ответ
        ago(65)
        self.assertTrue(self.call(COURIER, "GET", "/api/badges")["courier"]["shift_ask"])
        sh = self.call(COURIER, "POST", "/api/courier/shift/confirm")  # «Да, работаем!»
        self.assertEqual((sh["active"], sh["ask"]), (True, False))
        # перерыв: офлайн, баллоны и регионы сохраняются; «Вернуться на смену»
        sh = self.call(COURIER, "POST", "/api/courier/shift/break")
        self.assertEqual((sh["active"], sh["status"], sh["stock"]["start"]["1"]), (False, "break", 4))
        self.assertEqual(next(c for c in self.call(ADMIN, "GET", "/api/admin/orders")["couriers"]
                              if c["id"] == petr["id"])["status"], "break")
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/shift/break")
        sh = self.call(COURIER, "POST", "/api/courier/shift/resume")
        self.assertEqual((sh["active"], sh["status"], sh["stock"]["start"]["1"]), (True, "online", 4))
        # с заказом в работе: ни перерыва, ни «закончил»; не ответил — всё равно на смене
        order = self.place("лесн", "Москва", "7")
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [order["id"]], "courier_id": petr["id"]})
        for path in ("/api/courier/shift/break", "/api/courier/shift/end"):
            with self.assertRaises(ApiError):
                self.call(COURIER, "POST", path)
        ago(300)
        # 28.10.2026: не ответил за 10 минут — «не отвечает на опрос» (смена не закрывается сама)
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["active"], sh["status"], sh["ask"], sh["busy"]), (False, "silent", True, 1))
        self.assertEqual(self.call(COURIER, "GET", "/api/badges")["courier"]["shift_busy"], 1)
        self.call(ADMIN, "POST", f"/api/admin/orders/{order['id']}/status", {"status": "cancelled"})
        other = self.place("лесн", "Москва", "7")
        with self.assertRaises(ApiError) as ctx:  # молчащему курьеру заказ не назначить
            self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [other["id"]], "courier_id": petr["id"]})
        self.assertIn("перестал отвечать", ctx.exception.message)
        alerts = {a["kind"]: a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]}
        self.assertEqual(alerts["silent"]["id"], petr["id"])
        # ответил позже — снова на смене, уведомление ушло
        sh = self.call(COURIER, "POST", "/api/courier/shift/confirm")
        self.assertEqual((sh["active"], sh["status"]), (True, "online"))
        self.assertNotIn("silent", {a["kind"] for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]})
        # снова молчит — админ закрывает смену сам
        ago(75)
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["status"], "silent")
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift-end")
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["active"], sh["status"], sh["end_reason"]), (False, "offline", "admin"))
        self.assertFalse(self.call(COURIER, "GET", "/api/badges")["courier"]["shift_ask"])
        # «📣 Опросить всех»: вопрос сразу, отсчёт заново
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": []})  # остаток прошлой смены — без ввода
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True})
        self.assertFalse(self.call(COURIER, "GET", "/api/courier/shift")["ask"])
        self.assertEqual(self.call(ADMIN, "POST", "/api/admin/couriers/ask-all")["asked"], 1)
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["status"], "online")
        self.assertTrue(self.call(COURIER, "GET", "/api/courier/shift")["ask"])
        self.call(COURIER, "POST", "/api/courier/shift/confirm")
        # «Нет, закончил работу» → ждёт администратора → подтверждено
        sh = self.call(COURIER, "POST", "/api/courier/shift/end")
        self.assertEqual((sh["status"], sh["active"]), ("ending", False))
        with self.assertRaises(ApiError):  # пока не подтвердили — «В путь!» нельзя
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": []})
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift-end")
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["status"], sh["end_reason"]), ("offline", "self"))
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["id"] == petr["id"])
        self.assertEqual((petr["shift"]["status"], petr["shift"]["end_reason"]), ("offline", "self"))
        with self.assertRaises(ApiError):  # не на перерыве
            self.call(COURIER, "POST", "/api/courier/shift/resume")

    def test_regions_only_for_admin_and_export(self):
        """28.10.2026: работа по регионам убрана — регион заказа остаётся (фильтр админа, Excel),
        курьеры регион не выбирают и заказы сами не берут; раздаёт только администратор."""
        self.fresh()
        center = self.place("лесн", "Москва", "7")        # внутри ТТК (образец)
        south = self.place("профсоюз", "Москва", "15")    # за ТТК, на юг от Кремля
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        unknown = self.call(CUSTOMER, "POST", "/api/orders", {**self.body(line="Москва, неизвестная улица", house="5")})["order"]
        self.assertEqual((center["region"], center["region_label"], south["region"], unknown["region"]),
                         ("center", "Центр", "south", None))
        adm = self.call(ADMIN, "GET", "/api/admin/orders", status="active")
        self.assertEqual(({o["id"]: o["region"] for o in adm["orders"]}[south["id"]], len(adm["regions"])), ("south", 5))
        self.assertNotIn("regions_enabled", adm)
        st = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertFalse(any(k in st for k in ("regions_enabled", "all_regions", "regions", "change")))
        for method, path, body in (("PUT", "/api/admin/regions", {"enabled": True}),
                                   ("POST", f"/api/admin/orders/{center['id']}/reoffer", {}),
                                   ("POST", "/api/admin/couriers/1/region", {"allow": True})):
            with self.assertRaises(ApiError):  # переключателя, «передать курьерам» и смены региона больше нет
                self.call(ADMIN, method, path, body)
        sh = self.go_shift(["center", "north"], {"1": 5, "2": 5})  # регионы от старого приложения — игнорируются
        self.assertEqual((sh["active"], self.db.courier_shift(self.courier_of_petr())["regions"]), (True, []))
        self.assertEqual(self.available(), [])
        petr = self.courier_of_petr()
        r = self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [center["id"], unknown["id"], south["id"]],
                                                                   "courier_id": petr["id"]})
        self.assertEqual(sorted(r["assigned"]), sorted([center["id"], unknown["id"], south["id"]]))  # любой регион — решает админ
        # регион в выгрузке
        _, _ws = ExportTests.export(self)
        ws = self.wb["Все заказы"]
        head = [c.value for c in ws[6]]
        regions = {r[head.index("Заказ №")]: r[head.index("Регион")] for r in ws.iter_rows(min_row=7, values_only=True) if r[0]}
        self.assertEqual((regions[center["id"]], regions[south["id"]], regions[unknown["id"]]), ("Центр", "Юг", None))

    def courier_of_petr(self):
        return self.db.get_active_courier_by_telegram_id(COURIER.id)

    def test_balloon_stock_per_shift(self):
        self.fresh()
        pid = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 1000})["product"]["id"]

        def order(qty):
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": pid, "variant": "new", "quantity": qty})
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 3, "variant": "new", "quantity": 1})  # шарики — не баллон
            return self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]

        big, small = order(2), order(1)
        k = str(pid)
        with self.assertRaises(ApiError):  # такого товара в каталоге нет
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"999": 1}})
        catalog = [p["id"] for p in self.call(COURIER, "GET", "/api/courier/shift")["stock_items"]]
        self.assertTrue(pid in catalog and 3 in catalog)  # все позиции каталога, не только баллоны
        sh = self.go_shift(["center"], {"1": 3, k: 1, "3": 5})  # остальное убрано крестиком (= 0)
        self.assertEqual((sh["stock"]["start"][k], sh["stock"]["now"][k], sh["stock"]["start"]["1"], sh["stock"]["now"]["3"]), (1, 1, 3, 5))
        self.assertEqual(sh["stock"]["names"][k], "Баллон 5 л")
        with self.assertRaises(ApiError) as ctx:
            admin_take(self.call, big['id'])
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("Баллон 5 л — нужно 2, у вас 1", ctx.exception.message)
        admin_take(self.call, small['id'])
        now = self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"]
        self.assertEqual((now[k], now["3"], now["1"]), (0, 4, 3))  # списаны и баллон, и шарики
        cour = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        self.assertEqual((cour["shift"]["stock"]["start"][k], cour["shift"]["stock"]["now"][k]), (1, 0))
        # «Взял по ошибке» / снят админом — товар возвращается в остаток
        self.call(ADMIN, "POST", f"/api/admin/orders/{small['id']}/status", {"status": "new"})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"][k], 1)
        # доставленный заказ остаётся списанным
        admin_take(self.call, small['id'])
        arrive(self.call, small["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{small['id']}/delivered", {"rating": 5})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"][k], 0)
        # нехватка не-баллона: шариков 3 → 4-й заказ с шариками не взять
        with self.db.connection() as conn:
            conn.execute("UPDATE couriers SET shift_stock = ?", (self.db.dump_stock({pid: 9, 3: 0}),))
        with self.assertRaises(ApiError) as ctx:
            admin_take(self.call, big['id'])
        self.assertIn("— нужно 1, у вас 0", ctx.exception.message)

    def test_assign_only_within_courier_stock(self):
        """28.10.2026: заказы отдаёт только админ и только курьеру на смене, у которого хватает товаров;
        несколько заказов — по порядку, сколько хватает (пример владельца: 2 л × 5, 5 л × 10)."""
        self.fresh()
        two = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 2 л", "description": "", "price": 500})["product"]["id"]
        five = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 900})["product"]["id"]

        def order(items):
            for pid, q in items:
                self.call(CUSTOMER, "POST", "/api/cart", {"product_id": pid, "variant": "new", "quantity": q})
            return self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]

        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        big = order([(two, 5), (five, 10)])        # спишет весь остаток
        small = [order([(five, 4)]) for _ in range(3)]
        with self.assertRaises(ApiError):  # не на смене — нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{big['id']}/assign", {"courier_id": petr["id"]})
        with self.db.connection() as conn:  # «старая смена» без учёта товаров — тоже нельзя
            conn.execute("UPDATE couriers SET shift_state = 'on', shift_started_at = ?, shift_confirmed_at = ?, shift_stock = NULL "
                         "WHERE id = ?", (self.db.now(), self.db.now(), petr["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.call(ADMIN, "POST", f"/api/admin/orders/{big['id']}/assign", {"courier_id": petr["id"]})
        self.assertIn("нет учёта товаров", ctx.exception.message)
        self.call(COURIER, "POST", "/api/courier/shift/end")
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift-end")
        with self.db.connection() as conn:
            conn.execute("UPDATE couriers SET stock_left = NULL WHERE id = ?", (petr["id"],))
        self.go_shift([], {str(two): 5, str(five): 10})
        # много маленьких: 4 + 4 — хватает, третий (ещё 4) — нет: назначаются первые два
        r = self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [o["id"] for o in small], "courier_id": petr["id"]})
        self.assertEqual((r["assigned"], [x["id"] for x in r["short"]]), ([small[0]["id"], small[1]["id"]], [small[2]["id"]]))
        self.assertIn("Баллон 5 л — нужно 4, у вас 2", r["short"][0]["reason"])
        with self.assertRaises(ApiError) as ctx:  # большой — уже не хватает
            self.call(ADMIN, "POST", f"/api/admin/orders/{big['id']}/assign", {"courier_id": petr["id"]})
        self.assertIn("Баллон 5 л — нужно 10, у вас 2", ctx.exception.message)
        # сняли маленькие — большой заказ спишет весь остаток
        for o in small[:2]:
            self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/status", {"status": "new"})
        self.call(ADMIN, "POST", f"/api/admin/orders/{big['id']}/assign", {"courier_id": petr["id"]})
        now = self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"]
        self.assertEqual((now[str(two)], now[str(five)]), (0, 0))
        with self.assertRaises(ApiError):  # курьер сам — никогда
            self.call(COURIER, "POST", f"/api/courier/orders/{small[2]['id']}/take")

    def test_return_descent_paid_from_payout(self):
        """28.10.2026: «⬇️ Спуск пустого баллона» в возврате — ставка курьеру (по умолчанию 0), не больше
        числа баллонов, вычитается из выплаты покупателю и идёт курьеру в заработок."""
        self.fresh()
        five = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 3000, "return_price": 700})["product"]
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/checkout")["descent_price"], 0)
        r = self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 0, "balloon_next": 0, "return": 100, "descent": 200})["courier_rates"]
        self.assertEqual(r, {"balloon": 0, "balloon_next": 0, "return": 100, "descent": 200})
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/checkout")["descent_price"], 200)
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return",
                "return_items": [{"product_id": five["id"], "quantity": 2}]}
        body.pop("payment_method", None)
        with self.assertRaises(ApiError):  # спусков больше, чем баллонов
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "descent_count": 3})
        ret = self.call(CUSTOMER, "POST", "/api/orders", {**body, "descent_count": 2})["order"]
        self.assertEqual((ret["return_payout"], ret["descent"]), (2 * 700 - 2 * 200, {"count": 2, "price": 200, "total": 400}))
        self.go_shift([], {str(five["id"]): 1})
        admin_take(self.call, ret["id"])
        arrive(self.call, ret["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{ret['id']}/delivered", {"rating": 5})
        c = self.call(COURIER, "GET", "/api/courier/shift")["counters"]
        self.assertEqual((c["earned"], c["returns"]), (100 + 400, -1000))  # поездка на возврат 100 + спуск 400

    def test_return_paid_with_points(self):
        """30.10.2026: возврат баллами — наличные не отдаются, клиенту начисляются баллы (спуск вычитается в баллах)."""
        self.fresh()
        five = self.call(ADMIN, "POST", "/api/admin/products",
                         {"name": "Баллон 5 л", "description": "", "price": 3000, "return_price": 2000, "return_points": 3000})["product"]
        two = self.call(ADMIN, "POST", "/api/admin/products",
                        {"name": "Баллон 2 л", "description": "", "price": 1000, "return_price": 1000})["product"]  # без баллов
        self.assertEqual(five["return_points"], 3000)
        self.assertEqual({x["id"]: x["return_points"] for x in self.call(CUSTOMER, "GET", "/api/returns")["items"]},
                         {five["id"]: 3000, two["id"]: 0})
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 0, "return": 100, "descent": 200})
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return"}
        body.pop("payment_method", None)
        with self.assertRaises(ApiError):  # 2 л баллами не принимается
            self.call(CUSTOMER, "POST", "/api/orders", {**body, "return_mode": "points",
                                                         "return_items": [{"product_id": two["id"], "quantity": 1}]})
        before = self.db.bonus_balance(CUSTOMER.id)
        ret = self.call(CUSTOMER, "POST", "/api/orders", {**body, "return_mode": "points", "descent_count": 1,
                                                          "return_items": [{"product_id": five["id"], "quantity": 2}]})["order"]
        self.assertEqual((ret["return_mode"], ret["return_payout"], ret["return_points"]), ("points", 0, 2 * 3000 - 200))
        self.go_shift([], {str(five["id"]): 1})
        admin_take(self.call, ret["id"])
        arrive(self.call, ret["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{ret['id']}/delivered", {"rating": 5})
        self.assertEqual(self.db.bonus_balance(CUSTOMER.id) - before, 5800)
        c = self.call(COURIER, "GET", "/api/courier/shift")["counters"]
        self.assertEqual((c["earned"], c["returns"]), (100 + 200, 0))  # наличные клиенту не отдавались
        # спуск (200) курьер берёт из наличных на руках и записывает в заработок: keep = поездка + спуск
        self.assertEqual(c["keep"], 100 + 200)
        # наличными — как раньше
        cash = self.call(CUSTOMER, "POST", "/api/orders", {**body, "return_items": [{"product_id": five["id"], "quantity": 1}]})["order"]
        self.assertEqual((cash["return_mode"], cash["return_payout"], cash["return_points"]), ("cash", 2000, 0))

    def test_admin_edit_order(self):
        """31.10.2026: админ меняет состав активного заказа (товары, подъёмы); в возврате — состав и спуски."""
        self.fresh()
        five = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 3000, "return_price": 700})["product"]
        beads = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Шарики 5 штук", "description": "", "price": 500})["product"]
        self.db.set_floor_lift_price(300)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": five["id"], "variant": "new", "quantity": 1})
        o = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]
        self.assertEqual(o["total"], 3000)
        with self.assertRaises(ApiError):  # только админ
            self.call(COURIER, "POST", f"/api/admin/orders/{o['id']}/edit", {"items": []})
        opts = self.call(ADMIN, "GET", f"/api/admin/orders/{o['id']}/edit-options")
        self.assertEqual((opts["is_return"], opts["lift_price"], len(opts["items"])), (False, 300, 1))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {
            "items": [{"product_id": five["id"], "variant": "new", "quantity": 2}, {"product_id": beads["id"], "variant": "new", "quantity": 1}],
            "lift_floors": 2})
        self.assertEqual(r["order"]["total"], 2 * 3000 + 500 + 2 * 300)
        with self.assertRaises(ApiError):  # нельзя убрать всё
            self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {"items": [{"product_id": five["id"], "variant": "new", "quantity": 0}]})
        # в работе у курьера — тоже можно
        self.go_shift([], {str(five["id"]): 5, str(beads["id"]): 5})
        admin_take(self.call, o["id"])
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {
            "items": [{"product_id": five["id"], "variant": "new", "quantity": 1}], "lift_floors": 0})
        self.assertEqual(r["order"]["total"], 3000)
        # возврат: состав и спуски
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 0, "return": 100, "descent": 200})
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return",
                "return_items": [{"product_id": five["id"], "quantity": 1}]}
        body.pop("payment_method", None)
        ret = self.call(CUSTOMER, "POST", "/api/orders", body)["order"]
        self.assertEqual(ret["return_payout"], 700)
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{ret['id']}/edit", {
            "items": [{"product_id": five["id"], "quantity": 3}], "descent_count": 2})
        self.assertEqual((r["order"]["return_payout"], r["order"]["descent"]["count"]), (3 * 700 - 2 * 200, 2))
        with self.assertRaises(ApiError):  # спусков больше баллонов
            self.call(ADMIN, "POST", f"/api/admin/orders/{ret['id']}/edit", {"items": [{"product_id": five["id"], "quantity": 1}], "descent_count": 2})
        # закрытый заказ менять нельзя
        arrive(self.call, o["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        with self.assertRaises(ApiError):
            self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {"items": [{"product_id": five["id"], "variant": "new", "quantity": 2}]})

    def test_purchase_with_return_in_one_order(self):
        """01.11.2026: покупка и возврат пустого баллона в одном заказе; админ может добавить возврат."""
        self.fresh()
        ten = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 10 л", "description": "", "price": 4000, "return_price": 3000, "return_points": 4000})["product"]
        two = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 2 л", "description": "", "price": 1000, "return_price": 1000, "return_points": 1500})["product"]
        opts = self.call(CUSTOMER, "GET", "/api/checkout")["return_items"]
        self.assertEqual({x["id"] for x in opts}, {ten["id"], two["id"]})
        # наличные: зачёт в оплату
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": ten["id"], "variant": "new", "quantity": 1})
        b = {**self.body(line="Москва, неизвестная улица", house="5"), "return_items": [{"product_id": two["id"], "quantity": 1}]}
        o = self.call(CUSTOMER, "POST", "/api/orders", b)["order"]
        self.assertEqual((o["total"], o["return_payout"], o["return_mode"], len(o["returns"])), (3000, 1000, "cash", 1))
        # возврат больше покупки — отказ
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": two["id"], "variant": "new", "quantity": 1})
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "POST", "/api/orders", {**b, "return_items": [{"product_id": ten["id"], "quantity": 1}]})
        # баллами: цена не меняется, баллы придут при доставке
        o2 = self.call(CUSTOMER, "POST", "/api/orders", {**b, "return_mode": "points"})["order"]
        self.assertEqual((o2["total"], o2["return_points"], o2["return_mode"]), (1000, 1500, "points"))
        self.go_shift([], {str(ten["id"]): 3, str(two["id"]): 3})
        before = self.db.bonus_balance(CUSTOMER.id)
        admin_take(self.call, o2["id"])
        arrive(self.call, o2["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o2['id']}/delivered", {"rating": 5})
        self.assertEqual(self.db.bonus_balance(CUSTOMER.id) - before, 1500)
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["empties"].get("2"), 1)  # пустой 2 л принят
        # админ добавляет возврат в уже висящий заказ (без возврата)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": ten["id"], "variant": "new", "quantity": 1})
        o3 = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]
        self.assertEqual((o3["total"], o3["returns"]), (4000, []))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o3['id']}/edit", {
            "items": [{"product_id": ten["id"], "variant": "new", "quantity": 1}],
            "returns": [{"product_id": two["id"], "quantity": 1}], "return_mode": "cash"})
        self.assertEqual((r["order"]["total"], r["order"]["return_payout"], len(r["order"]["returns"])), (3000, 1000, 1))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o3['id']}/edit", {
            "items": [{"product_id": ten["id"], "variant": "new", "quantity": 1}], "returns": []})
        self.assertEqual((r["order"]["total"], r["order"]["returns"]), (4000, []))

    def test_admin_custom_buyout_price(self):
        """01.11.2026: админ в «Изменить» назначает свою сумму выкупа; не вписал — цена из карточки баллона."""
        self.fresh()
        five = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 3000, "return_price": 2000, "return_points": 3000})["product"]
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return",
                "return_items": [{"product_id": five["id"], "quantity": 1}]}
        body.pop("payment_method", None)
        ret = self.call(CUSTOMER, "POST", "/api/orders", body)["order"]
        self.assertEqual(ret["return_payout"], 2000)
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{ret['id']}/edit", {"items": [{"product_id": five["id"], "quantity": 2, "unit_price": 1000}]})
        self.assertEqual(r["order"]["return_payout"], 2000)  # 2 × 1000 по договорённости
        opts = self.call(ADMIN, "GET", f"/api/admin/orders/{ret['id']}/edit-options")
        self.assertEqual(opts["items"][0]["unit_price"], 1000)
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{ret['id']}/edit", {"items": [{"product_id": five["id"], "quantity": 2, "unit_price": None}]})
        self.assertEqual(r["order"]["return_payout"], 4000)  # пусто → цена из карточки (2000)
        # возврат внутри обычного заказа
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": five["id"], "variant": "new", "quantity": 1})
        o = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {
            "items": [{"product_id": five["id"], "variant": "new", "quantity": 1}],
            "returns": [{"product_id": five["id"], "quantity": 1, "unit_price": 1000}], "return_mode": "cash"})
        self.assertEqual((r["order"]["total"], r["order"]["return_payout"]), (2000, 1000))
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {
            "items": [{"product_id": five["id"], "variant": "new", "quantity": 1}],
            "returns": [{"product_id": five["id"], "quantity": 1}]})
        self.assertEqual((r["order"]["total"], r["order"]["return_payout"]), (1000, 2000))

    def test_courier_money_after_admin_edits(self):
        """01.11.2026: заработок и деньги курьера считаются по заказу в момент доставки — после правок админа."""
        self.fresh()
        ten = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 10 л", "description": "", "price": 4000, "return_price": 3000})["product"]
        two = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 2 л", "description": "", "price": 1000, "return_price": 1000})["product"]
        self.db.set_floor_lift_price(300)
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 1500, "balloon_next": 1000, "return": 100, "descent": 200})
        self.go_shift([], {str(ten["id"]): 9, str(two["id"]): 9})
        counters = lambda: self.call(COURIER, "GET", "/api/courier/shift")["counters"]

        def deliver(oid):
            admin_take(self.call, oid)
            arrive(self.call, oid)
            before = counters()
            self.call(COURIER, "POST", f"/api/courier/orders/{oid}/delivered", {"rating": 5})
            after = counters()
            return {k: after[k] - before[k] for k in ("earned", "cash", "returns", "keep")}

        # A. обычный заказ: 2 → 3 баллона и 2 подъёма
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": ten["id"], "variant": "new", "quantity": 2})
        a = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{a['id']}/edit", {"items": [{"product_id": ten["id"], "variant": "new", "quantity": 3}], "lift_floors": 2})
        self.assertEqual(r["order"]["total"], 3 * 4000 + 2 * 300)
        self.assertEqual(deliver(a["id"]), {"earned": 1500 + 1000 + 1000 + 2 * 300, "cash": 12600, "returns": 0, "keep": 1500 + 1000 + 1000 + 600})
        # B. покупка + возврат со своей суммой (1000 вместо цены из карточки)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": ten["id"], "variant": "new", "quantity": 1})
        b = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{b['id']}/edit", {
            "items": [{"product_id": ten["id"], "variant": "new", "quantity": 1}],
            "returns": [{"product_id": two["id"], "quantity": 1, "unit_price": 700}], "return_mode": "cash"})
        self.assertEqual(r["order"]["total"], 4000 - 700)
        self.assertEqual(deliver(b["id"]), {"earned": 1500, "cash": 3300, "returns": 0, "keep": 1500})
        # C. чистый возврат: 2 баллона по своей цене 1000 и один спуск
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return",
                "return_items": [{"product_id": ten["id"], "quantity": 1}]}
        body.pop("payment_method", None)
        c = self.call(CUSTOMER, "POST", "/api/orders", body)["order"]
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{c['id']}/edit", {
            "items": [{"product_id": ten["id"], "quantity": 2, "unit_price": 1000}], "descent_count": 1})
        self.assertEqual(r["order"]["return_payout"], 2 * 1000 - 200)
        self.assertEqual(deliver(c["id"]), {"earned": 100 + 200, "cash": 0, "returns": -1800, "keep": 300})

    def test_order_20_accounting(self):
        """Заказ №20 со скриншотов (01.11.2026): замена 10 л 9000 + шарики 25 шт 1000 + подарок шарики 5 шт + подъём 500
        + возврат 5 л по своей сумме 1000. Партнёр, клиент, курьер, магазин — вся бухгалтерия."""
        self.fresh()
        ten = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 10л.", "description": "", "price": 11000, "price_exchange": 9000, "return_price": 3000, "return_points": 4000})["product"]
        five = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5л.", "description": "", "price": 6000, "price_exchange": 4500, "return_price": 2000, "return_points": 3000})["product"]
        s5 = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Шарики 5 штук", "description": "", "price": 500})["product"]
        s25 = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Шарики 25 штук", "description": "", "price": 1000})["product"]
        self.call(ADMIN, "PATCH", f"/api/admin/products/{s25['id']}", {"bonus_product_id": s5["id"], "bonus_qty": 1, "bonus_desc": "подарок"})
        self.db.set_floor_lift_price(500)
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 1500, "balloon_next": 1000, "return": 1500, "descent": 0})
        # партнёр (CUSTOMER) и приведённый клиент (CUSTOMER2)
        self.call(CUSTOMER, "POST", "/api/referral/apply", {"kind": "partner", "comment": "промоутер"})
        req = self.call(ADMIN, "GET", "/api/referral")["requests"][0]
        code = self.call(ADMIN, "POST", f"/api/admin/referral/{req['id']}/approve")["partners"][0]["code"]
        self.db.upsert_user(CUSTOMER2.id, "irma", "Ирма", None)
        self.assertEqual(self.db.bind_referral(CUSTOMER2.id, code), "bound")
        self.go_shift([], {str(ten["id"]): 5, str(five["id"]): 5, str(s5["id"]): 5, str(s25["id"]): 5})
        stock0 = self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"]
        counters0 = self.call(COURIER, "GET", "/api/courier/shift")["counters"]
        for pid, var in ((ten["id"], "exchange"), (s25["id"], "new")):
            self.call(CUSTOMER2, "POST", "/api/cart", {"product_id": pid, "variant": var, "quantity": 1})
        b = {"payment_method": "cash_on_delivery", "phone": "+79251328225", "address": "Москва, ул. Новая, д. 5", "floor_lift_count": 1}
        o = self.call(CUSTOMER2, "POST", "/api/orders", b)["order"]
        self.assertEqual(o["total"], 9000 + 1000 + 500)
        r = self.call(ADMIN, "POST", f"/api/admin/orders/{o['id']}/edit", {
            "items": [{"product_id": ten["id"], "variant": "exchange", "quantity": 1}, {"product_id": s25["id"], "variant": "new", "quantity": 1}],
            "lift_floors": 1, "returns": [{"product_id": five["id"], "quantity": 1, "unit_price": 1000}], "return_mode": "cash"})
        self.assertEqual(r["order"]["total"], 9500)
        admin_take(self.call, o["id"]); arrive(self.call, o["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})
        c = self.call(COURIER, "GET", "/api/courier/shift")
        cnt = {k: c["counters"][k] - counters0[k] for k in ("earned", "cash", "returns", "keep")}
        stock = c["stock"]["now"]
        out = {
            "клиент платит курьеру": 9500,
            "курьер: заработал": cnt["earned"], "курьер: собрал наличными": cnt["cash"], "курьер: оставил себе": cnt["keep"],
            "курьер: сдать в кассу": cnt["cash"] + cnt["returns"] - cnt["keep"],
            "бонусы клиенту": self.db.bonus_balance(CUSTOMER2.id),
            "баллы партнёру": self.db.partner_points_balance(CUSTOMER.id),
            "остаток 10л": stock[str(ten["id"])] - stock0[str(ten["id"])], "остаток шарики 25": stock[str(s25["id"])] - stock0[str(s25["id"])],
            "остаток шарики 5 (подарок)": stock[str(s5["id"])] - stock0[str(s5["id"])],
            "пустые": {k: v for k, v in c["empties"].items() if v},
            "история баллов партнёра": self.call(CUSTOMER, "GET", "/api/referral")["partner"]["history"][0]["reason"],
        }
        print("\nORDER20", out)
        self.assertEqual((cnt["earned"], cnt["cash"], cnt["keep"], cnt["returns"]), (2000, 9500, 2000, 0))
        self.assertEqual(out["бонусы клиенту"], 200)
        self.assertEqual(out["баллы партнёру"], 200)
        self.assertEqual((out["остаток 10л"], out["остаток шарики 25"], out["остаток шарики 5 (подарок)"]), (-1, -1, -1))
        self.assertEqual(out["пустые"], {"5": 1, "10": 1})

    def test_admin_delete_history_orders(self):
        self.fresh()
        p = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 10л.", "description": "", "price": 11000, "price_exchange": 9000})["product"]
        self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 1500, "balloon_next": 1000, "return": 1500, "descent": 0})
        self.go_shift([], {str(p["id"]): 5})
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": p["id"], "variant": "exchange", "quantity": 1})
        b = {"payment_method": "cash_on_delivery", "phone": "+79251328225", "address": "Москва, ул. Новая, д. 5"}
        done = self.call(CUSTOMER, "POST", "/api/orders", b)["order"]
        admin_take(self.call, done["id"]); arrive(self.call, done["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{done['id']}/delivered", {"rating": 5})
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": p["id"], "variant": "exchange", "quantity": 1})
        active = self.call(CUSTOMER, "POST", "/api/orders", b)["order"]
        # без подтверждения — отказ
        with self.assertRaises(Exception):
            self.call(ADMIN, "POST", "/api/admin/history/delete", {"order_ids": [done["id"]]})
        # без пароля (не задан / неверный) удалять нельзя
        with self.assertRaises(Exception):
            self.call(ADMIN, "POST", "/api/admin/history/delete", {"order_ids": [done["id"]], "confirm": True, "password": "delpass1"})
        self.call(ADMIN, "POST", "/api/admin/dev/unlock", {"password": "delpass1"})
        with self.assertRaises(Exception):
            self.call(ADMIN, "POST", "/api/admin/history/delete", {"order_ids": [done["id"]], "confirm": True, "password": "wrong"})
        with self.assertRaises(Exception):
            self.call(ADMIN, "POST", "/api/admin/history/delete", {"order_ids": [done["id"]], "confirm": True})
        c0 = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertGreater(c0["counters"]["earned"], 0)
        self.assertGreater(self.db.bonus_balance(CUSTOMER.id), 0)
        self.assertEqual(c0["stock"]["now"][str(p["id"])], 4)  # доставленный заказ уже вычтен
        # активный заказ не удаляется, доставленный — удаляется, даже пока курьер на смене
        res = self.call(ADMIN, "POST", "/api/admin/history/delete", {"order_ids": [done["id"], active["id"]], "confirm": True, "password": "delpass1"})
        self.assertEqual(res["deleted"], [done["id"]])
        self.assertEqual([s["id"] for s in res["skipped"]], [active["id"]])
        with self.db.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM orders WHERE id = ?", (done["id"],)).fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM order_items WHERE order_id = ?", (done["id"],)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM courier_events WHERE order_id = ?", (done["id"],)).fetchone()[0], 0)
        # все деньги и баллы по заказу откатились: как до выполнения
        c1 = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((c1["counters"]["earned"], c1["counters"]["cash"], c1["counters"]["keep"], c1["counters"]["returns"]), (0, 0, 0, 0))
        self.assertEqual(self.db.bonus_balance(CUSTOMER.id), 0)
        self.assertEqual(c1["stock"]["now"][str(p["id"])], 5)  # баллон вернулся в машину
        hist = self.call(ADMIN, "GET", "/api/admin/history", status="all")
        self.assertEqual([o["id"] for o in hist["orders"]], [active["id"]])

    def test_balloon_stock_edge_cases(self):
        """Назначенный до смены заказ и заказ, изменённый после доставки, считаются правильно."""
        from datetime import datetime, timedelta, timezone
        self.fresh()
        pid = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 1000})["product"]["id"]

        def order():
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": pid, "variant": "new", "quantity": 1})
            return self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]

        now = lambda: self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"][str(pid)]
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        early = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        pre = order()
        with self.assertRaises(ApiError) as ctx:  # 28.10.2026: не на смене — назначить нельзя
            self.call(ADMIN, "POST", f"/api/admin/orders/{pre['id']}/assign", {"courier_id": petr["id"]})
        self.assertIn("не на смене", ctx.exception.message)
        self.go_shift(["center"], {str(pid): 5})
        self.call(ADMIN, "POST", f"/api/admin/orders/{pre['id']}/assign", {"courier_id": petr["id"]})
        self.assertEqual(now(), 4)  # баллон для назначенного заказа «занят»
        arrive(self.call, pre["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{pre['id']}/delivered", {"rating": 5})
        self.assertEqual(now(), 4)
        # склад: в машине 6; доставленный до загрузки заказ потом меняется — остаток не трогается
        self.call(COURIER, "POST", "/api/courier/reload", {"stock": {str(pid): 6}, "empties_ack": True})
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/reload", {"approve": True})
        with self.db.connection() as conn:
            conn.execute("UPDATE couriers SET shift_reload_at = ? WHERE id = ?",
                         ((datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="seconds"), petr["id"]))
            conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?",
                         ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds"), pre["id"]))
        self.assertEqual(now(), 6)
        b = order()
        admin_take(self.call, b['id'])
        self.assertEqual(now(), 5)
        self.call(COURIER, "POST", f"/api/courier/orders/{b['id']}/release")  # «взял по ошибке»
        self.assertEqual(now(), 6)

    def test_courier_money_returns_and_shift_journal(self):
        """Заработок / наличные / возвраты, пустые баллоны, «Возврат баллона» и журнал смены."""
        self.fresh()
        five = self.call(ADMIN, "POST", "/api/admin/products",
                         {"name": "Баллон 5 л", "description": "", "price": 3000, "price_exchange": 2000, "return_price": 700})["product"]
        self.assertEqual(five["return_price"], 700)
        self.call(ADMIN, "PUT", "/api/admin/settings", {"floor_lift_price": 300})
        # 28.10.2026: ставки по умолчанию 0; курьеру — за каждый отвезённый баллон и за каждый забранный пустой (возврат)
        self.assertEqual(self.db.courier_rates(), {"balloon": 0, "balloon_next": 0, "return": 1500, "descent": 0})  # возврат — 1 500 ₽ за поездку
        with self.assertRaises(ApiError):
            self.call(COURIER, "PUT", "/api/admin/courier-rates", {"balloon": 1000, "return": 500})
        self.assertEqual(self.call(ADMIN, "PUT", "/api/admin/courier-rates", {"balloon": 1000, "return": 500})["courier_rates"],
                         {"balloon": 1000, "balloon_next": 1000, "return": 500, "descent": 0})
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        self.go_shift(["center"], {str(five["id"]): 5})
        k5 = str(five["id"])

        def deliver(o):
            admin_take(self.call, o['id'])
            arrive(self.call, o["id"])
            self.call(COURIER, "POST", f"/api/courier/orders/{o['id']}/delivered", {"rating": 5})

        # наличными: новый + замена, подъём 300 ₽ → заработок 2 баллона × 1000 + 300; собрано — вся сумма
        # покупателя 3000 + 2000 + 300 (28.10.2026: подъём больше не вычитается из «Собрано»)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": five["id"], "variant": "new", "quantity": 1})
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": five["id"], "variant": "exchange", "quantity": 1})
        cash = self.call(CUSTOMER, "POST", "/api/orders", {**self.body(line="Москва, неизвестная улица", house="5"), "floor_lift": True})["order"]
        deliver(cash)
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["counters"]["earned"], sh["counters"]["cash"], sh["counters"]["returns"]), (2300, 5300, 0))
        self.assertEqual((sh["stock"]["now"][k5], sh["empties"]["5"]), (3, 1))  # замена — пустой в машине
        # возврат баллона: ничего не платит, курьер отдаёт 2 × 700, полные не трогаются, пустых +2
        self.assertEqual([x["id"] for x in self.call(CUSTOMER, "GET", "/api/returns")["items"]], [five["id"]])
        body = {**self.body(line="Москва, неизвестная улица", house="5"), "kind": "return",
                "return_items": [{"product_id": five["id"], "quantity": 2}]}
        body.pop("payment_method", None)
        ret = self.call(CUSTOMER, "POST", "/api/orders", body)["order"]
        self.assertEqual((ret["is_return"], ret["total"], ret["return_payout"]), (True, 0, 1400))
        deliver(ret)
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["counters"]["earned"], sh["counters"]["cash"], sh["counters"]["returns"]), (2800, 5300, -1400))
        self.assertEqual((sh["stock"]["now"][k5], sh["empties"]["5"]), (3, 3))
        # покупателю за возврат бонусы не начисляются
        self.assertFalse(any(h.get("order_id") == ret["id"] for h in self.call(CUSTOMER, "GET", "/api/bonus").get("history", [])))
        # курьер сам правит пустые; «Обнулить» наличные — только запрос: обнуляет админ (28.10.2026)
        self.call(COURIER, "POST", "/api/courier/empties", {"empty": {"2": 0, "5": 1, "10": 0}})
        sh = self.call(COURIER, "POST", "/api/courier/counters/reset", {"name": "cash"})
        self.assertEqual((sh["counters"]["cash"], sh["reset_request"]["names"]), (5300, ["cash"]))  # деньги на месте
        with self.assertRaises(ApiError):  # второй запрос, пока первый ждёт
            self.call(COURIER, "POST", "/api/courier/counters/reset", {"name": "earned"})
        alert = next(x for x in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if x["kind"] == "counter_reset")
        self.assertIn("Наличные за товары", alert["title"])
        with self.assertRaises(ApiError):  # подтверждает только админ
            self.call(COURIER, "POST", f"/api/admin/couriers/{petr['id']}/counter-reset", {"approve": True})
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/counter-reset", {"approve": True})
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["counters"]["earned"], sh["counters"]["cash"], sh["empties"]["5"], sh["reset_request"]), (2800, 0, 1, None))
        # отклонённый запрос ничего не обнуляет
        self.call(COURIER, "POST", "/api/courier/counters/reset", {"name": "earned"})
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/counter-reset", {"approve": False})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["counters"]["earned"], 2800)
        cour = next(c for c in self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/counters/reset", {"name": "returns"})["couriers"]
                    if c["id"] == petr["id"])
        self.assertEqual((cour["counters"]["returns"], cour["counters"]["earned"]), (0, 2800))
        # журнал смены: закрыть смену → в списке, подробно все действия
        self.call(COURIER, "POST", "/api/courier/shift/end")
        # 28.10.2026: админ видит остатки — полные, пустые с замен и с возвратов, деньги — и подтверждает
        end = next(a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift_end")
        self.assertEqual(({i["name"]: i["qty"] for i in end["end"]["stock"]}["Баллон 5 л"], end["end"]["empties"]["manual"]["5"]),
                         (3, 1))
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift-end")
        left = {i["name"]: i["qty"] for i in self.call(COURIER, "GET", "/api/courier/shift")["stock_left"]}
        self.assertEqual(left["Баллон 5 л"], 3)  # следующая смена — с этим остатком
        shifts = self.call(ADMIN, "GET", f"/api/admin/couriers/{petr['id']}/shifts")["shifts"]
        self.assertEqual((shifts[0]["open"], shifts[0]["end_reason"], shifts[0]["deliveries"], shifts[0]["pickups"]),
                         (False, "self", 1, 1))
        det = self.call(ADMIN, "GET", f"/api/admin/shifts/{shifts[0]['id']}")
        kinds = [e["kind"] for e in det["events"]]
        for k in ("shift_start", "take", "delivered", "return_picked", "empties_set", "reset_cash", "reset_returns", "shift_end"):
            self.assertIn(k, kinds)
        self.assertEqual((det["shift"]["earned"], det["shift"]["cash"], det["shift"]["returns"]), (2800, 5300, -1400))
        self.assertEqual(det["shift"]["stock_end"]["Баллон 5 л"], 3)
        # 28.10.2026: статистика курьеров — неделя пн–вс по дням и календарный месяц
        from datetime import datetime, timedelta, timezone as tz
        msk_today = datetime.now(tz(timedelta(hours=3))).date()
        wk = self.call(ADMIN, "GET", "/api/admin/courier-stats", period="week")
        self.assertEqual((len(wk["days"]), wk["days"][0]["label"][:2], wk["from"]),
                         (7, "Пн", (msk_today - timedelta(days=msk_today.weekday())).isoformat()))
        p = next(c for c in wk["couriers"] if c["id"] == petr["id"])
        self.assertEqual({k: p[k] for k in ("orders", "pickups", "balloons", "cash", "paid", "to_hand", "earned")},
                         {"orders": 1, "pickups": 1, "balloons": 2, "cash": 5300, "paid": 1400,
                          # 28.10.2026 (владелец): в кассу — после вычета заработка: 5300 − 1400 − 2800
                          "to_hand": 1100, "earned": 2800})
        self.assertEqual(list(p["days"]), [msk_today.isoformat()])  # остальные дни недели — пустые
        mo = self.call(ADMIN, "GET", "/api/admin/courier-stats", period="month")
        self.assertEqual((mo["from"], len(mo["days"]) >= 28), (msk_today.replace(day=1).isoformat(), True))
        self.assertEqual(next(c for c in mo["couriers"] if c["id"] == petr["id"])["to_hand"], 1100)
        prev = self.call(ADMIN, "GET", "/api/admin/courier-stats", period="week", offset="-1")
        self.assertEqual(next(c for c in prev["couriers"] if c["id"] == petr["id"])["orders"], 0)
        for bad in ({"period": "year"}, {"period": "week", "offset": "1"}):
            with self.assertRaises(ApiError):
                self.call(ADMIN, "GET", "/api/admin/courier-stats", **bad)
        with self.assertRaises(ApiError):
            self.call(COURIER, "GET", "/api/admin/courier-stats")

    def test_region_without_street_pick_old_map_and_backfill(self):
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        typed = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, Лесная улица", house="7"))["order"]
        self.assertEqual(typed["region"], "center")  # улицу не выбрали из подсказок — нашли по тексту
        geo = self.api.geo
        geo.ttk = None  # на сервере старая база без контура ТТК — берётся контур из сборки 22.10
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        south = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, Профсоюзная улица", house="15"))["order"]
        self.assertEqual(south["region"], "south")
        # заказ оформлен, когда карты не было, — регион появляется, когда админ открывает «Заказы»
        self.api.geo = None
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        old = self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, Лесная улица", house="12"))["order"]
        self.assertIsNone(old["region"])
        self.api.geo = geo
        regions = {o["id"]: o["region"] for o in self.call(ADMIN, "GET", "/api/admin/orders")["orders"]}
        self.assertEqual(regions[old["id"]], "center")

    def test_assigned_orders_popup_until_ack(self):
        def mk():
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
            return self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, Лесная улица", house="7"))["order"]
        a, b, c = mk(), mk(), mk()
        petr = next(x for x in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if x["telegram_id"] == COURIER.id)
        self.call(ADMIN, "POST", f"/api/admin/orders/{a['id']}/assign", {"courier_id": petr["id"]})
        self.call(ADMIN, "POST", "/api/admin/orders/assign", {"order_ids": [b["id"], c["id"]], "courier_id": petr["id"]})
        shown = self.call(COURIER, "GET", "/api/badges")["courier"]["assigned"]
        self.assertEqual([o["id"] for o in shown], [a["id"], b["id"], c["id"]])
        self.assertEqual(shown[0]["region"], "Центр")
        self.assertIsNone(self.call(CUSTOMER, "GET", "/api/badges").get("courier"))
        self.call(COURIER, "POST", "/api/courier/assigned/ack", {"ids": [a["id"], b["id"]]})
        self.assertEqual([o["id"] for o in self.call(COURIER, "GET", "/api/badges")["courier"]["assigned"]], [c["id"]])
        self.call(ADMIN, "POST", f"/api/admin/orders/{c['id']}/status", {"status": "cancelled"})  # отменён — окна нет
        self.assertEqual(self.call(COURIER, "GET", "/api/badges")["courier"]["assigned"], [])

    def test_warehouse_reload_and_region_request_badge(self):
        self.fresh()
        pid = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Баллон 5 л", "description": "", "price": 1000})["product"]["id"]

        def order(qty):
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": pid, "variant": "new", "quantity": qty})
            return self.call(CUSTOMER, "POST", "/api/orders", self.body(line="Москва, неизвестная улица", house="5"))["order"]

        self.go_shift(["center"], {str(pid): 2})
        k = str(pid)
        a, b = order(2), order(1)
        admin_take(self.call, a['id'])       # везёт 2, остаток 0
        with self.assertRaises(ApiError):
            admin_take(self.call, b['id'])   # нет баллонов
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/reload", {"stock": {k: "x"}, "empties_ack": True})
        # склад: в машине теперь 5 штук 5 л (2 из них — для заказа, который ещё везёт)
        with self.assertRaises(ApiError):  # без «Понял, не забуду» (сдать пустые) — нельзя
            self.call(COURIER, "POST", "/api/courier/reload", {"stock": {k: 5}})
        sh = self.call(COURIER, "POST", "/api/courier/reload", {"stock": {k: 5}, "empties_ack": True})
        # пока админ не подтвердил — остаток прежний
        self.assertEqual((sh["reload_pending"]["Баллон 5 л"], sh["stock"]["now"][k]), (5, 0))
        self.assertEqual(self.bridge.events[-1]["event"], "admin_alert")
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["reload_requests"], 1)
        petr = next(x for x in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if x["telegram_id"] == COURIER.id)
        alerts = self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]
        self.assertEqual([(x["kind"], x["source"]) for x in alerts], [("reload", "courier")])
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/reload", {"approve": True})
        with self.assertRaises(ApiError):
            self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/reload", {"approve": True})
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["stock"]["reload"][k], sh["stock"]["now"][k], sh["stock"]["start"][k], sh["reload_pending"]), (5, 3, 2, None))
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"], [])
        admin_take(self.call, b['id'])
        arrive(self.call, a["id"])
        self.call(COURIER, "POST", f"/api/courier/orders/{a['id']}/delivered", {"rating": 5})
        self.assertEqual(self.call(COURIER, "GET", "/api/courier/shift")["stock"]["now"][k], 2)
        # 28.10.2026: работа по регионам убрана — запроса «сменить регион» больше нет
        self.assertNotIn("region_requests", self.call(ADMIN, "GET", "/api/badges")["admin"])
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/region-request")

    def test_products_drag_order(self):
        """Админ перетаскивает товары — в таком порядке их видят каталог, бот и курьер (26.10.2026)."""
        ids = [p["id"] for p in self.call(ADMIN, "GET", "/api/admin/products")["products"]]
        self.assertGreater(len(ids), 2)
        new_order = list(reversed(ids))
        r = self.call(ADMIN, "PUT", "/api/admin/products/order", {"ids": new_order})
        self.assertEqual([p["id"] for p in r["products"]], new_order)
        self.assertEqual([p["id"] for p in self.call(CUSTOMER, "GET", "/api/catalog")["products"]], new_order)
        self.assertEqual([p["id"] for p in self.db.stock_catalog()], new_order)
        # новый товар — в конец
        pid = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Новинка", "description": "", "price": 10})["product"]["id"]
        self.assertEqual([p["id"] for p in self.call(CUSTOMER, "GET", "/api/catalog")["products"]], new_order + [pid])
        # список не совпадает с товарами в продаже (кто-то добавил/скрыл) — 409, порядок не меняется
        for bad in (new_order, new_order + [pid, pid], [*new_order, 99999], "x"):
            with self.assertRaises(ApiError) as ctx:
                self.call(ADMIN, "PUT", "/api/admin/products/order", {"ids": bad})
            self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "PUT", "/api/admin/products/order", {"ids": new_order + [pid]})
        # скрытый и возвращённый товар сохраняет место
        self.call(ADMIN, "POST", f"/api/admin/products/{new_order[0]}/hide")
        self.call(ADMIN, "POST", f"/api/admin/products/{new_order[0]}/restore")
        self.assertEqual([p["id"] for p in self.call(CUSTOMER, "GET", "/api/catalog")["products"]], new_order + [pid])

    def test_shift_start_needs_admin_approval(self):
        """«В путь!» (26.10.2026): курьер вводит все товары каталога, ждёт; админ подтверждает / исправляет / отклоняет."""
        self.fresh()
        petr = next(c for c in self.call(ADMIN, "GET", "/api/admin/couriers")["couriers"] if c["telegram_id"] == COURIER.id)
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        catalog = {p["id"] for p in sh["stock_items"]}
        self.assertEqual(catalog, {p["id"] for p in self.db.stock_catalog()})
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"999": 1}})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 1000}})
        # заявка: на смену не вышел, ждёт; админу — сообщение в бот и плашка на главной
        sh = self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 2, "3": 4}})
        self.assertEqual((sh["active"], sh["status"]), (False, "offline"))
        self.assertEqual({i["id"]: i["qty"] for i in sh["request"]["items"]}[3], 4)
        self.assertIn("хочет выйти на смену", self.bridge.events[-1]["text"])
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 2, "3": 5}})  # повтор — без 2-го сообщения
        self.assertEqual(sum(1 for e in self.bridge.events if "хочет выйти на смену" in (e.get("text") or "")), 1)
        alerts = [a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift"]
        self.assertEqual((len(alerts), alerts[0]["id"], alerts[0]["source"]), (1, petr["id"], "courier"))
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["shift_requests"], 1)
        self.assertEqual(self.available(), [])
        with self.assertRaises(ApiError):  # не админ
            self.call(COURIER, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True})
        # отклонить → курьер видит, что отклонено, и правит
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": False})
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["active"], sh["request"]), (False, None))
        self.assertTrue(sh["request_declined_at"])
        self.assertIn("не подтвердил", self.bridge.events[-1]["text"])
        with self.assertRaises(ApiError):  # заявки уже нет
            self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True})
        # «✏️ Изменить» снимает заявку
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 1}})
        self.assertIsNone(self.call(COURIER, "POST", "/api/courier/shift/request/cancel")["request"])
        self.assertEqual([a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift"], [])
        # исправить количества и подтвердить → смена с исправленными остатками
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 2, "3": 4}})
        # новый товар в каталоге сразу появляется: в «Исправить» у админа (0 шт.) и на экране «В путь!» курьера
        new = self.call(ADMIN, "POST", "/api/admin/products", {"name": "Новый товар", "description": "", "price": 100})["product"]["id"]
        al = next(a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift")
        self.assertEqual({i["id"]: i["qty"] for i in al["items"]}[new], 0)
        self.assertIn(new, [p["id"] for p in self.call(COURIER, "GET", "/api/courier/shift")["stock_items"]])
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True, "stock": {"1": 3, "3": 4}})
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["active"], sh["status"], sh["request"], sh["stock"]["start"]["1"], sh["stock"]["start"]["3"]),
                         (True, "online", None, 3, 4))
        self.assertIn("Количества исправлены", self.bridge.events[-1]["text"])
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["shift_requests"], 0)
        with self.assertRaises(ApiError) as ctx:
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "stock": {"1": 1}})
        self.assertEqual(ctx.exception.status, 409)
        # перерыв → «Вернуться на смену» без подтверждения; «Закончить смену» — сразу
        self.call(COURIER, "POST", "/api/courier/shift/break")
        self.assertTrue(self.call(COURIER, "POST", "/api/courier/shift/resume")["active"])
        self.assertEqual(self.call(COURIER, "POST", "/api/courier/shift/end")["status"], "ending")
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift-end")
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["status"], {i["id"]: i["qty"] for i in sh["stock_left"]}[1]), ("offline", 3))
        # следующая смена: «В путь!» с остатком (без ввода) — админ видит «остаток прошлой смены»
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": []})
        al = next(a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift")
        self.assertIn("остаток прошлой смены", al["title"])
        self.call(COURIER, "POST", "/api/courier/shift/request/cancel")
        # «Стартую со склада»: всё вводит заново, пустые сдаёт («Понял, не забуду»)
        with self.assertRaises(ApiError):
            self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "warehouse": True, "stock": {"1": 9}})
        self.call(COURIER, "POST", "/api/courier/shift", {"regions": [], "warehouse": True, "stock": {"1": 9}, "empties_ack": True})
        al = next(a for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"] if a["kind"] == "shift")
        self.assertIn("со склада", al["title"])
        self.assertIsNotNone(al["empties_handed"])
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/shift", {"approve": True})
        sh = self.call(COURIER, "GET", "/api/courier/shift")
        self.assertEqual((sh["active"], sh["stock"]["start"]["1"], sum(sh["empties"].values())), (True, 9, 0))
        # перерыв: уведомление админу висит до «ОК» или до возвращения
        self.call(COURIER, "POST", "/api/courier/shift/break")
        kinds = lambda: [a["kind"] for a in self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]]  # noqa: E731
        self.assertIn("break", kinds())
        self.call(ADMIN, "POST", f"/api/admin/couriers/{petr['id']}/break-seen")
        self.assertNotIn("break", kinds())
        self.call(COURIER, "POST", "/api/courier/shift/resume")
        self.call(COURIER, "POST", "/api/courier/shift/break")
        self.assertIn("break", kinds())  # новый перерыв — снова видно
        self.call(COURIER, "POST", "/api/courier/shift/resume")
        self.assertNotIn("break", kinds())

    def test_admin_alerts_until_resolved(self):
        self.fresh()
        self.call(CUSTOMER, "POST", "/api/call-admin", {"text": "Где мой заказ?"})
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        tula = self.call(CUSTOMER, "GET", "/api/address/suggest", q="советская")["suggestions"][0]
        ind = self.call(CUSTOMER, "POST", "/api/orders", self.body(line=tula["value"], street_id=tula["id"], house="1"))["order"]
        self.go_shift([], {"1": 1})
        alerts = self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"]
        kinds = {a["kind"]: a for a in alerts}
        self.assertEqual(set(kinds), {"individual", "call"})
        self.assertEqual((kinds["call"]["source"], kinds["individual"]["source"]), ("customer", "customer"))
        self.assertEqual((kinds["call"]["text"], kinds["individual"]["id"]), ("Где мой заказ?", ind["id"]))
        self.assertEqual(self.call(ADMIN, "GET", "/api/badges")["admin"]["alerts"], 2)
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "GET", "/api/admin/alerts")
        # решено — пропадает
        self.call(ADMIN, "POST", f"/api/admin/calls/{kinds['call']['id']}/resolve")
        with self.assertRaises(ApiError):
            self.call(ADMIN, "POST", f"/api/admin/calls/{kinds['call']['id']}/resolve")
        admin_take(self.call, ind['id'])  # индивидуальный: админ сам назначил курьера
        self.assertEqual(self.call(ADMIN, "GET", "/api/admin/alerts")["alerts"], [])

    def test_sector_math(self):
        from webapp.regions import clean_regions, sector
        self.assertEqual([sector(55.90, 37.62), sector(55.75, 37.90), sector(55.60, 37.62), sector(55.75, 37.30)],
                         ["north", "east", "south", "west"])
        # диагональ с поправкой на широту: 0.1° на север ≈ 0.178° на восток
        self.assertEqual(sector(55.752 + 0.1, 37.6175 + 0.17), "north")
        self.assertEqual(sector(55.752 + 0.1, 37.6175 + 0.19), "east")
        self.assertEqual(clean_regions(["west", "west", "x", "east", "south"]), ["west", "east"])


class AddressAdminTests(AddressCheckoutTests):
    def test_status_and_update_without_token(self):
        import os
        from unittest import mock
        st = self.call(ADMIN, "GET", "/api/admin/address-check")
        self.assertTrue(st["ok"])
        self.assertEqual((st["houses"], st["moscow_houses"]), (12, 8))
        with self.assertRaises(ApiError):
            self.call(CUSTOMER, "GET", "/api/admin/address-check")
        with mock.patch.dict(os.environ, {"GEODB_GITHUB_TOKEN": ""}):
            with self.assertRaises(ApiError) as ctx:
                self.call(ADMIN, "POST", "/api/admin/address-update")
        self.assertIn("GEODB_GITHUB_TOKEN", ctx.exception.message)


class BuildAddressDbTests(unittest.TestCase):
    def test_builder_and_polygon(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from build_address_db import PolygonIndex, house_key, query_tokens, _simplify
        self.assertEqual(query_tokens("ул. Лесная, д 7"), ["лесная", "7"])
        self.assertEqual([house_key("7 к2"), house_key("7К2"), house_key("7, корп. 2")], ["7к2", "7к2", "7к2"])
        sq = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        self.assertEqual(len(_simplify(sq, 0.0002)), 5)  # замкнутое кольцо не схлопывается
        donut = PolygonIndex([[sq, [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]]])
        self.assertEqual([donut.contains(0.2, 0.2), donut.contains(0.5, 0.5), donut.contains(2, 2)], [True, False, False])


class BuildAddressCleanTests(unittest.TestCase):
    def test_clean_names(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from build_address_db import _MOSCOW_DISTRICT, clean
        self.assertEqual(clean("​Ленинский  проспект "), "Ленинский проспект")
        self.assertTrue(_MOSCOW_DISTRICT.match("район Тёплый Стан"))
        self.assertFalse(_MOSCOW_DISTRICT.match("Зеленоград"))


class GeoTokenTests(unittest.TestCase):
    def test_token_lookup_tolerates_typos(self):
        import os
        from unittest import mock
        from webapp.geodb import find_token, token_missing_message
        with mock.patch.dict(os.environ, {"GEODB_GITHUB_TOKEN": " abc "}, clear=False):
            self.assertEqual(find_token(), ("GEODB_GITHUB_TOKEN", "abc"))
        env = {k: v for k, v in os.environ.items() if "GEODB" not in k.upper()}
        with mock.patch.dict(os.environ, {**env, "GEODB_GITHUB_TОKEN ": "xyz"}, clear=True):  # русская «О» и пробел
            self.assertEqual(find_token(), ("GEODB_GITHUB_TОKEN ", "xyz"))
        with mock.patch.dict(os.environ, {**env, "GEODB_GITHUB_TOKEN_OLD": ""}, clear=True):
            self.assertEqual(find_token(), (None, ""))
            self.assertIn("«GEODB_GITHUB_TOKEN_OLD»", token_missing_message())
            self.assertIn("этап «Запуск»", token_missing_message())


class PhoneFormatTests(CustomerBonusTests):
    def test_phone_normalized_and_validated(self):
        from webapp.core import normalize_phone
        self.assertEqual(normalize_phone("+7 (916) 123-45-67"), "8 916 123 45 67")
        self.assertEqual(normalize_phone("89161234567"), "8 916 123 45 67")
        for bad in ("916 123 45 67", "8 916 123 45 678", "9 916 123 45 67", ""):
            with self.assertRaises(ApiError):
                normalize_phone(bad)
        self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
        with self.assertRaises(ApiError) as ctx:
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "8 916 123",
                                                        "address": "Москва, ул. Тестовая, д. 1"})
        self.assertIn("8 916 123 45 67", ctx.exception.message)

    def test_delete_saved_address(self):
        for line in ("г Москва, ул Первая, д 1", "г Москва, ул Вторая, д 2"):
            self.call(CUSTOMER, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
            self.call(CUSTOMER, "POST", "/api/orders", {"payment_method": "cash_on_delivery", "phone": "89161234567",
                                                        "address_parts": {"line": line}})
        saved = self.call(CUSTOMER, "GET", "/api/checkout")["saved_addresses"]
        self.assertEqual([a["line"] for a in saved], ["г Москва, ул Вторая, д 2", "г Москва, ул Первая, д 1"])
        with self.assertRaises(ApiError):  # не тот адрес под номером — не удаляем
            self.call(CUSTOMER, "DELETE", "/api/addresses/saved/0", {"line": "г Москва, ул Первая, д 1"})
        left = self.call(CUSTOMER, "DELETE", "/api/addresses/saved/0", {"line": "г Москва, ул Вторая, д 2"})["saved_addresses"]
        self.assertEqual([a["line"] for a in left], ["г Москва, ул Первая, д 1"])
        self.assertEqual(self.call(CUSTOMER2, "GET", "/api/checkout")["saved_addresses"], [])
        self.assertEqual(self.call(CUSTOMER, "GET", "/api/checkout")["phone_formatted"], "8 916 123 45 67")


class LuxButtonsMigrationTests(unittest.TestCase):
    """Премиум-кнопки включаются у уже сохранённой темы один раз и не
    перезаписывают выбор, сделанный админом после этого."""

    def test_lux_applied_once(self):
        import json as _json
        from db import Database
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db = Database(path)
            db.initialize()
            self.assertEqual(db.get_theme()["button_fill"], "lux")
            # старая тема без метки (как на проде до обновления)
            with db.connection() as conn:
                conn.execute("UPDATE app_theme SET theme_json = ? WHERE id = 1",
                             (_json.dumps({"button_fill": "solid", "accent_color": "#123456"}),))
            Database(path).initialize()
            theme = db.get_theme()
            self.assertEqual(theme["button_fill"], "lux")
            self.assertEqual(theme["accent_color"], "#123456")
            # админ сам выбрал другую заливку — перезапуск её не трогает
            db.set_theme({"button_fill": "glass"})
            Database(path).initialize()
            self.assertEqual(db.get_theme()["button_fill"], "glass")


class BonusTransferTests(unittest.TestCase):
    setUp = ApiFlowTests.setUp
    tearDown = ApiFlowTests.tearDown
    call = ApiFlowTests.call
    events = ApiFlowTests.events

    def test_send_bonuses_to_friend_with_fee(self):
        self.call(CUSTOMER, "GET", "/api/me")
        self.call(CUSTOMER2, "GET", "/api/me")
        self.db.upsert_user(CUSTOMER2.id, "boris_tg", "Борис", None)
        self.call(ADMIN, "POST", f"/api/admin/customers/{CUSTOMER.id}/bonus", {"amount": 120})
        with self.assertRaises(ApiError) as ctx:  # нет такого логина
            self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "@nobody_here", "amount": 50})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ApiError):  # самому себе
            self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "@anna_dev", "amount": 50})
        with self.assertRaises(ApiError):  # меньше минимума
            self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "@boris_tg", "amount": 9})
        with self.assertRaises(ApiError) as ctx:  # больше остатка
            self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "@boris_tg", "amount": 121})
        self.assertEqual(ctx.exception.status, 409)
        pv = self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "BORIS_tg", "amount": 15, "preview": True})
        self.assertEqual((pv["friend"], pv["sent"], pv["fee"], pv["received"]), ("Борис", 15, 2, 13))  # 10 % вверх
        self.assertEqual(self.db.bonus_balance(CUSTOMER.id), 120)  # предпросмотр ничего не списал
        r = self.call(CUSTOMER, "POST", "/api/bonus/transfer", {"username": "@boris_tg", "amount": 100})
        self.assertEqual((r["sent"], r["fee"], r["received"], r["balance"]), (100, 10, 90, 20))
        self.assertEqual((self.db.bonus_balance(CUSTOMER.id), self.db.bonus_balance(CUSTOMER2.id)), (20, 90))
        mine = self.call(CUSTOMER, "GET", "/api/bonus")["history"][0]
        self.assertEqual((mine["kind"], mine["amount"], mine["fee"], mine["who"]), ("transfer_out", -100, 10, "Борис (@boris_tg)"))
        his = self.call(CUSTOMER2, "GET", "/api/bonus")["history"][0]
        self.assertEqual((his["kind"], his["amount"]), ("transfer_in", 90))
        self.assertIn("Анна", his["who"])
        gift = [e for e in self.bridge.events if e["event"] == "bonus_gift"][-1]
        self.assertEqual(gift["recipient_id"], CUSTOMER2.id)
        self.assertIn("90 бонусов", gift["text"])
        self.assertIn("комиссия 10", self.bridge.events[-1]["text"])

    def test_old_bonus_table_is_migrated(self):
        import sqlite3 as _sqlite3
        from db import Database
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "old.sqlite3"
        with _sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE customer_bonuses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    order_id INTEGER, amount INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'spend', 'refund', 'manual')),
                    reason TEXT, order_total INTEGER, percent REAL, created_at TEXT NOT NULL);
                INSERT INTO customer_bonuses (user_id, amount, kind, created_at) VALUES (1, 300, 'manual', '2026-10-20');
            """)
        db = Database(path)
        db.initialize()
        db.upsert_user(1, "a_user", "А", None)
        db.upsert_user(2, "b_user", "Б", None)
        self.assertEqual(db.bonus_balance(1), 300)
        self.assertEqual(db.transfer_bonus(1, 2, 100)["received"], 90)


class PartnerPointsMigrationTests(unittest.TestCase):
    def test_old_partner_points_table_gets_spend_kind(self):
        import sqlite3 as _sqlite3
        from db import Database
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "old.sqlite3"
        with _sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE partner_points (id INTEGER PRIMARY KEY AUTOINCREMENT, partner_id INTEGER NOT NULL,
                    order_id INTEGER, referee_id INTEGER, points INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'writeoff')), reason TEXT, order_total INTEGER,
                    created_at TEXT NOT NULL);
                INSERT INTO partner_points (partner_id, order_id, points, kind, created_at) VALUES (7, 1, 900, 'accrual', '2026-10-20');
            """)
        db = Database(path)
        db.initialize()
        self.assertEqual(db.partner_points_balance(7), 900)
        with db.connection() as conn:
            conn.execute("INSERT INTO partner_points (partner_id, order_id, points, kind, created_at) VALUES (7, 2, -100, 'spend', 'x')")
        self.assertEqual(db.partner_points_balance(7), 800)


class BalloonRewardTests(unittest.TestCase):
    def test_balloon_detection_and_defaults(self):
        from db import default_partner_reward, is_balloon, partner_reward_for
        cases = {"Баллон 2 л": 100, "Баллон 5 литров": 150, "Баллон 10л": 200, "Баллон 2л.": 100, "Газ 5 л": 0,
                 "Баллон 8 л": 0, "Баллон 2,5 л": 0, "Шарики (50 шт.)": 0, "Насадка-глушитель": 0,
                 "Подставка для баллона": 0, "Подставка для баллона 5 л": 0, "Баллоны 5 л (набор)": 0}
        self.assertEqual({n: default_partner_reward(n) for n in cases}, cases)
        self.assertEqual([is_balloon(n) for n in ("Баллон 8 л", "БАЛЛОН 5 л", "Шарики (50 шт.)", "Лента 5 м",
                                                   "Подставка для баллона", "Баллоны", "Газ 5 л")],
                         [True, True, False, False, False, False, False])
        self.assertEqual([partner_reward_for("Баллон 5 л", None), partner_reward_for("Баллон 5 л", 175),
                          partner_reward_for("Баллон 5 л", 0), partner_reward_for("Шарики", 500)], [150, 175, 0, 0])


from db import Database  # noqa: E402


class CourierBalloonRatesTests(unittest.TestCase):
    """30.10.2026: первый баллон в заказе — одна ставка, каждый следующий — другая."""

    def test_balloons_pay(self):
        pay = Database.balloons_pay
        rates = {"balloon": 1500, "balloon_next": 1000}
        self.assertEqual([pay(rates, n) for n in (0, 1, 2, 3)], [0, 1500, 2500, 3500])

    def test_next_rate_defaults_to_first(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.sqlite3"))
            db.set_courier_rates(1500, 1000)
            self.assertEqual(db.courier_rates()["balloon_next"], 1500)
            self.assertEqual(db.set_courier_rates(1500, 1000, None, 1000)["balloon_next"], 1000)
