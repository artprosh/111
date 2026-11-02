"""aiohttp-маршруты диспетчерской карты для ПК (/dispatch). Логика входа — dispatch.py.

  GET  /dispatch                 — страница карты (static/dispatch.html)
  GET  /dispatch/login/<токен>   — «Войти в карту?» (сама ссылка вход НЕ выполняет)
  POST /dispatch/login/<токен>   — вход: ссылка гасится, браузер получает cookie
  GET  /dispatch/api/session     — кто вошёл и до какого времени действует вход
  POST /dispatch/api/logout      — выйти (этот браузер)
  POST /dispatch/api/logout-all  — выйти везде (все входы этого админа)
  POST /dispatch/api/assign      — отдать заказы курьеру (переназначение — с флагом reassign)
  GET  /dispatch/api/admin/...   — данные карты: те же ответы, что у админки Mini App
  GET  /dispatch/vendor/ol.js|ol.css — OpenLayers с нашего сервера (vendor_ol.py)

POST-запросы API принимаются только с заголовком X-Dispatch: 1 и (если браузер
прислал Origin) с того же адреса — защита от подделки запросов с чужих сайтов;
cookie к тому же SameSite=Strict.
"""
from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path
from typing import Any

from aiohttp import web

from .auth import TelegramUser
from .core import Api, ApiError
from .dispatch import COOKIE_NAME, COOKIE_PATH, SESSION_TTL, pc_allowed
from .vendor_ol import TYPES as OL_TYPES
from .vendor_ol import OlVendor

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Не «no-referrer»: тайл-серверы OpenStreetMap требуют заголовок Referer.
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
BUSY = ("accepted", "in_transit")


def _json(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str), status=status,
        content_type="application/json", headers={"Cache-Control": "no-store"},
    )


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote) or ""


def _is_local(request: web.Request) -> bool:
    return request.host.split(":")[0] in ("localhost", "127.0.0.1") or request.host.startswith("[::1]")


def _set_cookie(request: web.Request, response: web.StreamResponse, token: str, max_age: int) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=max(0, int(max_age)), path=COOKIE_PATH, httponly=True,
        secure=not _is_local(request), samesite="Strict",
    )


def _clear_cookie(request: web.Request, response: web.StreamResponse) -> None:
    response.del_cookie(COOKIE_NAME, path=COOKIE_PATH)


def _page(title: str, body: str, status: int = 200) -> web.Response:
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{escape(title)}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f172a;color:#e2e8f0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.box{{max-width:460px;margin:24px;padding:28px;border-radius:18px;background:#1e293b;box-shadow:0 10px 40px #0006}}
h1{{font-size:22px;margin:0 0 12px}} p{{margin:8px 0;color:#cbd5e1}} .warn{{color:#fca5a5}}
button,a.btn{{display:inline-block;margin-top:16px;padding:12px 22px;border:0;border-radius:12px;background:#f97316;color:#fff;font:600 16px system-ui;cursor:pointer;text-decoration:none}}
</style></head><body><div class="box">{body}</div></body></html>"""
    return web.Response(text=html, status=status, content_type="text/html", headers=PAGE_HEADERS)


LINK_ERRORS = {
    "used": ("Ссылка уже использована", "Эту ссылку уже открыли. Повторно она не работает — так никто не войдёт по пересланной ссылке."),
    "revoked": ("Ссылка отменена", "Вместо неё выдана новая ссылка, или был нажат «Выйти везде»."),
    "unknown": ("Ссылка не найдена", "Проверьте, что ссылка скопирована целиком."),
}
NEW_LINK_HINT = "<p>Новая ссылка: Mini App → Админка → «🗺 Карта» → «🖥 Карта для ПК».</p>"


def add_dispatch_routes(app: web.Application, api: Api) -> None:
    auth = api.dispatch
    vendor = OlVendor(Path(str(api.db.path)).resolve().parent / "vendor")
    app["ol_vendor"] = vendor

    def remember_origin(request: web.Request) -> None:
        # Запасной вариант, если WEBAPP_URL не задан: адрес, по которому открыли сервер.
        if not getattr(api, "public_url", None) and not _is_local(request):
            proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip() or "https"
            api.public_url = f"{proto}://{request.host}"

    async def origin_hook(request: web.Request, response: web.StreamResponse) -> None:
        if request.path.startswith(("/api/", "/dispatch")):
            remember_origin(request)

    app.on_response_prepare.append(origin_hook)

    async def page(request: web.Request) -> web.Response:
        vendor.kick()  # OpenLayers — скачать на сервер заранее, если его ещё нет
        html = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers=PAGE_HEADERS)

    async def login_page(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        state = auth.link_state(token)
        if state != "ok":
            title, text = LINK_ERRORS[state]
            return _page(title, f"<h1>⛔ {escape(title)}</h1><p>{escape(text)}</p>{NEW_LINK_HINT}", 410)
        hours = SESSION_TTL // 3600
        return _page("Вход в карту для ПК", f"""<h1>🖥 Диспетчерская карта</h1>
<p>Вход будет действовать <b>{hours} ч</b> в этом браузере.</p>
<p class="warn">Ссылка одноразовая: после нажатия «Войти» она перестанет работать — в том числе у того, кому её переслали.</p>
<p>Открывайте на компьютере, с которого будете работать.</p>
<form method="post"><button type="submit">Войти</button></form>""")

    async def login(request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin != "null" and origin.split("://", 1)[-1] != request.host:
            return _page("Ошибка входа", "<h1>⛔ Вход отклонён</h1><p>Запрос пришёл не с этой страницы.</p>", 403)
        result = auth.redeem(request.match_info["token"], request.headers.get("User-Agent"), _client_ip(request))
        if isinstance(result, str):
            title, text = LINK_ERRORS[result]
            return _page(title, f"<h1>⛔ {escape(title)}</h1><p>{escape(text)}</p>{NEW_LINK_HINT}", 410)
        token, session = result
        if not api.is_admin(session["admin_id"]):
            auth.revoke_session(session["id"], session["admin_id"])
            return _page("Нет доступа", "<h1>⛔ Нет доступа</h1><p>Карта доступна только администраторам.</p>", 403)
        await api._safe(api.bridge.dispatch_login(session["admin_id"], session), "уведомление о входе на ПК")
        response = web.HTTPSeeOther("/dispatch")
        response.headers.update(PAGE_HEADERS)
        _set_cookie(request, response, token, SESSION_TTL)
        raise response

    def current(request: web.Request) -> tuple[dict[str, Any], TelegramUser]:
        session = auth.session(request.cookies.get(COOKIE_NAME))
        if not session:
            raise ApiError(401, "Вход истёк или не выполнен — запросите новую ссылку в Mini App.")
        if not api.is_admin(session["admin_id"]):
            raise ApiError(403, "Карта доступна только администраторам.")
        row = api.db.get_user(session["admin_id"])
        user = TelegramUser(
            session["admin_id"],
            (row["first_name"] if row and row["first_name"] else "Админ"),
            row["last_name"] if row else None,
            row["username"] if row else None,
        )
        return session, user

    def check_post(request: web.Request) -> None:
        if request.headers.get("X-Dispatch") != "1":
            raise ApiError(403, "Запрос отклонён.")
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            raise ApiError(403, "Запрос отклонён: чужой адрес.")

    async def read_body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Некорректный JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "Некорректный JSON.")
        return body

    def guarded(handler):
        async def wrapper(request: web.Request) -> web.Response:
            try:
                if request.method != "GET":
                    check_post(request)
                session, user = current(request)
                return await handler(request, session, user)
            except ApiError as exc:
                response = _json({"error": exc.message, "status": exc.status}, exc.status)
                if exc.status == 401:
                    _clear_cookie(request, response)
                return response
            except Exception:
                logger.exception("Карта для ПК: ошибка в %s %s", request.method, request.path)
                return _json({"error": "Внутренняя ошибка. Попробуйте ещё раз."}, 500)
        return wrapper

    async def session_info(request, session, user) -> web.Response:
        import time

        return _json({
            "admin": {"id": user.id, "name": user.display_name},
            "session": session,
            "server_ts": int(time.time()),
            "ttl_hours": SESSION_TTL // 3600,
        })

    async def logout(request, session, user) -> web.Response:
        auth.revoke_session(session["id"], session["admin_id"])
        response = _json({"ok": True})
        _clear_cookie(request, response)
        return response

    async def logout_all(request, session, user) -> web.Response:
        count = auth.revoke_all(session["admin_id"])
        response = _json({"ok": True, "revoked": count})
        _clear_cookie(request, response)
        return response

    async def proxy(request, session, user) -> web.Response:
        path = "/api/admin/" + request.match_info["tail"]
        if not pc_allowed(request.method, path):
            raise ApiError(404, "Не найдено.")
        body = await read_body(request) if request.method == "POST" else {}
        result = await api.handle(request.method, path, user, body, dict(request.query))
        return _json(result)

    async def assign(request, session, user) -> web.Response:
        """Отдать заказы курьеру. Заказ, который уже у ДРУГОГО курьера, переназначается
        только при reassign=true (на странице — после подтверждения): сначала он
        снимается с прежнего курьера (тот получает «Заказ снят с вас»), потом
        назначается новому — так же, как в админке Mini App."""
        body = await read_body(request)
        try:
            courier_id = int(body.get("courier_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Выберите курьера.")
        raw_ids = body.get("order_ids")
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 50:
            raise ApiError(400, "Выберите заказы (до 50).")
        try:
            ids = list(dict.fromkeys(int(x) for x in raw_ids))
        except (TypeError, ValueError):
            raise ApiError(400, "Неверный номер заказа.")
        reassign = body.get("reassign") is True
        # 28.10.2026: не ответил на опрос / не на смене / не хватает товаров на все выбранные — ничего не снимаем
        api._check_not_silent(courier_id, ids)
        already, busy, released, failed, to_assign = [], [], [], [], []
        for order_id in ids:
            order = api.db.get_order(order_id)
            if not order:
                failed.append(order_id)
            elif order["courier_id"] is not None and int(order["courier_id"]) == courier_id:
                already.append(order_id)
            elif order["status"] in BUSY and order["courier_id"] is not None:
                if not reassign:
                    busy.append(order_id)
                    continue
                try:
                    await api.handle("POST", f"/api/admin/orders/{order_id}/status", user, {"status": "new"}, {})
                    released.append(order_id)
                    to_assign.append(order_id)
                except ApiError:
                    failed.append(order_id)
            else:
                to_assign.append(order_id)
        assigned: list[int] = []
        if to_assign:
            try:
                result = await api.handle("POST", "/api/admin/orders/assign", user,
                                          {"order_ids": to_assign, "courier_id": courier_id}, {})
                assigned, failed = result["assigned"], failed + result["failed"]
            except ApiError:
                failed += to_assign
        return _json({"assigned": assigned, "failed": failed, "already": already, "busy": busy, "released": released})

    async def vendor_file(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        data = vendor.get(name)
        if data is None:
            vendor.kick()
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type=OL_TYPES[name], charset="utf-8",
                            headers={"Cache-Control": "public, max-age=604800"})

    app.router.add_get("/dispatch", page)
    app.router.add_get("/dispatch/", page)
    app.router.add_get("/dispatch/login/{token}", login_page)
    app.router.add_post("/dispatch/login/{token}", login)
    app.router.add_get("/dispatch/api/session", guarded(session_info))
    app.router.add_post("/dispatch/api/logout", guarded(logout))
    app.router.add_post("/dispatch/api/logout-all", guarded(logout_all))
    app.router.add_post("/dispatch/api/assign", guarded(assign))
    app.router.add_route("*", "/dispatch/api/admin/{tail:.+}", guarded(proxy))
    app.router.add_get("/dispatch/vendor/{name:ol\\.js|ol\\.css}", vendor_file)
