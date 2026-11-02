"""Сборка базы адресов Москвы и Московской области из OpenStreetMap.

Запускается на GitHub Actions (.github/workflows/address-db.yml) по кнопке администратора «🛠 Обновить карты»:
  1. скачивается выгрузка Центрального ФО с Geofabrik (данные © участники
     OpenStreetMap, лицензия ODbL — https://www.openstreetmap.org/copyright);
  2. osmium вырезает Москву с областью и объекты с адресом + границу Москвы
     и экспортирует их в GeoJSONSeq (по объекту на строку);
  3. этот скрипт собирает из них компактную SQLite-базу для приложения:
     улицы (для подсказок), дома с координатами (дом → точка, точка → адрес)
     и упрощённую границу Москвы (точка → «Москва или нет»).

Только стандартная библиотека Python — чтобы скрипт можно было проверить где
угодно. В базе нет никаких данных покупателей: только адреса OSM.

Использование:
    python tools/build_address_db.py addr.geojsonseq addresses.sqlite
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
# Регионы курьеров: «Центр» — внутри ТТК. Контур ТТК берём из дорог OSM
# name=«Третье транспортное кольцо» / short_name=ТТК.
TTK_NAMES = {"Третье транспортное кольцо"}
KREMLIN = (55.7520, 37.6175)  # lat, lon — центр «креста» и контура ТТК
TTK_BINS = 360
MOSCOW_NAMES = {"Москва"}
SIMPLIFY_DEG = 0.0002  # ≈ 15–20 м: граница Москвы без лишних точек

_STREET_ABBR = {
    "ул", "улица", "пр", "пр-т", "просп", "проспект", "пер", "переулок", "ш", "шоссе", "б-р", "бульвар",
    "бул", "наб", "набережная", "пл", "площадь", "проезд", "пр-д", "туп", "тупик", "аллея", "д", "дом",
    "г", "город", "кв", "корп", "к", "стр", "мкр", "микрорайон",
}


_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# addr:city внутри Москвы, который на самом деле район/округ, а не город.
_MOSCOW_DISTRICT = re.compile(r"^(район|муниципальный округ|поселение|административный округ)\b", re.IGNORECASE)


def clean(text: str) -> str:
    """Убрать невидимые символы и двойные пробелы из названий OSM."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", text or "")).strip()


def norm(text: str) -> str:
    """Для поиска: строчные, «ё» → «е», без знаков препинания."""
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def query_tokens(text: str) -> list[str]:
    """Слова запроса покупателя без «ул», «д», «г» и т.п. (в OSM они пишутся полностью)."""
    return [t for t in norm(text).replace("-", " ").split() if t and t not in _STREET_ABBR]


def house_key(house: str) -> str:
    """«7 к2» / «7К2» / «7, корп. 2» → «7к2» — чтобы сравнивать номера домов."""
    h = norm(house).replace("корпус", "к").replace("корп", "к").replace("строение", "с").replace("стр", "с")
    return re.sub(r"[\s,.-]", "", h)


def ring_centroid(ring: list[list[float]]) -> tuple[float, float]:
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def feature_point(geom: dict) -> tuple[float, float] | None:
    t, c = geom.get("type"), geom.get("coordinates")
    if not c:
        return None
    if t == "Point":
        return c[1], c[0]
    if t == "Polygon":
        return ring_centroid(c[0])
    if t == "MultiPolygon":
        return ring_centroid(c[0][0])
    if t == "LineString":
        return ring_centroid(c)
    return None


def _simplify(points: list[list[float]], tol: float) -> list[list[float]]:
    """Дуглас — Пекер (итеративно), чтобы граница была лёгкой."""
    if len(points) < 4:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if length < 1e-12:  # замкнутое кольцо: начало = конец — берём расстояние до точки
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / length
            if d > best:
                best, idx = d, i
        if best > tol and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def simplify_multipolygon(polys: list) -> list:
    out = []
    for poly in polys:
        rings = [_simplify(r, SIMPLIFY_DEG) for r in poly]
        if len(rings[0]) >= 4:
            out.append([r for r in rings if len(r) >= 4])
    return out


class PolygonIndex:
    """Точка в многоугольнике (с дырами) с разбиением рёбер по полосам широты —
    быстро даже для сотен тысяч проверок."""

    BAND = 0.01

    def __init__(self, multipolygon: list) -> None:
        self.bands: dict[int, list[tuple[float, float, float, float]]] = {}
        lons, lats = [], []
        for poly in multipolygon:
            for ring in poly:
                for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                    lons += [x1, x2]
                    lats += [y1, y2]
                    if y1 == y2:
                        continue
                    lo, hi = sorted((y1, y2))
                    for b in range(int(lo // self.BAND), int(hi // self.BAND) + 1):
                        self.bands.setdefault(b, []).append((x1, y1, x2, y2))
        self.bbox = (min(lons), min(lats), max(lons), max(lats)) if lons else None

    def contains(self, lat: float, lon: float) -> bool:
        if not self.bbox:
            return False
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            return False
        inside = False
        for ax, ay, bx, by in self.bands.get(int(lat // self.BAND), ()):
            if (ay > lat) != (by > lat):
                cross = ax + (lat - ay) * (bx - ax) / (by - ay)
                if cross > lon:
                    inside = not inside
        return inside


def _is_ttk(p: dict) -> bool:
    return p.get("name") in TTK_NAMES or p.get("short_name") == "ТТК"


def _line_points(g: dict) -> list[list[float]]:
    t, c = g.get("type"), g.get("coordinates") or []
    if t == "LineString":
        return c
    if t in ("MultiLineString", "Polygon"):
        return [pt for part in c for pt in part]
    return []


def ttk_ring(points: list[list[float]]) -> list[list[float]]:
    """Контур ТТК вокруг Кремля: по каждому градусу направления — самая дальняя точка
    дороги (внешняя полоса). Кольцо «звёздное» относительно центра, поэтому так можно.
    Пропуски (тоннели с другим названием) заполняем между соседними направлениями."""
    lat0, lon0 = KREMLIN
    k = math.cos(math.radians(lat0))
    best: list[float | None] = [None] * TTK_BINS
    for lon, lat in points:
        dx, dy = (lon - lon0) * k, lat - lat0
        r = math.hypot(dx, dy)
        if r < 0.005 or r > 0.1:  # не ТТК (центр или далеко за кольцом)
            continue
        b = int(math.degrees(math.atan2(dy, dx)) % 360) % TTK_BINS
        if best[b] is None or r > best[b]:
            best[b] = r
    known = [i for i, r in enumerate(best) if r is not None]
    if len(known) < 24:  # слишком мало направлений — контура нет
        return []
    for i in range(TTK_BINS):
        if best[i] is None:
            prev = max((j for j in known if j < i), default=known[-1] - TTK_BINS)
            nxt = min((j for j in known if j > i), default=known[0] + TTK_BINS)
            rp, rn = best[prev % TTK_BINS], best[nxt % TTK_BINS]
            best[i] = rp + (rn - rp) * (i - prev) / (nxt - prev)
    ring = []
    for i, r in enumerate(best):
        a = math.radians(i + 0.5)
        ring.append([round(lon0 + r * math.cos(a) / k, 6), round(lat0 + r * math.sin(a), 6)])
    ring.append(ring[0])
    return ring


def build(src: Path, dst: Path, source_note: str = "") -> dict:
    moscow_polys: list = []
    ttk_points: list[list[float]] = []
    raw: list[tuple[str, str, str, float, float]] = []
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")  # RFC 8142: перед объектом может стоять RS
            if not line:
                continue
            f = json.loads(line)
            p = f.get("properties") or {}
            g = f.get("geometry") or {}
            if (p.get("boundary") == "administrative" and str(p.get("admin_level")) == "4"
                    and (p.get("name") in MOSCOW_NAMES or p.get("name:ru") in MOSCOW_NAMES)):
                if g.get("type") == "Polygon":
                    moscow_polys.append(g["coordinates"])
                elif g.get("type") == "MultiPolygon":
                    moscow_polys.extend(g["coordinates"])
                continue
            if _is_ttk(p):
                ttk_points.extend(_line_points(g))
                continue
            house = clean(p.get("addr:housenumber") or "")
            street = clean(p.get("addr:street") or p.get("addr:place") or "")
            if not house or not street:
                continue
            pt = feature_point(g)
            if not pt:
                continue
            city = clean(p.get("addr:city") or "")
            raw.append((city, street, house, round(pt[0], 6), round(pt[1], 6)))

    moscow_polys = simplify_multipolygon(moscow_polys)
    index = PolygonIndex(moscow_polys) if moscow_polys else None
    if index is None:
        raise SystemExit("Граница Москвы не найдена — проверьте фильтр osmium (boundary=administrative, admin_level=4).")

    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(dst)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE streets (id INTEGER PRIMARY KEY, city TEXT, street TEXT, moscow INTEGER,
                              lat REAL, lon REAL, houses INTEGER, search TEXT);
        CREATE TABLE houses (id INTEGER PRIMARY KEY, street_id INTEGER, house TEXT, hkey TEXT,
                             lat REAL, lon REAL, moscow INTEGER);
        CREATE INDEX idx_houses_street ON houses (street_id, hkey);
        CREATE INDEX idx_houses_lat ON houses (lat);
        CREATE INDEX idx_streets_search ON streets (search);
        CREATE TABLE moscow (geojson TEXT);
        CREATE TABLE ttk (geojson TEXT);
        """
    )
    streets: dict[tuple[str, str], int] = {}
    seen: set[tuple[int, str]] = set()
    houses = []
    moscow_count = 0
    for city, street, house, lat, lon in raw:
        in_moscow = index.contains(lat, lon)
        if in_moscow and (not city or _MOSCOW_DISTRICT.match(city)):
            city = "Москва"
        key = (city, street)
        sid = streets.get(key)
        if sid is None:
            sid = streets[key] = len(streets) + 1
            conn.execute(
                "INSERT INTO streets (id, city, street, moscow, lat, lon, houses, search) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (sid, city, street, 1 if in_moscow else 0, lat, lon, norm(f"{street} {city}")),
            )
        hk = house_key(house)
        if (sid, hk) in seen:
            continue
        seen.add((sid, hk))
        houses.append((sid, house, hk, lat, lon, 1 if in_moscow else 0))
        moscow_count += 1 if in_moscow else 0
    conn.executemany(
        "INSERT INTO houses (street_id, house, hkey, lat, lon, moscow) VALUES (?, ?, ?, ?, ?, ?)", houses
    )
    conn.execute("UPDATE streets SET houses = (SELECT COUNT(*) FROM houses h WHERE h.street_id = streets.id)")
    # Улица «в Москве», если большинство её домов в Москве.
    conn.execute(
        """UPDATE streets SET moscow = (SELECT CASE WHEN SUM(h.moscow) * 2 >= COUNT(*) THEN 1 ELSE 0 END
                                         FROM houses h WHERE h.street_id = streets.id)"""
    )
    try:
        conn.execute("CREATE VIRTUAL TABLE streets_fts USING fts5(search, content='streets', content_rowid='id')")
        conn.execute("INSERT INTO streets_fts (rowid, search) SELECT id, search FROM streets")
    except sqlite3.OperationalError:
        pass  # без FTS5 сервер ищет через LIKE
    conn.execute("INSERT INTO moscow (geojson) VALUES (?)", (json.dumps(moscow_polys),))
    ring = ttk_ring(ttk_points)
    if ring:
        conn.execute("INSERT INTO ttk (geojson) VALUES (?)", (json.dumps([[ring]]),))
    info = {
        "schema": str(SCHEMA_VERSION),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "houses": str(len(houses)),
        "streets": str(len(streets)),
        "moscow_houses": str(moscow_count),
        "ttk_points": str(len(ttk_points)),
        "source": source_note or "OpenStreetMap (Geofabrik)",
        "license": "© участники OpenStreetMap, ODbL — https://www.openstreetmap.org/copyright",
    }
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", list(info.items()))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return info


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    result = build(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if int(result["houses"]) < 1000 and "--allow-small" not in sys.argv:
        raise SystemExit("Слишком мало домов — похоже, выгрузка неполная. База не публикуется.")
