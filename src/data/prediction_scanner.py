"""Background scanner for unusual prediction-market activity.

Polls Polymarket's public trade feed, profiles the wallets behind large
bets (account age, activity count, win record), and alerts on the
combinations that read as informed money: fresh wallets sizing in, proven
winners sizing in, and conviction bets on longshots. Runs continuously —
prediction markets don't keep equity hours.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from src.analysis.prediction import classify_bet
from src.config import settings

if TYPE_CHECKING:
    from src.bot.bot import TradeBot
    from src.data.polymarket import PolymarketClient

log = logging.getLogger(__name__)

# Wallet profiling costs two API calls; bound the candidates per cycle.
_MAX_PROFILES_PER_CYCLE = 15


class PredictionScanner:
    def __init__(
        self,
        client: PolymarketClient,
        bot: TradeBot,
        db_path: str,
        interval_minutes: int | None = None,
    ) -> None:
        self.client = client
        self.bot = bot
        self.db_path = db_path
        self.interval_minutes = interval_minutes or settings.prediction_poll_minutes
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(
            "PredictionScanner started — bets >= $%.0f every %d min",
            settings.prediction_min_bet_usd,
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
        """One pass over the public trade feed; returns alerts posted."""
        from src.storage.database import record_prediction_event, was_prediction_alerted

        trades = await self.client.get_recent_trades()
        candidates = [
            t for t in trades if t.usd_size >= settings.prediction_min_bet_usd
        ]

        posted = 0
        profiled = 0
        for trade in candidates:
            if await was_prediction_alerted(self.db_path, trade.event_key):
                continue
            if profiled >= _MAX_PROFILES_PER_CYCLE:
                break
            profiled += 1
            profile = await self.client.get_wallet_profile(trade.wallet)
            bet = classify_bet(
                trade, profile, min_bet_usd=settings.prediction_min_bet_usd
            )
            await asyncio.sleep(0.5)  # pace the API
            if bet is None:
                continue
            try:
                await self.bot.send_prediction_alert(bet)
                await record_prediction_event(
                    self.db_path,
                    {
                        "event_key": trade.event_key,
                        "wallet": trade.wallet,
                        "pseudonym": trade.pseudonym,
                        "market_title": trade.market_title,
                        "market_url": trade.market_url,
                        "outcome": trade.outcome,
                        "side": trade.side,
                        "usd_size": trade.usd_size,
                        "price": trade.price,
                        "signals": json.dumps(bet.signals),
                        "wallet_age_hours": profile.age_hours(),
                        "wallet_win_rate": profile.win_rate,
                        "wallet_pnl_usd": profile.total_pnl_usd,
                    },
                )
                posted += 1
            except Exception:
                log.exception("Failed to post prediction alert for %s", trade.event_key)

        if posted:
            log.info("PredictionScanner: posted %d unusual-bet alerts", posted)
        return posted

    async def _loop(self) -> None:
        import time

        from src.storage.database import kv_set

        while self._running:
            try:
                await self.scan_once()
                await kv_set(self.db_path, "last_prediction_scan_at", str(time.time()))
            except Exception:
                log.exception("Unhandled error in prediction scan")
            await asyncio.sleep(self.interval_minutes * 60)
