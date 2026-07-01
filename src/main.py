from __future__ import annotations

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


async def run():
    from src.config import settings
    from src.storage.migrations import run_migrations
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

    # 4. Create bot (pass broker so /portfolio command has access)
    bot = TradeBot(broker=broker)

    # 5. Create poller
    poller = Poller(
        fetcher=fetcher,
        bot=bot,
        executor=executor,
        db_path=settings.database_path,
        interval_minutes=settings.poll_interval_minutes,
    )

    # 6. Start poller when bot is ready
    original_on_ready = bot.on_ready

    async def on_ready_with_poller():
        await original_on_ready()
        await poller.start()

    bot.on_ready = on_ready_with_poller

    # 7. Validate config
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN is required. Set it in .env")
        sys.exit(1)

    # 8. Run the bot (blocks until shutdown)
    log.info("Starting bot...")
    try:
        await bot.start(settings.discord_bot_token)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        await poller.stop()
        await bot.close()


def main():
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
