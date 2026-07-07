"""Background scanner that watches options chains for unusual activity.

Scans the configured watchlist during US market hours and posts an alert
embed for each contract that trips the thresholds. Alerts are deduplicated
per contract per day in the ``flow_alerts`` table so a contract that stays
hot doesn't repost every cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from src.config import settings
from src.analysis.flow import detect_unusual_activity

if TYPE_CHECKING:
    from src.bot.bot import TradeBot
    from src.data.options import OptionsProvider

log = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")


def is_market_hours(now: datetime | None = None) -> bool:
    """True during regular US equity hours (9:30–16:00 ET, Mon–Fri)."""
    now_et = (now or datetime.now(tz=_EASTERN)).astimezone(_EASTERN)
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


class FlowScanner:
    def __init__(
        self,
        provider: OptionsProvider,
        bot: TradeBot,
        db_path: str,
        interval_minutes: int | None = None,
    ) -> None:
        self.provider = provider
        self.bot = bot
        self.db_path = db_path
        self.interval_minutes = interval_minutes or settings.flow_poll_minutes
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(
            "FlowScanner started — watchlist=%s every %d min (market hours only)",
            settings.flow_watchlist,
            self.interval_minutes,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def scan_once(self) -> int:
        """Scan the watchlist once; returns the number of alerts posted."""
        from src.storage.database import record_flow_event, was_flow_alerted

        posted = 0
        for ticker in settings.flow_watchlist_tickers():
            chain = await self.provider.fetch_chain(ticker)
            if chain is None or not chain.contracts:
                continue
            hits = detect_unusual_activity(
                chain,
                min_premium_usd=settings.flow_min_premium_usd,
                limit=3,
            )
            for hit in hits:
                c = hit.contract
                key = c.contract_symbol or (
                    f"{ticker}|{c.option_type}|{c.strike}|{c.expiration}"
                )
                today = date.today().isoformat()
                if await was_flow_alerted(self.db_path, key, today):
                    continue
                try:
                    await self.bot.send_flow_alert(hit)
                    await record_flow_event(
                        self.db_path,
                        {
                            "contract_key": key,
                            "alert_date": today,
                            "ticker": c.ticker,
                            "option_type": c.option_type,
                            "strike": c.strike,
                            "expiration": c.expiration.isoformat(),
                            "premium_usd": c.premium_notional,
                            "volume": c.volume,
                            "open_interest": c.open_interest,
                            "vol_oi_ratio": hit.vol_oi_ratio,
                            "sentiment": hit.sentiment,
                            "spot": hit.spot,
                        },
                    )
                    posted += 1
                except Exception:
                    log.exception("Failed to post flow alert for %s", key)
            await asyncio.sleep(1.0)  # pace chain fetches
        if posted:
            log.info("FlowScanner: posted %d unusual-activity alerts", posted)
        return posted

    async def _loop(self) -> None:
        while self._running:
            try:
                if is_market_hours():
                    await self.scan_once()
            except Exception:
                log.exception("Unhandled error in flow scan")
            await asyncio.sleep(self.interval_minutes * 60)
