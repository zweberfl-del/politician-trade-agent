"""Background scanner for unusual prediction-market activity.

Two layers over Polymarket's public trade feed:

- **per-bet** (``scan_once``): profiles the wallet behind each large fill and
  alerts on the combinations that read as informed money — fresh wallets,
  proven winners, longshot conviction, whale size. Every large fill is also
  logged to ``prediction_trades`` so the surge layer can see the crowd.
- **surge** (``scan_surges``): the pattern 60 Minutes surfaced — many wallets
  converging one-sided on the same military/geopolitical outcome inside a
  rolling window. Detected wallets are clustered behaviorally (co-betting) and,
  when a Polygonscan key is set, by shared on-chain funder.

Runs continuously — prediction markets don't keep equity hours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from src.analysis.clusters import (
    CooccurrenceEvent,
    annotate_surge_with_clusters,
    cooccurrence_clusters,
    funder_clusters,
)
from src.analysis.prediction import classify_bet
from src.analysis.surge import SurgeTrade, detect_surges
from src.config import settings
from src.data.categories import classify_market

if TYPE_CHECKING:
    from src.bot.bot import TradeBot
    from src.data.polygonscan import PolygonscanClient
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
        polygonscan: PolygonscanClient | None = None,
    ) -> None:
        self.client = client
        self.bot = bot
        self.db_path = db_path
        self.interval_minutes = interval_minutes or settings.prediction_poll_minutes
        self.polygonscan = polygonscan or self._maybe_polygonscan()
        self._running = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _maybe_polygonscan() -> PolygonscanClient | None:
        if not settings.polygonscan_api_key:
            return None
        from src.data.polygonscan import PolygonscanClient

        return PolygonscanClient(settings.polygonscan_api_key)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(
            "PredictionScanner started — bets >= $%.0f every %d min (surges: %s)",
            settings.prediction_min_bet_usd,
            self.interval_minutes,
            "on" if settings.enable_surge_alerts else "off",
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
        """One pass over the public trade feed; returns per-bet alerts posted."""
        from src.storage.database import (
            record_prediction_event,
            record_prediction_trade,
            was_prediction_alerted,
        )

        trades = await self.client.get_recent_trades()
        candidates = [
            t for t in trades if t.usd_size >= settings.prediction_min_bet_usd
        ]

        # Log every large fill (not just alerted ones) so surges can be
        # reconstructed over a 24h window. Idempotent on event_key.
        for trade in candidates:
            await record_prediction_trade(
                self.db_path,
                {
                    "event_key": trade.event_key,
                    "wallet": trade.wallet,
                    "market_slug": trade.market_slug,
                    "market_title": trade.market_title,
                    "market_url": trade.market_url,
                    "outcome": trade.outcome,
                    "side": trade.side,
                    "usd_size": trade.usd_size,
                    "price": trade.price,
                    "category": classify_market(trade.market_title, trade.market_slug),
                    "ts": trade.timestamp,
                },
            )

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

    async def scan_surges(self) -> int:
        """Detect and alert insider-style surges over the rolling window."""
        from src.storage.database import (
            get_recent_prediction_trades,
            record_surge,
            was_surge_alerted,
        )

        now = time.time()
        window = int(settings.surge_window_hours * 3600)
        rows = await get_recent_prediction_trades(self.db_path, int(now - window))
        if not rows:
            return 0

        surge_trades = [
            SurgeTrade(
                wallet=r["wallet"],
                market_slug=r["market_slug"] or "",
                market_title=r["market_title"] or "",
                market_url=r["market_url"] or "",
                outcome=r["outcome"] or "",
                side=r["side"] or "",
                usd_size=r["usd_size"] or 0.0,
                timestamp=int(r["ts"] or 0),
                category=r["category"] or "",
            )
            for r in rows
        ]

        # First pass without profiles finds the qualifying, un-alerted surges.
        candidates = detect_surges(
            surge_trades,
            {},
            window_seconds=window,
            min_wallets=settings.surge_min_wallets,
            min_total_usd=settings.surge_min_total_usd,
            min_buy_ratio=settings.surge_min_buy_ratio,
            now=now,
        )
        new_keys = {
            s.surge_key for s in candidates if not await was_surge_alerted(self.db_path, s.surge_key)
        }
        if not new_keys:
            return 0

        # Profile a bounded set of the wallets driving the new surges.
        to_profile: list[str] = []
        for s in candidates:
            if s.surge_key not in new_keys:
                continue
            for w in s.top_wallets:
                if w not in to_profile:
                    to_profile.append(w)
        to_profile = to_profile[:_MAX_PROFILES_PER_CYCLE]

        profiles = {}
        for w in to_profile:
            profiles[w] = await self.client.get_wallet_profile(w)
            await asyncio.sleep(0.5)

        # Behavioral clusters from the whole window; optional on-chain funder graph.
        cooc = [
            CooccurrenceEvent(
                wallet=t.wallet, market_slug=t.market_slug,
                outcome=t.outcome, timestamp=t.timestamp,
            )
            for t in surge_trades if t.side == "BUY"
        ]
        clusters = cooccurrence_clusters(
            cooc,
            window_seconds=settings.cluster_window_minutes * 60,
            min_shared=settings.cluster_min_shared,
        )
        if self.polygonscan and to_profile:
            try:
                funding = await self.polygonscan.funding_map(to_profile)
                clusters = clusters + funder_clusters(funding)
            except Exception:
                log.warning("Polygonscan funding lookup failed", exc_info=True)

        # Re-detect with profiles so fresh/sharp counts and scores are populated.
        enriched = detect_surges(
            surge_trades,
            profiles,
            window_seconds=window,
            min_wallets=settings.surge_min_wallets,
            min_total_usd=settings.surge_min_total_usd,
            min_buy_ratio=settings.surge_min_buy_ratio,
            now=now,
        )

        posted = 0
        for s in enriched:
            if s.surge_key not in new_keys:
                continue
            s.cluster_note = annotate_surge_with_clusters(s.top_wallets, clusters)
            try:
                await self.bot.send_surge_alert(s)
                await record_surge(
                    self.db_path,
                    {
                        "surge_key": s.surge_key,
                        "market_title": s.market_title,
                        "market_url": s.market_url,
                        "market_slug": s.market_slug,
                        "outcome": s.outcome,
                        "category": s.category,
                        "window_start": s.window_start,
                        "window_end": s.window_end,
                        "wallet_count": s.wallet_count,
                        "total_usd": s.total_usd,
                        "buy_ratio": s.buy_ratio,
                        "fresh_count": s.fresh_count,
                        "sharp_count": s.sharp_count,
                        "cluster_note": s.cluster_note,
                        "score": s.score,
                        "reasons": json.dumps(s.reasons),
                    },
                )
                posted += 1
            except Exception:
                log.exception("Failed to post surge alert for %s", s.surge_key)

        if posted:
            log.info("PredictionScanner: posted %d insider-surge alerts", posted)
        return posted

    async def _loop(self) -> None:
        from src.storage.database import kv_set

        while self._running:
            try:
                await self.scan_once()
                if settings.enable_surge_alerts:
                    await self.scan_surges()
                await kv_set(self.db_path, "last_prediction_scan_at", str(time.time()))
            except Exception:
                log.exception("Unhandled error in prediction scan")
            await asyncio.sleep(self.interval_minutes * 60)
