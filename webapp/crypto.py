"""Криптовалюты для оплаты при курьере: список монет, курсы, пересчёт рублей.

Курсы — публичный эндпоинт CoinGecko «simple price» (без ключа), тот же,
откуда бот уже берёт курс USDT (main.USDT_RATE_URL). Сумма в монете
округляется ВВЕРХ, чтобы продавец не получил меньше, — как rub_to_usdt в боте.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import ROUND_CEILING, Decimal
from typing import Any

logger = logging.getLogger(__name__)

# код → (id в CoinGecko, знаков после запятой, название)
CURRENCIES: dict[str, tuple[str, int, str]] = {
    "USDT": ("tether", 2, "Tether (USDT)"),
    "USDC": ("usd-coin", 2, "USD Coin (USDC)"),
    "BTC": ("bitcoin", 8, "Bitcoin (BTC)"),
    "ETH": ("ethereum", 6, "Ethereum (ETH)"),
    "TON": ("the-open-network", 3, "Toncoin (TON)"),
    "TRX": ("tron", 2, "TRON (TRX)"),
    "LTC": ("litecoin", 6, "Litecoin (LTC)"),
    "SOL": ("solana", 5, "Solana (SOL)"),
    "BNB": ("binancecoin", 6, "BNB (BNB)"),
}
# Смайлик монеты — в начале кнопок выбора у покупателя и курьера.
EMOJI: dict[str, str] = {
    "USDT": "💵", "USDC": "🔵", "BTC": "🟠", "ETH": "🔷", "TON": "💎",
    "TRX": "🔴", "LTC": "⚪", "SOL": "🟣", "BNB": "🟡",
}
SBP_EMOJI = "📱"
# Сети для каждой монеты (выпадающий список в админке). Первая — по умолчанию.
NETWORKS: dict[str, list[str]] = {
    "USDT": ["TRC-20 (Tron)", "BEP-20 (BNB Smart Chain)", "ERC-20 (Ethereum)", "TON", "Solana"],
    "USDC": ["ERC-20 (Ethereum)", "BEP-20 (BNB Smart Chain)", "Solana", "TRC-20 (Tron)"],
    "BTC": ["Bitcoin"],
    "ETH": ["Ethereum", "BEP-20 (BNB Smart Chain)"],
    "TON": ["TON"],
    "TRX": ["Tron"],
    "LTC": ["Litecoin"],
    "SOL": ["Solana"],
    "BNB": ["BNB Smart Chain (BEP-20)"],
}
RATE_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=rub"
RATE_TTL = 3600  # секунд — курс обновляется раз в час (решение владельца)
RATE_TIMEOUT = 6  # секунд — дольше CoinGecko не ждём


def rub_to_coin(rub: int, rate: float, currency: str) -> Decimal:
    decimals = CURRENCIES[currency][1]
    step = Decimal(1).scaleb(-decimals)
    return (Decimal(int(rub)) / Decimal(str(rate))).quantize(step, rounding=ROUND_CEILING)


def build_quotes(total_rub: int, rates: dict[str, float]) -> dict[str, dict[str, Any]]:
    """{'USDT': {'amount': '68.43', 'rate': 95.0}, ...} — только для монет с курсом."""
    result = {}
    for code, rate in rates.items():
        if code in CURRENCIES and rate and rate > 0:
            result[code] = {"amount": str(rub_to_coin(total_rub, rate, code)), "rate": float(rate)}
    return result


def dumps(quotes: dict[str, Any]) -> str:
    return json.dumps(quotes, ensure_ascii=False)


def loads(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


class RateCache:
    """Курсы к рублю из CoinGecko. Обновляются не чаще раза в RATE_TTL (час).

    Никто не ждёт CoinGecko, если есть хоть какой-то сохранённый курс: устаревший
    курс отдаётся сразу, а свежий подтягивается в фоне. Ждать приходится только
    самый первый запрос после запуска, если курса ещё нет совсем."""

    def __init__(self) -> None:
        self._rates: dict[str, float] = {}
        self._at: float = 0.0
        self._refresh: "asyncio.Task | None" = None

    def peek(self) -> dict[str, float]:
        return dict(self._rates)

    async def get(self, codes: list[str]) -> dict[str, float]:
        codes = [c for c in dict.fromkeys(codes) if c in CURRENCIES]
        if not codes:
            return {}
        fresh = time.time() - self._at < RATE_TTL
        if fresh and all(c in self._rates for c in codes):
            return {c: self._rates[c] for c in codes if c in self._rates}
        if self._rates and not fresh:
            self.refresh_in_background()
            return {c: self._rates[c] for c in codes if c in self._rates}
        await self._refresh_once()
        return {c: self._rates[c] for c in codes if c in self._rates}

    async def get_now(self, codes: list[str]) -> tuple[dict[str, float], float | None]:
        """Свежий курс прямо сейчас (для выгрузки в Excel). Если CoinGecko не
        ответил — последний сохранённый. Возвращает (курсы, время курса)."""
        try:
            await self._refresh_once()
        except Exception:
            logger.exception("Курсы криптовалют: свежий курс для выгрузки недоступен")
        return {c: self._rates[c] for c in codes if c in self._rates}, (self._at or None)

    def refresh_in_background(self) -> None:
        if self._refresh is None or self._refresh.done():
            self._refresh = asyncio.create_task(self._refresh_safely())

    async def _refresh_safely(self) -> None:
        try:
            await self._refresh_once()
        except Exception:
            logger.exception("Курсы криптовалют: CoinGecko недоступен, остаётся прежний курс")

    async def _refresh_once(self) -> None:
        # Один запрос на всех: если обновление уже идёт — ждём его.
        if self._refresh is not None and not self._refresh.done() and asyncio.current_task() is not self._refresh:
            await asyncio.shield(self._refresh)
            return
        import aiohttp  # только в рабочем режиме

        ids = ",".join(CURRENCIES[c][0] for c in CURRENCIES)
        timeout = aiohttp.ClientTimeout(total=RATE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(RATE_URL.format(ids=ids)) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        fresh = {}
        for code, (cg_id, _, _) in CURRENCIES.items():
            try:
                rate = float(data[cg_id]["rub"])
            except (KeyError, TypeError, ValueError):
                continue
            if rate > 0:
                fresh[code] = rate
        if fresh:
            self._rates, self._at = fresh, time.time()
