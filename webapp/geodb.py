"""Своя база адресов (OpenStreetMap) вместо онлайн-сервисов.

База собирается на GitHub Actions только по кнопке администратора
(tools/build_address_db.py, .github/workflows/address-db.yml) и лежит в Releases
репозитория под тегом «address-db». Сервер скачивает её сам (при первом запуске,
если базы ещё нет, и после сборки по кнопке) и дальше ищет у себя — во время
оформления заказа никакие данные никуда не уходят.

Скачивание из закрытого репозитория — с токеном GitHub (переменная
GEODB_GITHUB_TOKEN на Amvera): Contents — чтение; для кнопки «собрать заново» ещё
Actions — чтение и запись. Без токена/базы приложение работает
как раньше: адрес вводится вручную, «Москва или нет» — по слову «Москва».

Данные © участники OpenStreetMap, ODbL — https://www.openstreetmap.org/copyright
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
from build_address_db import PolygonIndex, house_key, query_tokens  # noqa: E402

ATTRIBUTION = "© участники OpenStreetMap, ODbL"
RELEASE_TAG = "address-db"
ASSET_NAME = "addresses.sqlite.gz"
DEFAULT_REPO = "artprosh/miniapp"
API_BASE = "https://api.github.com"
WORKFLOW_FILE = "address-db.yml"
REBUILD_POLL = 60               # после запуска сборки — раз в минуту смотреть, готова ли
REBUILD_TIMEOUT = 75 * 60       # сборка обычно 6–10 минут; дольше часа — считаем сбоем
MIN_KEEP_RATIO = 0.7            # новая база меньше 70 % старой — подозрительно, не ставим
NEAREST_METERS = 300

_MOSCOW_WORD = re.compile(r"(^|[\s,.])(г\.?\s*)?москва($|[\s,.])", re.IGNORECASE)


TOKEN_VAR = "GEODB_GITHUB_TOKEN"
# Кириллица, похожая на латиницу — частая ошибка при вводе имени переменной.
_LOOKALIKE = str.maketrans("АВЕКМНОРСТУХаеорсух", "ABEKMHOPCTYXaeopcyx")


def _canon(name: str) -> str:
    return re.sub(r"[\s\-]", "_", name.translate(_LOOKALIKE).strip()).upper()


def find_token() -> tuple[str | None, str]:
    """GEODB_GITHUB_TOKEN; если имя введено с пробелом/русской буквой/строчными —
    тоже находим (и сообщаем админу, что имя стоит поправить)."""
    exact = (os.getenv(TOKEN_VAR) or "").strip().strip("'\"")
    if exact:
        return TOKEN_VAR, exact
    for key, value in os.environ.items():
        if key != TOKEN_VAR and _canon(key) == TOKEN_VAR and value.strip():
            return key, value.strip().strip("'\"")
    return None, ""


def token_missing_message() -> str:
    """Почему токена нет — с подсказкой по похожим именам (значения не показываем)."""
    similar = sorted(k for k in os.environ if "GEODB" in _canon(k) or ("GITHUB" in _canon(k) and "TOKEN" in _canon(k)))
    msg = (f"Сервер не видит переменную {TOKEN_VAR}. Проверьте в Amvera: имя ровно {TOKEN_VAR}, "
           "этап «Запуск» (не «Сборка»), затем «Перезапустить».")
    if similar:
        msg += " Похожие переменные на сервере: " + ", ".join(f"«{k}»" for k in similar) + " — у них пустое значение или другое имя."
    return msg


def text_is_moscow(line: str) -> bool:
    """Запасной способ (нет базы или адрес не нашёлся): в адресе есть слово «Москва»."""
    return bool(_MOSCOW_WORD.search(line or ""))


_TTK_FALLBACK = None


def _fallback_ttk() -> PolygonIndex:
    global _TTK_FALLBACK
    if _TTK_FALLBACK is None:
        ring = json.loads((Path(__file__).with_name("ttk_fallback.json")).read_text(encoding="utf-8"))["ring"]
        _TTK_FALLBACK = PolygonIndex([[ring]])
    return _TTK_FALLBACK


def street_label(city: str, street: str) -> str:
    return f"{street}, {city}" if city else street


def _meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 111_320, (lon2 - lon1) * 111_320 * k)


class AddressDB:
    """Только чтение. Файл подменяется целиком при обновлении (reload)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None
        self.meta: dict[str, str] = {}
        self.moscow: PolygonIndex | None = None
        self.ttk: PolygonIndex | None = None
        self.fts = False
        self.last_check: float | None = None
        self.last_error: str | None = None
        self.rebuild: dict[str, Any] | None = None  # {"state": building|done|error, "started_at", "message"}
        self.reload()

    # ------------------------------------------------------------ загрузка
    def reload(self) -> bool:
        old = self.conn
        if not self.path.exists():
            self.conn, self.meta, self.moscow, self.ttk = None, {}, None, None
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
            row = conn.execute("SELECT geojson FROM moscow LIMIT 1").fetchone()
            polygon = PolygonIndex(json.loads(row["geojson"])) if row else None
            has_ttk = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ttk'").fetchone()
            trow = conn.execute("SELECT geojson FROM ttk LIMIT 1").fetchone() if has_ttk else None
            ttk = PolygonIndex(json.loads(trow["geojson"])) if trow else None
            fts = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'streets_fts'").fetchone())
        except (sqlite3.Error, ValueError) as exc:
            logger.error("База адресов повреждена (%s): %s", self.path, exc)
            return False
        self.conn, self.meta, self.moscow, self.fts, self.ttk = conn, meta, polygon, fts, ttk
        if old is not None:
            old.close()
        logger.info("База адресов: %s домов, сборка %s", meta.get("houses"), meta.get("built_at"))
        return True

    @property
    def loaded(self) -> bool:
        return self.conn is not None

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "built_at": self.meta.get("built_at"),
            "houses": int(self.meta.get("houses") or 0),
            "streets": int(self.meta.get("streets") or 0),
            "moscow_houses": int(self.meta.get("moscow_houses") or 0),
            "ttk": self.ttk is not None,
            "source": self.meta.get("source"),
            "last_error": self.last_error,
            "token_var": find_token()[0],
            "rebuild": dict(self.rebuild) if self.rebuild else None,
        }

    # ------------------------------------------------------------ поиск
    def suggest(self, q: str, limit: int = 7) -> list[dict[str, Any]]:
        """Улицы по началу слов: «лесн», «ул лесная», «лесная москва»; Москва — выше."""
        if not self.conn:
            return []
        tokens = query_tokens(q)
        if not tokens:
            return []
        if self.fts:
            match = " ".join(f'"{t}"*' for t in tokens)
            sql = """SELECT s.* FROM streets_fts f JOIN streets s ON s.id = f.rowid
                     WHERE streets_fts MATCH ? ORDER BY s.moscow DESC, s.houses DESC LIMIT ?"""
            args: list[Any] = [match, limit]
        else:
            sql = ("SELECT * FROM streets WHERE " + " AND ".join("search LIKE ?" for _ in tokens)
                   + " ORDER BY moscow DESC, houses DESC LIMIT ?")
            args = [f"%{t}%" for t in tokens] + [limit]
        args[-1] = limit * 3  # с запасом — одинаковые подписи ниже убираем
        try:
            rows = self.conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        out, seen = [], set()
        for r in rows:
            value = street_label(r["city"], r["street"])
            if value.lower() in seen:
                continue
            seen.add(value.lower())
            out.append({"id": r["id"], "value": value, "city": r["city"], "street": r["street"], "moscow": bool(r["moscow"])})
            if len(out) >= limit:
                break
        return out

    def street(self, street_id: int) -> dict[str, Any] | None:
        if not self.conn:
            return None
        r = self.conn.execute("SELECT * FROM streets WHERE id = ?", (int(street_id),)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "value": street_label(r["city"], r["street"]), "city": r["city"],
                "street": r["street"], "moscow": bool(r["moscow"]), "lat": r["lat"], "lon": r["lon"]}

    def house_point(self, street_id: int, house: str, building: str | None = None) -> dict[str, Any] | None:
        """Дом на выбранной улице → точка. «7» + корпус «2» ищется как «7к2», затем «7»."""
        if not self.conn or not house:
            return None
        keys = []
        if building:
            keys += [house_key(f"{house}к{building}"), house_key(f"{house}с{building}"), house_key(f"{house}{building}")]
        keys.append(house_key(house))
        for k in keys:
            r = self.conn.execute(
                "SELECT lat, lon, moscow, house FROM houses WHERE street_id = ? AND hkey = ? LIMIT 1", (int(street_id), k)
            ).fetchone()
            if r:
                return {"lat": r["lat"], "lon": r["lon"], "moscow": bool(r["moscow"]), "house": r["house"]}
        return None

    def nearest(self, lat: float, lon: float, max_m: float = NEAREST_METERS) -> dict[str, Any] | None:
        """Точка на карте → ближайший дом (до 300 м)."""
        if not self.conn:
            return None
        dlat = max_m / 111_320
        dlon = dlat / max(math.cos(math.radians(lat)), 0.2)
        rows = self.conn.execute(
            """SELECT h.house, h.lat, h.lon, h.moscow, s.id AS sid, s.city, s.street FROM houses h
               JOIN streets s ON s.id = h.street_id
               WHERE h.lat BETWEEN ? AND ? AND h.lon BETWEEN ? AND ?""",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        best, best_d = None, max_m
        for r in rows:
            d = _meters(lat, lon, r["lat"], r["lon"])
            if d <= best_d:
                best, best_d = r, d
        if not best:
            return None
        return {"street_id": best["sid"], "street": street_label(best["city"], best["street"]),
                "house": best["house"], "moscow": bool(best["moscow"]), "meters": round(best_d)}

    def point_in_moscow(self, lat: float, lon: float) -> bool | None:
        return self.moscow.contains(lat, lon) if self.moscow else None

    def region(self, lat: float | None, lon: float | None) -> str | None:
        """Регион курьеров для точки в Москве: center (внутри ТТК) или сектор north/east/south/west.
        None — точки нет или она не в Москве (тогда заказ видят все курьеры). Если в скачанной
        базе ещё нет контура ТТК — берём контур из сборки 22.10 (webapp/ttk_fallback.json)."""
        if lat is None or lon is None or not self.point_in_moscow(lat, lon):
            return None
        from .regions import sector
        ttk = self.ttk or _fallback_ttk()
        return "center" if ttk.contains(lat, lon) else sector(lat, lon)

    def locate_address(self, address: str | None) -> tuple[float, float] | None:
        """Точка по тексту адреса заказа («Лесная улица, Москва, д. 7, корп./стр. 2, …»):
        улица по подсказкам, затем дом; дом не нашёлся — точка улицы."""
        if not self.conn or not address:
            return None
        text = str(address).split("\n")[0]
        m = re.match(r"\s*(.+?),\s*(?:д\.?|дом)\s*([0-9][0-9A-Za-zА-Яа-я/\-]*)", text)
        line, house = (m.group(1), m.group(2)) if m else (text, None)
        b = re.search(r"корп\./стр\.\s*([^,]+)", text)
        found = [s for s in self.suggest(line, limit=3) if s["moscow"]] or self.suggest(line, limit=1)
        if not found:
            return None
        street_id = int(found[0]["id"])
        if house:
            point = self.house_point(street_id, house, b.group(1).strip() if b else None)
            if point:
                return point["lat"], point["lon"]
        st = self.street(street_id)
        return (st["lat"], st["lon"]) if st and st.get("lat") is not None else None

    # ------------------------------------------------------------ обновление с GitHub
    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RelaxGas-miniapp"}

    async def _remote_asset_time(self) -> str:
        """Когда на GitHub последний раз загружен файл базы ('' — файла ещё нет)."""
        _, token = find_token()
        repo = (os.getenv("GEODB_REPO") or DEFAULT_REPO).strip()
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(f"{API_BASE}/repos/{repo}/releases/tags/{RELEASE_TAG}",
                                   headers=self._headers(token)) as resp:
                if resp.status == 404:
                    return ""
                resp.raise_for_status()
                release = await resp.json()
        asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
        return (asset or {}).get("updated_at") or ""

    async def start_rebuild(self) -> dict[str, Any]:
        """Кнопка админа «Обновить карты»: запустить сборку на GitHub Actions, дождаться
        новой базы в Releases и скачать её. Возвращается сразу, ждёт — фоном."""
        if self.rebuild and self.rebuild.get("state") == "building":
            return {"ok": True, "already": True, **self.status()}
        name, token = find_token()
        if not token:
            raise RuntimeError(token_missing_message())
        repo = (os.getenv("GEODB_REPO") or DEFAULT_REPO).strip()
        import aiohttp

        before = await self._remote_asset_time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(f"{API_BASE}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
                                    headers=self._headers(token), json={"ref": "main"}) as resp:
                if resp.status in (401, 403, 404):
                    raise RuntimeError(
                        f"GitHub не дал запустить сборку (HTTP {resp.status}). Токену GEODB_GITHUB_TOKEN нужно право "
                        "«Actions: Read and write» (в настройках токена на GitHub → Repository permissions). "
                        "Или запустите вручную: GitHub → Actions → «Address database» → Run workflow, "
                        "потом здесь «⬇️ Скачать готовую».")
                if resp.status not in (200, 204):
                    raise RuntimeError(f"GitHub ответил HTTP {resp.status} — сборка не запущена.")
        self.rebuild = {"state": "building", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "message": "Сборка запущена на GitHub (обычно 6–10 минут), потом база скачается сама."}
        self._watch_task = asyncio.get_event_loop().create_task(self._wait_and_download(before))
        return {"ok": True, **self.status()}

    async def _wait_and_download(self, before: str) -> None:
        deadline = time.monotonic() + REBUILD_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(REBUILD_POLL)
            try:
                now = await self._remote_asset_time()
            except Exception as exc:  # noqa: BLE001 — сеть моргнула, пробуем дальше
                logger.warning("База адресов: не удалось проверить сборку: %s", exc)
                continue
            if now and now != before:
                res = await self.update_from_github(force=True)
                if res.get("error"):
                    self.rebuild.update(state="error", message=f"Сборка готова, но скачать не удалось: {res['error']}")
                else:
                    self.rebuild.update(state="done", message="Карты обновлены.")
                return
        self.rebuild.update(state="error", message="Сборка не завершилась за час — посмотрите GitHub → Actions → «Address database».")

    async def update_from_github(self, force: bool = False) -> dict[str, Any]:
        """Скачать свежую базу из Releases (тег address-db), если она новее нашей."""
        name, token = find_token()
        repo = (os.getenv("GEODB_REPO") or DEFAULT_REPO).strip()
        self.last_check = time.time()
        if not token:
            self.last_error = token_missing_message()
            return {"updated": False, "error": self.last_error}
        import aiohttp  # только в рабочем режиме

        headers = self._headers(token)
        timeout = aiohttp.ClientTimeout(total=600)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{API_BASE}/repos/{repo}/releases/tags/{RELEASE_TAG}",
                                       headers=headers) as resp:
                    if resp.status == 404:
                        raise RuntimeError("Релиз address-db не найден — запустите на GitHub Actions «Address database».")
                    if resp.status in (401, 403):
                        raise RuntimeError(f"GitHub отклонил токен (HTTP {resp.status}) — проверьте GEODB_GITHUB_TOKEN.")
                    resp.raise_for_status()
                    release = await resp.json()
                asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
                if not asset:
                    raise RuntimeError("В релизе address-db нет файла базы — дождитесь окончания сборки.")
                remote_at = asset.get("updated_at") or ""
                if not force and self.loaded and self.meta.get("asset_updated_at") == remote_at:
                    self.last_error = None
                    return {"updated": False, "reason": "up_to_date"}
                tmp_gz = self.path.with_suffix(".download.gz")
                tmp_db = self.path.with_suffix(".download")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # GitHub отвечает перенаправлением на хранилище файлов — туда идём
                # уже без токена (он нужен только самому GitHub).
                dl_headers = dict(headers, Accept="application/octet-stream")
                async with session.get(asset["url"], headers=dl_headers, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        target = resp.headers["Location"]
                    else:
                        resp.raise_for_status()
                        target = None
                        with open(tmp_gz, "wb") as out:
                            async for chunk in resp.content.iter_chunked(1 << 20):
                                out.write(chunk)
                if target:
                    async with session.get(target, headers={"User-Agent": "RelaxGas-miniapp"}) as resp:
                        resp.raise_for_status()
                        with open(tmp_gz, "wb") as out:
                            async for chunk in resp.content.iter_chunked(1 << 20):
                                out.write(chunk)
            await asyncio.to_thread(self._unpack_and_swap, tmp_gz, tmp_db, remote_at)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.error("База адресов не обновлена: %s", exc)
            return {"updated": False, "error": self.last_error}
        self.last_error = None
        return {"updated": True, **self.status()}

    def _unpack_and_swap(self, tmp_gz: Path, tmp_db: Path, remote_at: str) -> None:
        with gzip.open(tmp_gz, "rb") as src, open(tmp_db, "wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_gz.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp_db)
        try:
            new_houses = int(conn.execute("SELECT value FROM meta WHERE key = 'houses'").fetchone()[0])
            old_houses = int(self.meta.get("houses") or 0)
            if old_houses and new_houses < old_houses * MIN_KEEP_RATIO:
                raise RuntimeError(f"В новой базе подозрительно мало домов ({new_houses} против {old_houses}) — оставлена прежняя.")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('asset_updated_at', ?)", (remote_at,))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('downloaded_at', ?)",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp_db, self.path)
        self.reload()

    def start_updates(self) -> None:
        """При запуске: только если базы на сервере ещё нет — скачать готовую.
        Дальше база обновляется лишь по кнопке администратора."""
        if getattr(self, "_task", None) is not None or self.loaded:
            return
        self._task = asyncio.get_event_loop().create_task(self.update_from_github())
