"""Маршрут по дорогам для доставки за пределы Москвы (25.10.2026).

Маршрут строит openrouteservice (карты OpenStreetMap) от Кремля до точки дома.
Платные — только километры маршрута ЗА границей Москвы (граница — из своей базы
адресов): у каждого отрезка маршрута смотрим, где его середина. Ключ — переменная
Amvera ORS_API_KEY. Нет ключа, нет ответа, нет точки — заказ остаётся
индивидуальным, как раньше (решает администратор).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

logger = logging.getLogger(__name__)

KREMLIN = (55.7520, 37.6175)
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def outside_km(coords: list[list[float]], inside: Callable[[float, float], bool | None]) -> float:
    """Длина частей маршрута вне Москвы. coords — [[lon, lat], …] как в GeoJSON."""
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        if not inside((lat1 + lat2) / 2, (lon1 + lon2) / 2):
            total += haversine_km(lat1, lon1, lat2, lon2)
    return total


def parse_ors(data: dict[str, Any]) -> list[list[float]]:
    """Точки маршрута из ответа openrouteservice (GeoJSON)."""
    feats = (data or {}).get("features") or []
    if not feats:
        raise ValueError("пустой ответ маршрута")
    coords = ((feats[0] or {}).get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        raise ValueError("в маршруте нет точек")
    return [[float(p[0]), float(p[1])] for p in coords]


class RouteService:
    """Запрос маршрута. fetch — для тестов (подмена сети)."""

    def __init__(self, api_key: str | None, fetch: Callable | None = None, timeout: float = 12.0) -> None:
        self.api_key = (api_key or "").strip()
        self._fetch = fetch
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key or self._fetch)

    async def route(self, lat: float, lon: float) -> list[list[float]]:
        params = {"start": f"{KREMLIN[1]},{KREMLIN[0]}", "end": f"{lon},{lat}"}
        if self._fetch is not None:
            return parse_ors(await self._fetch(params))
        import aiohttp

        headers = {"Authorization": self.api_key, "Accept": "application/geo+json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            async with session.get(ORS_URL, params=params, headers=headers) as resp:
                if resp.status != 200:
                    raise ValueError(f"openrouteservice ответил {resp.status}")
                return parse_ors(await resp.json(content_type=None))
