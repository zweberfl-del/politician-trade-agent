from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.bot.bot import TradeBot
    from src.data.fetcher import MultiFetcher
    from src.trading.executor import TradeExecutor

log = logging.getLogger(__name__)


class Poller:
    """Polls data sources for new politician trades on a configurable interval."""

    def __init__(
        self,
        fetcher: MultiFetcher,
        bot: TradeBot,
        executor: TradeExecutor | None = None,
        db_path: str = "trades.db",
        interval_minutes: int = 30,
    ):
        self.fetcher = fetcher
        self.bot = bot
        self.executor = executor
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

    async def poll_once(self):
        """Run a single poll cycle. Useful for manual triggers."""
        log.info("Starting poll cycle...")

        # 1. Fetch trades from all sources
        try:
            trades = await self.fetcher.fetch_all_trades()
        except Exception:
            log.exception("Failed to fetch trades")
            return

        if not trades:
            log.info("No trades fetched this cycle")
            return

        log.info(f"Fetched {len(trades)} trades from sources")

        # 2. Insert into DB, get back only NEW trades
        from src.storage.database import insert_trades
        new_trades = await insert_trades(self.db_path, trades)

        if not new_trades:
            log.info("No new trades this cycle")
            return

        log.info(f"Found {len(new_trades)} NEW trades")

        # 3. Send alerts for new trades
        for trade in new_trades:
            try:
                await self.bot.send_trade_alert(trade)
            except Exception:
                log.exception(f"Failed to send alert for {trade.politician_name}")

        # 4. Execute mirror trades if enabled
        if self.executor:
            from src.config import settings
            if settings.enable_auto_trade:
                results = await self.executor.mirror_trades(new_trades)
                log.info(f"Executed {len(results)} mirror trades")

    async def _poll_loop(self):
        """Internal polling loop."""
        while self._running:
            try:
                await self.poll_once()
            except Exception:
                log.exception("Unhandled error in poll cycle")

            await asyncio.sleep(self.interval_minutes * 60)
