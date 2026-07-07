from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.config import settings
from src.data.models import PoliticianTrade
from src.data.parsers import amount_range_bounds

if TYPE_CHECKING:
    from src.bot.bot import TradeBot
    from src.data.enrichment import EnrichmentService
    from src.data.fetcher import MultiFetcher
    from src.trading.executor import TradeExecutor

log = logging.getLogger(__name__)

_DIGEST_KV_KEY = "last_digest_at"
_DIGEST_SECONDS = 7 * 24 * 3600


class Poller:
    """Polls data sources for new politician trades on a configurable interval."""

    def __init__(
        self,
        fetcher: MultiFetcher,
        bot: TradeBot,
        executor: TradeExecutor | None = None,
        enrichment: EnrichmentService | None = None,
        db_path: str = "trades.db",
        interval_minutes: int = 30,
    ):
        self.fetcher = fetcher
        self.bot = bot
        self.executor = executor
        self.enrichment = enrichment
        self.db_path = db_path
        self.interval_minutes = interval_minutes
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the polling loop as a background task."""
        if self._running:
            log.warning("Poller already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(f"Poller started — checking every {self.interval_minutes} minutes")

    async def stop(self):
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Poller stopped")

    async def poll_once(self) -> dict:
        """Run a single poll cycle. Returns a summary dict (also logged)."""
        from src.storage.database import count_trades, get_known_source_ids, insert_trades

        started = time.monotonic()
        summary = {"fetched": 0, "new": 0, "alerted": 0, "mirrored": 0, "backfill": False}

        # First run (empty table) means we're ingesting history, not news:
        # store everything but stay quiet so the channel isn't flooded and
        # weeks-old trades aren't mirrored.
        backfill = settings.backfill_silent and await count_trades(self.db_path) == 0
        summary["backfill"] = backfill
        if backfill:
            log.info("Empty database — running silent backfill (no alerts, no orders)")

        if self.enrichment is not None:
            try:
                await self.enrichment.ensure_fresh()
            except Exception:
                log.exception("Enrichment refresh failed — continuing without")

        # 1. Fetch trades from all sources, skipping known filings
        try:
            known_ids = await get_known_source_ids(self.db_path)
            trades = await self.fetcher.fetch_all_trades(known_ids)
        except Exception:
            log.exception("Failed to fetch trades")
            return summary
        summary["fetched"] = len(trades)

        if trades:
            if self.enrichment is not None:
                for trade in trades:
                    self.enrichment.enrich(trade)

            # 2. Insert into DB, get back only NEW trades
            new_trades = await insert_trades(self.db_path, trades)
            summary["new"] = len(new_trades)

            if new_trades and not backfill:
                # 3. Send alerts for new trades that pass the filters
                for trade in new_trades:
                    if not self._passes_alert_filters(trade):
                        continue
                    try:
                        await self.bot.send_trade_alert(trade)
                        summary["alerted"] += 1
                    except Exception:
                        log.exception(f"Failed to send alert for {trade.politician_name}")

                # 4. Execute mirror trades if enabled
                if self.executor and settings.enable_auto_trade:
                    results = await self.executor.mirror_trades(new_trades)
                    summary["mirrored"] = len(results)

        await self._maybe_post_digest()

        try:
            from src.storage.database import kv_set

            await kv_set(self.db_path, "last_poll_at", str(time.time()))
        except Exception:
            log.debug("Failed to record poll heartbeat", exc_info=True)

        duration = time.monotonic() - started
        log.info(
            "Poll cycle done in %.1fs — fetched=%d new=%d alerted=%d mirrored=%d%s",
            duration,
            summary["fetched"],
            summary["new"],
            summary["alerted"],
            summary["mirrored"],
            " (silent backfill)" if backfill else "",
        )
        return summary

    def _passes_alert_filters(self, trade: PoliticianTrade) -> bool:
        """Apply the configured channel-alert filters to one trade."""
        watchlist = settings.watchlist_tickers()
        if watchlist and trade.ticker.upper() not in watchlist:
            return False

        parties = settings.party_filter()
        if parties and trade.party and trade.party.upper() not in parties:
            return False

        if settings.alert_min_amount_usd > 0:
            bounds = amount_range_bounds(trade.amount_range)
            # Metadata-only trades (no amount) are not suppressed by the
            # amount filter — the filing itself is still news.
            if bounds is not None and bounds[0] < settings.alert_min_amount_usd:
                return False
        return True

    async def _maybe_post_digest(self) -> None:
        """Post the weekly digest when enabled and due."""
        if not settings.enable_weekly_digest:
            return
        from src.storage.database import (
            get_most_active_politicians,
            get_top_tickers,
            kv_get,
            kv_set,
        )

        try:
            last = await kv_get(self.db_path, _DIGEST_KV_KEY)
            if last and (time.time() - float(last)) < _DIGEST_SECONDS:
                return

            tickers = await get_top_tickers(self.db_path, days=7)
            politicians = await get_most_active_politicians(self.db_path, days=7)
            if not tickers and not politicians:
                return
            await self.bot.send_weekly_digest(tickers, politicians)
            await kv_set(self.db_path, _DIGEST_KV_KEY, str(time.time()))
        except Exception:
            log.exception("Failed to post weekly digest")

    async def _poll_loop(self):
        """Internal polling loop."""
        while self._running:
            try:
                await self.poll_once()
            except Exception:
                log.exception("Unhandled error in poll cycle")

            await asyncio.sleep(self.interval_minutes * 60)
