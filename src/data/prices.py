"""Daily close prices and trade-performance computation.

Uses Yahoo Finance's public chart endpoint (no key required) with a SQLite
cache so /leaderboard and /politician don't hammer the API. Performance is
measured from the first close on/after the disclosure (filing) date to the
latest close, compared against SPY over the same window — i.e. what a
copy-trader could actually have captured.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from src.data.http_util import request_with_retry
from src.storage import database as db

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# Re-fetch a ticker's history at most every 12 hours.
_CACHE_SECONDS = 12 * 3600
# Cap distinct tickers per performance computation to stay polite.
_MAX_TICKERS = 40


class PriceService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get_history(self, ticker: str, start: date) -> dict[str, float]:
        """Return {iso_date: close} for *ticker* from *start* to today."""
        ticker = ticker.upper()
        kv_key = f"price_fetched_at:{ticker}"
        fetched_at = await db.kv_get(self.db_path, kv_key)

        cached = await db.get_price_history(self.db_path, ticker, start.isoformat())
        if cached and fetched_at and (time.time() - float(fetched_at)) < _CACHE_SECONDS:
            earliest = min(cached)
            # The cache must actually cover the requested window.
            if earliest <= (start + timedelta(days=5)).isoformat():
                return cached

        history = await self._fetch_history(ticker, start)
        if history:
            await db.upsert_price_history(self.db_path, ticker, history)
            await db.kv_set(self.db_path, kv_key, str(time.time()))
            return history
        return cached

    async def _fetch_history(self, ticker: str, start: date) -> dict[str, float]:
        period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        period2 = int(time.time())
        url = CHART_URL.format(ticker=ticker)
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=_UA) as client:
                resp = await request_with_retry(
                    client,
                    "GET",
                    url,
                    params={"period1": period1, "period2": period2, "interval": "1d"},
                )
                resp.raise_for_status()
                payload = resp.json()
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            closes = result["indicators"]["quote"][0].get("close", [])
        except Exception:
            logger.warning("PriceService: failed to fetch history for %s", ticker, exc_info=True)
            return {}

        history: dict[str, float] = {}
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            history[day] = float(close)
        return history


def window_return(history: dict[str, float], start: date) -> float | None:
    """Return (last / first-close-on-or-after-*start*) - 1, or None."""
    if not history:
        return None
    days = sorted(history)
    entry_day = next((d for d in days if d >= start.isoformat()), None)
    if entry_day is None:
        return None
    entry, latest = history[entry_day], history[days[-1]]
    if entry <= 0:
        return None
    return latest / entry - 1.0


async def compute_performance(
    price_service: PriceService,
    purchases: list[dict],
) -> list[dict]:
    """Annotate purchase rows with return-since-disclosure vs SPY.

    *purchases* rows need ``ticker``, ``politician_name`` and a disclosure
    date (``filing_date`` falling back to ``transaction_date``). Rows whose
    prices can't be resolved are dropped.
    """
    dated: list[tuple[dict, date]] = []
    for row in purchases:
        raw = row.get("filing_date") or row.get("transaction_date")
        if not raw or not row.get("ticker"):
            continue
        try:
            disclosed = date.fromisoformat(raw)
        except ValueError:
            continue
        dated.append((row, disclosed))
    if not dated:
        return []

    earliest = min(d for _, d in dated)
    tickers = list(dict.fromkeys(row["ticker"].upper() for row, _ in dated))[:_MAX_TICKERS]

    spy_history = await price_service.get_history("SPY", earliest)
    histories: dict[str, dict[str, float]] = {}
    for ticker in tickers:
        histories[ticker] = await price_service.get_history(ticker, earliest)
        await asyncio.sleep(0.2)  # gentle pacing

    results: list[dict] = []
    for row, disclosed in dated:
        history = histories.get(row["ticker"].upper())
        if not history:
            continue
        ret = window_return(history, disclosed)
        spy_ret = window_return(spy_history, disclosed)
        if ret is None or spy_ret is None:
            continue
        annotated = dict(row)
        annotated["return_pct"] = ret * 100
        annotated["spy_return_pct"] = spy_ret * 100
        annotated["excess_pct"] = (ret - spy_ret) * 100
        results.append(annotated)
    return results


def leaderboard_from_performance(
    performance: list[dict], *, min_trades: int = 2, limit: int = 10
) -> list[dict]:
    """Aggregate per-politician average excess return over SPY."""
    grouped: dict[str, list[dict]] = {}
    for row in performance:
        grouped.setdefault(row["politician_name"], []).append(row)

    board: list[dict] = []
    for name, rows in grouped.items():
        if len(rows) < min_trades:
            continue
        board.append(
            {
                "politician_name": name,
                "trades": len(rows),
                "avg_return_pct": sum(r["return_pct"] for r in rows) / len(rows),
                "avg_excess_pct": sum(r["excess_pct"] for r in rows) / len(rows),
            }
        )
    board.sort(key=lambda r: r["avg_excess_pct"], reverse=True)
    return board[:limit]
