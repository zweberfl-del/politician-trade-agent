from __future__ import annotations

import argparse
import asyncio
import logging
import sys

log = logging.getLogger("politician-trade-agent")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Quiet noisy libraries
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_once() -> None:
    """Fetch + store one cycle without Discord or trading (smoke test / cron)."""
    from src.config import settings
    from src.data.enrichment import EnrichmentService
    from src.data.fetcher import build_default_fetcher
    from src.storage.database import get_known_source_ids, insert_trades
    from src.storage.migrations import run_migrations

    await run_migrations(settings.database_path)
    fetcher = build_default_fetcher()

    if settings.enable_enrichment:
        enrichment = EnrichmentService(settings.database_path)
        await enrichment.ensure_fresh()
    else:
        enrichment = None

    known_ids = await get_known_source_ids(settings.database_path)
    trades = await fetcher.fetch_all_trades(known_ids)
    if enrichment is not None:
        for trade in trades:
            enrichment.enrich(trade)
    new_trades = await insert_trades(settings.database_path, trades)

    log.info("--once complete: fetched=%d new=%d", len(trades), len(new_trades))
    for trade in new_trades[:20]:
        log.info(
            "  NEW %s | %s | %s | %s | %s",
            trade.politician_name,
            trade.ticker or "(no ticker)",
            trade.transaction_type.value,
            trade.amount_range or "(no amount)",
            trade.filing_date,
        )


async def run():
    from src.config import settings
    from src.storage.migrations import run_migrations
    from src.data.enrichment import EnrichmentService
    from src.data.fetcher import build_default_fetcher
    from src.bot.bot import TradeBot
    from src.data.poller import Poller

    # 1. Initialize database
    await run_migrations(settings.database_path)
    log.info(f"Database initialized at {settings.database_path}")

    # 2. Build data fetcher
    fetcher = build_default_fetcher()

    # 3. Build broker (if trading enabled)
    broker = None
    executor = None
    if settings.enable_auto_trade:
        try:
            from src.trading.alpaca_broker import AlpacaBroker
            from src.trading.executor import TradeExecutor
            broker = AlpacaBroker()
            executor = TradeExecutor(broker=broker, db_path=settings.database_path)
            log.info("Auto-trading enabled with Alpaca broker")
        except Exception:
            log.exception("Failed to initialize broker — auto-trading disabled")

    # 4. Politician metadata enrichment (party/state/committees)
    enrichment = (
        EnrichmentService(settings.database_path) if settings.enable_enrichment else None
    )

    # 5. Create bot (broker for /portfolio, enrichment for /politician)
    bot = TradeBot(broker=broker, enrichment=enrichment)

    # 6. Create poller
    poller = Poller(
        fetcher=fetcher,
        bot=bot,
        executor=executor,
        enrichment=enrichment,
        db_path=settings.database_path,
        interval_minutes=settings.poll_interval_minutes,
    )

    # 7. Options flow scanner (unusual-activity alerts)
    flow_scanner = None
    if settings.enable_flow_alerts:
        from src.data.flow_scanner import FlowScanner
        from src.data.options import build_options_provider

        flow_scanner = FlowScanner(
            provider=build_options_provider(),
            bot=bot,
            db_path=settings.database_path,
        )

    # 8. Web dashboard
    dashboard = None
    if settings.enable_dashboard:
        from src.web.dashboard import DashboardServer

        dashboard = DashboardServer(settings.database_path)

    # 9. Start background tasks when bot is ready
    original_on_ready = bot.on_ready

    async def on_ready_with_poller():
        await original_on_ready()
        await poller.start()
        if flow_scanner is not None:
            await flow_scanner.start()
        if dashboard is not None:
            await dashboard.start()

    bot.on_ready = on_ready_with_poller

    # 10. Validate config
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN is required. Set it in .env")
        sys.exit(1)

    # 11. Run the bot (blocks until shutdown)
    log.info("Starting bot...")
    try:
        await bot.start(settings.discord_bot_token)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        await poller.stop()
        if flow_scanner is not None:
            await flow_scanner.stop()
        if dashboard is not None:
            await dashboard.stop()
        await bot.close()


def main():
    parser = argparse.ArgumentParser(prog="politician-trade-agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single fetch+store cycle without Discord or trading, then exit",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        asyncio.run(run_once() if args.once else run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
