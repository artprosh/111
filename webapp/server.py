"""aiohttp-сервер Mini App (рабочий режим). Запускается из main.py в том же
процессе, что и long polling бота, и работает с той же базой.

Маршруты:
  GET  /               — index.html
  GET  /static/...     — app.js, style.css
  GET  /api/media/...  — картинки из Telegram (фото товаров, шапка, QR оплаты)
  *    /api/...        — JSON API (webapp/core.py), заголовок
                         `Authorization: tma <initData>` обязателен
  GET  /healthz        — проверка, что сервер жив, и какой коммит запущен
  *    /dispatch...     — диспетчерская карта для ПК (webapp/dispatch_web.py)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from pathlib import Path

from aiohttp import web

from .auth import InitDataError, validate_init_data
from .core import XLSX_MIME, Api, ApiError
from .version import assets_hash, render_index, version_text

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 12 * 1024 * 1024  # скриншот оплаты в base64 до ~8 МБ


def _json(data, status: int = 200) -> web.Response:
    response = web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
    if len(response.body) > 1024:
        response.enable_compression()  # gzip, если телефон его принимает (все принимают)
    return response


class _Assets:
    """app.js и style.css — из памяти, сжатые gzip один раз (134 КБ → ~35 КБ),
    с ETag: телефон перепроверяет файл и получает 304 без тела, если он не менялся."""

    TYPES = {"app.js": "application/javascript", "style.css": "text/css"}

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.cache: dict[str, tuple[float, bytes, bytes, str]] = {}

    def get(self, name: str) -> tuple[bytes, bytes, str]:
        path = self.folder / name
        mtime = path.stat().st_mtime
        hit = self.cache.get(name)
        if hit is None or hit[0] != mtime:
            raw = path.read_bytes()
            etag = '"' + hashlib.sha256(raw).hexdigest()[:16] + '"'
            hit = (mtime, raw, gzip.compress(raw, 6), etag)
            self.cache[name] = hit
        return hit[1], hit[2], hit[3]


def create_app(api: Api, bridge, bot_token: str | None, auth_service=None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)

    async def index(request: web.Request) -> web.StreamResponse:
        # app.js/style.css подключаются с ?v=<хеш содержимого>: новый код —
        # новый адрес, и Telegram не покажет старую копию из своего кеша.
        return web.Response(
            text=render_index(STATIC_DIR), content_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    async def healthz(request: web.Request) -> web.Response:
        return web.Response(text=version_text(), headers={"Cache-Control": "no-store"})

    async def media(request: web.Request) -> web.Response:
        file_id = api.media_file_id(request.match_info["kind"], request.match_info["ident"])
        if not file_id:
            raise web.HTTPNotFound()
        try:
            result = await bridge.fetch_file(file_id)
        except Exception:
            logger.exception("Mini App: не удалось скачать файл %s", file_id)
            raise web.HTTPBadGateway()
        if not result:
            raise web.HTTPNotFound()
        data, mime = result
        return web.Response(body=data, content_type=mime, headers={"Cache-Control": "private, max-age=300"})

    async def api_handler(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth_service is not None:
            token = auth[7:].strip() if auth.startswith("Bearer ") else request.cookies.get("app_session", "")
            user = auth_service.user_for_token(token)
            if user is None:
                return _json({"error": "Сеанс истёк. Войдите снова."}, 401)
        else:
            if not auth.startswith("tma ") or not bot_token:
                return _json({"error": "Откройте приложение из Telegram."}, 401)
            try:
                user = validate_init_data(auth[4:], bot_token)
            except InitDataError as exc:
                return _json({"error": f"Не удалось проверить вход: {exc}. Откройте приложение заново."}, 401)
        body = {}
        if request.method in ("POST", "PUT", "PATCH") and request.can_read_body:
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json({"error": "Некорректный JSON."}, 400)
            if not isinstance(body, dict):
                return _json({"error": "Некорректный JSON."}, 400)
        try:
            result = await api.handle(request.method, request.path, user, body, dict(request.query))
        except ApiError as exc:
            return _json({"error": exc.message, **getattr(exc, "extra", {})}, exc.status)
        except Exception:
            logger.exception("Mini App: ошибка в %s %s", request.method, request.path)
            return _json({"error": "Внутренняя ошибка. Попробуйте ещё раз."}, 500)
        return _json(result)

    async def standalone_auth(request: web.Request) -> web.Response:
        if auth_service is None:
            return _json({"error": "Этот способ входа не включён."}, 404)
        action = request.match_info["action"]
        body = {}
        if request.can_read_body:
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json({"error": "Некорректный JSON."}, 400)
            if not isinstance(body, dict):
                return _json({"error": "Некорректный JSON."}, 400)
        try:
            if action == "request-code" and request.method == "POST":
                result = await auth_service.request_code(body.get("phone"), request.remote)
                return _json(result)
            if action == "verify-code" and request.method == "POST":
                result = auth_service.verify_code(body.get("phone"), body.get("code"), body.get("name"))
                response = _json({
                    "ok": True,
                    "user": {
                        "id": result.user.id,
                        "first_name": result.user.first_name,
                        "last_name": result.user.last_name,
                    },
                })
                response.set_cookie(
                    "app_session", result.token, max_age=30 * 24 * 60 * 60,
                    httponly=True, secure=True, samesite="Strict", path="/",
                )
                return response
            if action == "logout" and request.method == "POST":
                header = request.headers.get("Authorization", "")
                if header.startswith("Bearer "):
                    auth_service.logout(header[7:].strip())
                token = request.cookies.get("app_session", "")
                auth_service.logout(token)
                response = _json({"ok": True})
                response.del_cookie("app_session", path="/", secure=True, httponly=True, samesite="Strict")
                return response
            return _json({"error": "Не найдено."}, 404)
        except Exception as exc:
            if hasattr(exc, "status") and hasattr(exc, "message"):
                return _json({"error": exc.message}, exc.status)
            logger.exception("Standalone login failed")
            return _json({"error": "Не удалось обработать запрос входа."}, 500)

    app.router.add_get("/", index)
    # 🖥 Диспетчерская карта для ПК: /dispatch (вход без Telegram) — webapp/dispatch_web.py
    from .dispatch_web import add_dispatch_routes

    add_dispatch_routes(app, api)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/media/{kind}/{ident}", media)
    app.router.add_route("*", "/api/auth/{action}", standalone_auth)

    async def export_file(request: web.Request) -> web.Response:
        # Без заголовка Authorization: файл скачивает сам Telegram (downloadFile)
        # по ссылке со случайным токеном, выданной админу; живёт 10 минут.
        found = api.get_export(request.match_info["token"])
        if not found:
            raise web.HTTPNotFound(text="Ссылка устарела — сформируйте выгрузку заново.")
        data, filename = found
        return web.Response(
            body=data,
            content_type=XLSX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
        )

    app.router.add_get("/export/{token}/{name}", export_file)

    assets = _Assets(STATIC_DIR)

    async def asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        raw, packed, etag = assets.get(name)
        # Адрес с ?v=<хеш содержимого> меняется вместе с файлом — такой файл телефон
        # может хранить без перепроверки (нет лишнего запроса при каждом открытии).
        versioned = request.query.get("v") == assets_hash(STATIC_DIR)
        cache = "public, max-age=31536000, immutable" if versioned else "no-cache"
        headers = {"ETag": etag, "Cache-Control": cache, "Vary": "Accept-Encoding"}
        if etag in request.headers.get("If-None-Match", ""):
            return web.Response(status=304, headers=headers)
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            body = packed
        else:
            body = raw
        return web.Response(body=body, content_type=assets.TYPES[name], charset="utf-8", headers=headers)

    app.router.add_get("/static/{name:app\\.js|style\\.css}", asset)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.router.add_route("*", "/api/{tail:.*}", api_handler)

    async def no_stale_static(request: web.Request, response: web.StreamResponse) -> None:
        # Статику всегда перепроверять у сервера (ETag/Last-Modified), не брать
        # молча из кеша — так обновление приложения доходит до телефона сразу.
        if request.path.startswith("/static/img/"):
            # Картинки меню и карты регионов меняются редко — 6 часов без перепроверки.
            response.headers["Cache-Control"] = "public, max-age=21600"
        elif request.path.startswith("/static/") and "immutable" not in response.headers.get("Cache-Control", ""):
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(no_stale_static)

    return app


async def start_webapp(
    api: Api, bridge, bot_token: str | None, host: str, port: int, auth_service=None
) -> web.AppRunner:
    app = create_app(api, bridge, bot_token, auth_service=auth_service)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Mini App server listening on %s:%s", host, port)
    return runner
