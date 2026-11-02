"""Регионы курьеров (решение владельца 23.10.2026).

5 регионов: «Центр» — внутри ТТК; за ТТК Москва делится косым крестом (×)
через Кремль на «Север», «Восток», «Юг», «Запад» (границы — диагонали
СВ/ЮВ/ЮЗ/СЗ). Контур ТТК — из своей базы адресов (OpenStreetMap).
"""
from __future__ import annotations

import math

REGIONS: list[tuple[str, str]] = [
    ("center", "Центр"), ("north", "Север"), ("east", "Восток"), ("south", "Юг"), ("west", "Запад"),
]
REGION_LABELS = dict(REGIONS)
KREMLIN = (55.7520, 37.6175)
MAX_COURIER_REGIONS = 2


def sector(lat: float, lon: float) -> str:
    """Сектор за ТТК по направлению от Кремля. Долготу сжимаем на cos широты,
    иначе на широте Москвы «45°» было бы перекошено."""
    lat0, lon0 = KREMLIN
    dx = (lon - lon0) * math.cos(math.radians(lat0))
    dy = lat - lat0
    a = math.degrees(math.atan2(dy, dx)) % 360  # 0 — восток, 90 — север
    if 45 <= a < 135:
        return "north"
    if 135 <= a < 225:
        return "west"
    if 225 <= a < 315:
        return "south"
    return "east"


def clean_regions(raw) -> list[str]:
    """Список регионов курьера: только известные, без повторов, не больше двух."""
    out: list[str] = []
    for r in raw or []:
        if r in REGION_LABELS and r not in out:
            out.append(r)
    return out[:MAX_COURIER_REGIONS]
