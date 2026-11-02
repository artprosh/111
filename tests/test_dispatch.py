"""Диспетчерская карта для ПК (/dispatch): одноразовая ссылка, сессия на сутки,
доступ только админам, данные карты, назначение и переназначение заказов."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover
    TestClient = None

import dev_server  # noqa: E402
from webapp.core import ApiError  # noqa: E402
from webapp.dispatch import SESSION_TTL, DispatchAuth, browser_label, pc_allowed  # noqa: E402
from webapp.vendor_ol import check_integrity, extract  # noqa: E402

ADMIN = dev_server.ADMIN
CUSTOMER = dev_server.DEV_USERS[0][0]


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def call(api, user, method, path, body=None):
    return asyncio.run(api.handle(method, path, user, body or {}, {}))


def cash_order(api, user=CUSTOMER):
    call(api, user, "POST", "/api/cart", {"product_id": 1, "variant": "new", "quantity": 1})
    return call(api, user, "POST", "/api/orders", {
        "payment_method": "cash_on_delivery", "phone": "+7 900 000-00-00", "address": "Москва, ул. Тестовая, д. 1",
    })["order"]


class DispatchAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "t.sqlite3", reset=True)
        self.clock = Clock()
        self.auth = DispatchAuth(self.db, now=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_link_is_single_use(self):
        token = self.auth.issue_link(ADMIN.id)
        self.assertEqual(self.auth.link_state(token), "ok")
        sid, session = self.auth.redeem(token, "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", "1.2.3.4")
        self.assertEqual(session["admin_id"], ADMIN.id)
        self.assertEqual(session["browser"], "Chrome · Windows")
        self.assertEqual(self.auth.link_state(token), "used")
        self.assertEqual(self.auth.redeem(token, None, None), "used")  # второй раз — нет (друг по пересланной)
        self.assertEqual(self.auth.redeem("нет-такой", None, None), "unknown")
        self.assertIsNotNone(self.auth.session(sid))

    def test_link_does_not_expire_by_time_but_new_link_revokes_old(self):
        old = self.auth.issue_link(ADMIN.id)
        self.clock.t += 30 * 24 * 3600  # месяц — ссылка всё ещё рабочая
        self.assertEqual(self.auth.link_state(old), "ok")
        new = self.auth.issue_link(ADMIN.id)
        self.assertEqual(self.auth.link_state(old), "revoked")
        self.assertEqual(self.auth.redeem(old, None, None), "revoked")
        self.assertIsInstance(self.auth.redeem(new, None, None), tuple)

    def test_session_lasts_24h_from_opening(self):
        self.assertEqual(SESSION_TTL, 24 * 3600)
        token = self.auth.issue_link(ADMIN.id)
        self.clock.t += 5 * 3600  # открыли через 5 часов после выдачи — сутки считаются с открытия
        sid, session = self.auth.redeem(token, None, None)
        self.assertEqual(session["expires_ts"], int(self.clock.t) + SESSION_TTL)
        self.clock.t += SESSION_TTL - 1
        self.assertIsNotNone(self.auth.session(sid))
        self.clock.t += 1
        self.assertIsNone(self.auth.session(sid))
        self.assertEqual(self.auth.list_sessions(ADMIN.id), [])

    def test_revoke_and_logout_everywhere(self):
        a = self.auth.redeem(self.auth.issue_link(ADMIN.id), None, None)
        b = self.auth.redeem(self.auth.issue_link(ADMIN.id), None, None)
        other = self.auth.redeem(self.auth.issue_link(4004), None, None)
        self.assertFalse(self.auth.revoke_session(a[1]["id"], 4004))  # чужой вход не отозвать
        self.assertTrue(self.auth.revoke_session(a[1]["id"], ADMIN.id))
        self.assertIsNone(self.auth.session(a[0]))
        pending = self.auth.issue_link(ADMIN.id)
        self.assertEqual(self.auth.revoke_all(ADMIN.id), 1)
        self.assertIsNone(self.auth.session(b[0]))
        self.assertEqual(self.auth.link_state(pending), "revoked")  # неоткрытая ссылка тоже
        self.assertIsNotNone(self.auth.session(other[0]))  # входы другого админа не тронуты

    def test_tokens_are_stored_hashed(self):
        token = self.auth.issue_link(ADMIN.id)
        sid, _ = self.auth.redeem(token, None, None)
        dump = "\n".join(self.db.connection().__enter__().iterdump())
        self.assertNotIn(token, dump)
        self.assertNotIn(sid, dump)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), dump)

    def test_browser_label_and_allowed_paths(self):
        self.assertEqual(browser_label("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit Safari/605"), "Safari · macOS")
        self.assertEqual(browser_label(None), "Браузер")
        self.assertTrue(pc_allowed("GET", "/api/admin/orders"))
        self.assertTrue(pc_allowed("POST", "/api/admin/orders/assign"))
        self.assertFalse(pc_allowed("POST", "/api/admin/wipe-orders"))
        self.assertFalse(pc_allowed("GET", "/api/admin/settings"))


async def acash_order(api, user=CUSTOMER):
    await api.handle("POST", "/api/cart", user, {"product_id": 1, "variant": "new", "quantity": 1}, {})
    return (await api.handle("POST", "/api/orders", user, {
        "payment_method": "cash_on_delivery", "phone": "+7 900 000-00-00", "address": "Москва, ул. Тестовая, д. 1",
    }, {}))["order"]


class MiniAppDispatchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "t.sqlite3", reset=True)
        self.api.public_url = "https://example.test"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_admin_gets_link(self):
        call(self.api, CUSTOMER, "GET", "/api/me")
        with self.assertRaises(ApiError) as ctx:
            call(self.api, CUSTOMER, "POST", "/api/admin/dispatch/link")
        self.assertEqual(ctx.exception.status, 403)
        r = call(self.api, ADMIN, "POST", "/api/admin/dispatch/link")
        self.assertTrue(r["url"].startswith("https://example.test/dispatch/login/"))
        self.assertTrue(r["sent"])
        self.assertEqual(r["hours"], 24)
        sent = [e for e in self.bridge.events if e["event"] == "dispatch_link"]
        self.assertEqual(sent[-1]["telegram_id"], ADMIN.id)
        self.assertEqual(sent[-1]["url"], r["url"])

    def test_sessions_list_and_revoke(self):
        token = call(self.api, ADMIN, "POST", "/api/admin/dispatch/link")["url"].rsplit("/", 1)[1]
        self.assertIsNotNone(call(self.api, ADMIN, "GET", "/api/admin/dispatch/sessions")["pending_link"])
        _, session = self.api.dispatch.redeem(token, "Firefox/130", "5.6.7.8")
        listed = call(self.api, ADMIN, "GET", "/api/admin/dispatch/sessions")
        self.assertEqual([s["id"] for s in listed["sessions"]], [session["id"]])
        self.assertIsNone(listed["pending_link"])
        after = call(self.api, ADMIN, "POST", f"/api/admin/dispatch/sessions/{session['id']}/revoke")
        self.assertEqual(after["sessions"], [])
        with self.assertRaises(ApiError):
            call(self.api, ADMIN, "POST", f"/api/admin/dispatch/sessions/{session['id']}/revoke")

    def test_no_public_url_gives_clear_error(self):
        self.api.public_url = None
        with self.assertRaises(ApiError) as ctx:
            call(self.api, ADMIN, "POST", "/api/admin/dispatch/link")
        self.assertEqual(ctx.exception.status, 503)


class VendorTests(unittest.TestCase):
    def test_integrity_and_extract(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in (("package/dist/ol.js", b"window.ol={};"), ("package/ol.css", b".ol{}"),
                               ("package/LICENSE.md", b"BSD"), ("package/other.js", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        raw = buf.getvalue()
        sri = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()
        self.assertTrue(check_integrity(raw, sri))
        self.assertFalse(check_integrity(raw + b"!", sri))
        self.assertFalse(check_integrity(raw, "sha1-abc"))
        self.assertEqual(extract(raw), {"ol.js": b"window.ol={};", "ol.css": b".ol{}", "LICENSE": b"BSD"})


@unittest.skipIf(TestClient is None, "aiohttp не установлен")
class DispatchServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from webapp.server import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "s.sqlite3", reset=True)
        stock = {p["id"]: 999 for p in self.db.stock_catalog()}
        self.db.start_courier_shift(2002, ["center"], stock)
        with self.db.connection() as conn:  # Иван (без Telegram ID) тоже на смене — назначать можно только курьеру на смене
            conn.execute("UPDATE couriers SET shift_state = 'on', shift_started_at = ?, shift_confirmed_at = ?, shift_stock = ? WHERE id = 2",
                         (self.db.now(), self.db.now(), self.db.dump_stock(stock)))
        self.api.public_url = "https://example.test"
        self.client = TestClient(TestServer(create_app(self.api, self.bridge, "123:T")))
        await self.client.start_server()
        # В тестах не ходим в сеть за OpenLayers (как будто недавняя попытка не удалась).
        self.client.app["ol_vendor"]._failed_at = __import__("time").time()

    async def asyncTearDown(self):
        await self.api.flush_notifications()
        await self.client.close()
        self.tmp.cleanup()

    async def login(self, admin_id=None) -> str:
        token = self.api.dispatch.issue_link(admin_id or ADMIN.id)
        page = await self.client.get(f"/dispatch/login/{token}")
        self.assertEqual(page.status, 200)
        self.assertIn("Войти", await page.text())
        self.assertEqual(self.api.dispatch.link_state(token), "ok")  # просмотр страницы ссылку не гасит
        r = await self.client.post(f"/dispatch/login/{token}", allow_redirects=False)
        return token, r

    async def test_login_cookie_and_single_use(self):
        token, r = await self.login()
        self.assertEqual(r.status, 303)
        self.assertEqual(r.headers["Location"], "/dispatch")
        cookie = r.headers["Set-Cookie"]
        for part in ("dsp_sid=", "HttpOnly", "Path=/dispatch", "SameSite=Strict", f"Max-Age={SESSION_TTL}"):
            self.assertIn(part, cookie)
        s = await self.client.get("/dispatch/api/session")
        self.assertEqual(s.status, 200)
        data = await s.json()
        self.assertEqual(data["admin"]["id"], ADMIN.id)
        self.assertEqual(data["ttl_hours"], 24)
        again = await self.client.post(f"/dispatch/login/{token}", allow_redirects=False)
        self.assertEqual(again.status, 410)
        self.assertIn("уже использована", await again.text())
        await self.api.flush_notifications()
        self.assertIn("dispatch_login", [e["event"] for e in self.bridge.events])

    async def test_without_cookie_401_and_page_is_served(self):
        self.assertEqual((await self.client.get("/dispatch/api/session")).status, 401)
        self.assertEqual((await self.client.get("/dispatch/api/admin/orders")).status, 401)
        page = await self.client.get("/dispatch")
        self.assertEqual(page.status, 200)
        self.assertIn("dispatch.js", await page.text())
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        self.assertNotEqual(page.headers["Referrer-Policy"], "no-referrer")  # тайлам OSM нужен Referer

    async def test_not_admin_is_refused(self):
        _, r = await self.login(admin_id=CUSTOMER.id)
        self.assertEqual(r.status, 403)
        self.assertNotIn("dsp_sid", r.headers.get("Set-Cookie", ""))
        # Админа убрали из ADMIN_ID после входа — его вход сразу перестаёт работать.
        _, r = await self.login()
        self.assertEqual(r.status, 303)
        self.api.admin_ids = frozenset()
        self.assertEqual((await self.client.get("/dispatch/api/session")).status, 403)

    async def test_data_and_only_allowed_admin_calls(self):
        await self.login()
        for path in ("orders", "map", "couriers"):
            r = await self.client.get(f"/dispatch/api/admin/{path}")
            self.assertEqual(r.status, 200, path)
        self.assertIn("couriers", await (await self.client.get("/dispatch/api/admin/map")).json())
        self.assertEqual((await self.client.get("/dispatch/api/admin/settings")).status, 404)
        r = await self.client.post("/dispatch/api/admin/wipe-orders", json={}, headers={"X-Dispatch": "1"})
        self.assertEqual(r.status, 404)

    async def test_post_needs_header_and_same_origin(self):
        await self.login()
        order = await acash_order(self.api)
        body = {"order_ids": [order["id"]], "courier_id": 1}
        self.assertEqual((await self.client.post("/dispatch/api/assign", json=body)).status, 403)
        r = await self.client.post("/dispatch/api/assign", json=body,
                                   headers={"X-Dispatch": "1", "Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertIsNone(self.db.get_order(order["id"])["courier_id"])

    async def test_assign_and_reassign(self):
        await self.login()
        free, busy = await acash_order(self.api), await acash_order(self.api)
        await self.api.handle("POST", "/api/admin/orders/assign", ADMIN, {"order_ids": [busy["id"]], "courier_id": 1}, {})
        ivan = 2  # второй курьер из демо-данных (dev_server.seed)
        h = {"X-Dispatch": "1"}
        r = await (await self.client.post("/dispatch/api/assign", json={"order_ids": [free["id"], busy["id"]], "courier_id": ivan}, headers=h)).json()
        self.assertEqual(r["assigned"], [free["id"]])
        self.assertEqual(r["busy"], [busy["id"]])  # без подтверждения чужой заказ не трогаем
        self.assertEqual(int(self.db.get_order(busy["id"])["courier_id"]), 1)
        r = await (await self.client.post("/dispatch/api/assign", json={"order_ids": [busy["id"], free["id"]], "courier_id": ivan, "reassign": True}, headers=h)).json()
        self.assertEqual(r["released"], [busy["id"]])
        self.assertEqual(r["assigned"], [busy["id"]])
        self.assertEqual(r["already"], [free["id"]])
        self.assertEqual(int(self.db.get_order(busy["id"])["courier_id"]), ivan)
        await self.api.flush_notifications()
        events = [(e["event"], e.get("order_id")) for e in self.bridge.events]
        self.assertIn(("admin_status", busy["id"]), events)  # прежний курьер: «Заказ снят с вас»
        self.assertIn(("admin_assigned", busy["id"]), events)

    async def test_logout_and_logout_everywhere(self):
        await self.login()
        r = await self.client.post("/dispatch/api/logout", headers={"X-Dispatch": "1"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await self.client.get("/dispatch/api/session")).status, 401)
        await self.login()
        self.assertEqual((await self.client.post("/dispatch/api/logout-all", headers={"X-Dispatch": "1"})).status, 200)
        self.assertEqual((await self.client.get("/dispatch/api/session")).status, 401)

    async def test_session_expires(self):
        await self.login()
        clock = Clock(__import__("time").time() + SESSION_TTL + 1)
        self.api.dispatch.now = clock
        r = await self.client.get("/dispatch/api/session")
        self.assertEqual(r.status, 401)

    async def test_vendor_files_from_disk(self):
        vendor = self.client.app["ol_vendor"]
        self.assertEqual((await self.client.get("/dispatch/vendor/ol.js")).status, 404)
        vendor.folder.mkdir(parents=True)
        (vendor.folder / "ol.js").write_bytes(b"window.ol={Map:1};")
        (vendor.folder / "ol.css").write_bytes(b".ol{}")
        r = await self.client.get("/dispatch/vendor/ol.js")
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.read(), b"window.ol={Map:1};")
        self.assertEqual((await self.client.get("/dispatch/vendor/other.js")).status, 404)


if __name__ == "__main__":
    unittest.main()
