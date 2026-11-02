"""Проверки веб-сервера (aiohttp): сжатие статики, ETag, фоновые уведомления.

Запускаются, только если установлен aiohttp (на сервере он есть всегда)."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DANGER_PASSWORD", "AdminGas")  # test-only; production must configure a private secret

try:
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover
    TestClient = None

import dev_server  # noqa: E402
from webapp.auth import sign_init_data  # noqa: E402

TOKEN = "123456:TEST"


def auth(user_id: int, name: str) -> dict[str, str]:
    data = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": name}, ensure_ascii=False),
    }
    return {"Authorization": "tma " + sign_init_data(data, TOKEN)}


@unittest.skipIf(TestClient is None, "aiohttp не установлен")
class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from webapp.server import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.bridge, self.db = dev_server.build(Path(self.tmp.name) / "s.sqlite3", reset=True)
        self.db.start_courier_shift(2002, ["center", "north", "east", "south", "west"], None)
        self.api.background = True  # как на рабочем сервере
        self.client = TestClient(TestServer(create_app(self.api, self.bridge, TOKEN)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.api.flush_notifications()
        await self.client.close()
        self.tmp.cleanup()

    async def test_static_gzip_and_etag(self):
        raw = (ROOT / "webapp" / "static" / "app.js").read_bytes()
        r = await self.client.get("/static/app.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        body = await r.read()
        self.assertLess(len(body), len(raw) // 2)
        self.assertEqual(gzip.decompress(body), raw)
        etag = r.headers["ETag"]
        r2 = await self.client.get("/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(r2.status, 304)
        r3 = await self.client.get("/static/style.css", headers={"Accept-Encoding": "identity"})
        self.assertEqual(await r3.read(), (ROOT / "webapp" / "static" / "style.css").read_bytes())
        self.assertTrue(r3.headers["Content-Type"].startswith("text/css"))
        index = await (await self.client.get("/")).text()
        self.assertIn("/static/app.js?v=", index)

    async def test_cache_headers(self):
        import re as _re
        index = await (await self.client.get("/")).text()
        v = _re.search(r"/static/app\.js\?v=(\w+)", index).group(1)
        r = await self.client.get(f"/static/app.js?v={v}")
        self.assertIn("immutable", r.headers["Cache-Control"])  # версия в адресе — без перепроверки
        r = await self.client.get("/static/app.js?v=old")
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        r = await self.client.get("/static/app.js")
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        r = await self.client.get("/static/index.html")
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        img = next((ROOT / "webapp" / "static" / "img").rglob("*.webp"), None)
        if img:
            rel = img.relative_to(ROOT / "webapp" / "static").as_posix()
            r = await self.client.get(f"/static/{rel}")
            self.assertEqual((r.status, r.headers["Cache-Control"]), (200, "public, max-age=21600"))
        self.assertEqual(await (await self.client.get("/")).text(), index)  # из кеша — та же страница
        self.assertIn('media="print" onload', index)  # шрифт Google не задерживает показ

    async def test_buttons_do_not_wait_for_telegram(self):
        sent: list[str] = []

        async def slow(name, order_id, *_):
            await asyncio.sleep(0.6)  # «медленный Telegram»
            sent.append(f"{name}:{order_id}")

        self.bridge.dispatch_new_order = lambda oid: slow("dispatch", oid)
        self.bridge.admin_assigned = lambda oid, prev: slow("took", oid)
        self.api.db.start_courier_shift(2002, [], {p["id"]: 999 for p in self.api.db.stock_catalog()})
        anna, petr = auth(1001, "Анна"), auth(2002, "Пётр")
        await self.client.post("/api/cart", json={"product_id": 1, "variant": "new", "quantity": 1}, headers=anna)
        t = time.perf_counter()
        r = await self.client.post("/api/orders", headers=anna, json={
            "payment_method": "cash_on_delivery", "phone": "+79000000000", "address": "Москва, ул. Тестовая, д. 1, кв. 2"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertLess(time.perf_counter() - t, 0.4)  # ответ не ждёт рассылку
        order_id = (await r.json())["order"]["id"]
        # 28.10.2026: курьеры сами не берут — назначает администратор
        r = await self.client.post(f"/api/courier/orders/{order_id}/take", headers=petr)
        self.assertEqual(r.status, 404)  # «Взять заказ» больше нет (28.10.2026)
        r = await self.client.post(f"/api/admin/orders/{order_id}/assign", headers=auth(3003, "Админ"), json={"courier_id": 1})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(sent, [])  # всё ещё рассылается в фоне
        await self.api.flush_notifications()
        # порядок по заказу сохранён: сначала рассылка, потом «взял»
        self.assertEqual(sent, [f"dispatch:{order_id}", f"took:{order_id}"])

    async def test_json_api_still_works(self):
        r = await self.client.get("/api/badges", headers=auth(1001, "Анна"))
        self.assertEqual(r.status, 200)
        self.assertIn("cart", await r.json())
        r = await self.client.get("/api/badges")
        self.assertEqual(r.status, 401)


@unittest.skipIf(TestClient is None, "aiohttp не установлен")
class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_rate_is_returned_at_once_and_refreshed_in_background(self):
        from webapp import crypto

        cache = crypto.RateCache()
        cache._rates, cache._at = {"USDT": 90.0}, time.time() - crypto.RATE_TTL - 5

        async def fake_refresh():
            await asyncio.sleep(1.0)
            cache._rates, cache._at = {"USDT": 95.0}, time.time()

        cache._refresh_once = fake_refresh
        t = time.perf_counter()
        self.assertEqual(await cache.get(["USDT"]), {"USDT": 90.0})  # без ожидания CoinGecko
        self.assertLess(time.perf_counter() - t, 0.5)  # не ждали «CoinGecko» (1 с)
        await cache._refresh
        self.assertEqual(await cache.get(["USDT"]), {"USDT": 95.0})
        self.assertEqual(crypto.RATE_TTL, 3600)  # раз в час

    async def test_media_disk_cache(self):
        from webapp.bridge import TelegramBridge

        with tempfile.TemporaryDirectory() as tmp:
            bridge = TelegramBridge.__new__(TelegramBridge)
            bridge.db = type("D", (), {"path": str(Path(tmp) / "bot.sqlite3")})()
            self.assertIsNone(bridge._disk_get("file-1"))
            bridge._disk_put("file-1", b"jpegdata", "image/jpeg")
            self.assertEqual(bridge._disk_get("file-1"), (b"jpegdata", "image/jpeg"))
            self.assertTrue((Path(tmp) / "media_cache").is_dir())


if __name__ == "__main__":
    unittest.main()



@unittest.skipIf(TestClient is None, "aiohttp не установлен")
class AddressDbDownloadTests(unittest.IsolatedAsyncioTestCase):
    """Скачивание базы из Releases: токен только для GitHub, файл — по перенаправлению без токена."""

    async def test_download_swaps_database(self):
        import gzip
        import os
        from unittest import mock
        from aiohttp import web
        sys.path.insert(0, str(ROOT / "tools"))
        from build_address_db import build
        from webapp import geodb

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "fresh.sqlite"
        build(ROOT / "tools" / "sample_addresses.geojsonseq", src, "test")
        blob = gzip.compress(src.read_bytes())
        seen = []

        async def release(request):
            seen.append(("release", request.headers.get("Authorization")))
            base = f"http://127.0.0.1:{request.url.port}"
            return web.json_response({"assets": [{"name": "addresses.sqlite.gz", "url": base + "/asset/1",
                                                  "updated_at": "2026-10-21T10:00:00Z"}]})

        async def asset(request):
            seen.append(("asset", request.headers.get("Authorization")))
            raise web.HTTPFound(f"http://127.0.0.1:{request.url.port}/storage/file.gz")

        async def storage(request):
            seen.append(("storage", request.headers.get("Authorization")))
            return web.Response(body=blob)

        app = web.Application()
        app.router.add_get("/repos/artprosh/miniapp/releases/tags/address-db", release)
        app.router.add_get("/asset/1", asset)
        app.router.add_get("/storage/file.gz", storage)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        target = Path(tmp.name) / "data" / "addresses.sqlite"
        db = geodb.AddressDB(target)
        self.assertFalse(db.loaded)
        with mock.patch.object(geodb, "API_BASE", f"http://127.0.0.1:{server.port}"), \
                mock.patch.dict(os.environ, {"GEODB_GITHUB_TOKEN": "t0ken"}):
            res = await db.update_from_github()
            again = await db.update_from_github()
        self.assertTrue(res["updated"])
        self.assertEqual(res["houses"], 12)
        self.assertEqual(again, {"updated": False, "reason": "up_to_date"})
        self.assertEqual(db.suggest("лесн")[0]["value"], "Лесная улица, Москва")
        self.assertEqual(seen[0], ("release", "Bearer t0ken"))
        self.assertEqual(seen[1], ("asset", "Bearer t0ken"))
        self.assertEqual(seen[2], ("storage", None))  # токен не уходит в хранилище файлов


class AddressDbRebuildTests(unittest.IsolatedAsyncioTestCase):
    """«🛠 Обновить карты»: запуск сборки на GitHub, ожидание нового файла, скачивание."""

    async def test_rebuild_dispatches_waits_and_downloads(self):
        import gzip
        import os
        from unittest import mock
        from aiohttp import web
        sys.path.insert(0, str(ROOT / "tools"))
        from build_address_db import build
        from webapp import geodb

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "fresh.sqlite"
        build(ROOT / "tools" / "sample_addresses.geojsonseq", src, "test")
        blob = gzip.compress(src.read_bytes())
        st = {"updated_at": "2026-10-01T00:00:00Z", "dispatched": [], "deny": False, "polls": 0}

        async def release(request):
            if st["dispatched"] and st["polls"] >= 1:
                st["updated_at"] = "2026-10-22T00:00:00Z"  # сборка «закончилась»
            st["polls"] += 1
            base = f"http://127.0.0.1:{request.url.port}"
            return web.json_response({"assets": [{"name": "addresses.sqlite.gz", "url": base + "/asset/1",
                                                  "updated_at": st["updated_at"]}]})

        async def dispatch(request):
            if st["deny"]:
                return web.json_response({"message": "Resource not accessible"}, status=403)
            st["dispatched"].append((await request.json(), request.headers.get("Authorization")))
            return web.Response(status=204)

        async def asset(request):
            return web.Response(body=blob)

        app = web.Application()
        app.router.add_get("/repos/artprosh/miniapp/releases/tags/address-db", release)
        app.router.add_post("/repos/artprosh/miniapp/actions/workflows/address-db.yml/dispatches", dispatch)
        app.router.add_get("/asset/1", asset)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        db = geodb.AddressDB(Path(tmp.name) / "addresses.sqlite")
        with mock.patch.object(geodb, "API_BASE", f"http://127.0.0.1:{server.port}"), \
                mock.patch.object(geodb, "REBUILD_POLL", 0.05), \
                mock.patch.dict(os.environ, {"GEODB_GITHUB_TOKEN": "t0ken"}):
            st["deny"] = True
            with self.assertRaises(RuntimeError) as ctx:
                await db.start_rebuild()
            self.assertIn("Actions: Read and write", str(ctx.exception))
            st["deny"] = False
            res = await db.start_rebuild()
            self.assertEqual(res["rebuild"]["state"], "building")
            self.assertTrue((await db.start_rebuild())["already"])  # вторая кнопка не запускает ещё одну сборку
            await asyncio.wait_for(db._watch_task, 5)
        self.assertEqual(st["dispatched"], [({"ref": "main"}, "Bearer t0ken")])
        self.assertEqual(db.rebuild["state"], "done")
        self.assertTrue(db.loaded)
        self.assertEqual(db.meta["asset_updated_at"], "2026-10-22T00:00:00Z")

    def test_no_schedule_in_workflow(self):
        text = (ROOT / ".github" / "workflows" / "address-db.yml").read_text(encoding="utf-8")
        self.assertNotIn("schedule:", text)
        self.assertIn("workflow_dispatch:", text)

