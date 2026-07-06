"""End-to-end pipeline simulation: sources → dedup → alerts → mirroring.

Exercises Poller.poll_once against a real SQLite database and a real
TradeExecutor, with the network (DataSource) and Discord (bot) boundaries
faked — i.e. the production flow minus external services.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.data.fetcher import DataSource, MultiFetcher
from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.data.poller import Poller
from src.storage.migrations import run_migrations
from src.trading.base import Broker, OrderResult
from src.trading.executor import TradeExecutor


class ScriptedSource(DataSource):
    """Returns a preset list of trades and records the known_ids it was given."""

    name = "house_clerk"

    def __init__(self) -> None:
        self.trades: list[PoliticianTrade] = []
        self.seen_known_ids: list[set[str]] = []

    async def fetch_trades(self, known_ids: set[str] | None = None) -> list[PoliticianTrade]:
        self.seen_known_ids.append(set(known_ids or set()))
        # Mimic the real sources: skip filings already in the database.
        return [t for t in self.trades if t.source_id not in (known_ids or set())]


class FakeBot:
    def __init__(self) -> None:
        self.alerts: list[PoliticianTrade] = []
        self.digests: list[tuple] = []

    async def send_trade_alert(self, trade: PoliticianTrade) -> None:
        self.alerts.append(trade)

    async def send_weekly_digest(self, top_tickers, most_active) -> None:
        self.digests.append((top_tickers, most_active))


class FakeBroker(Broker):
    async def buy(self, ticker: str, amount_usd: float) -> OrderResult:
        raise NotImplementedError

    async def sell(self, ticker: str, amount_usd: float) -> OrderResult:
        raise NotImplementedError

    async def get_positions(self):
        raise NotImplementedError

    async def get_account(self):
        raise NotImplementedError

    def __init__(self) -> None:
        self.buy = AsyncMock(
            return_value=OrderResult(
                order_id="o1", ticker="X", side="buy", quantity=1.0, status="submitted"
            )
        )
        self.sell = AsyncMock()
        self.get_positions = AsyncMock(return_value=[])
        self.get_account = AsyncMock()


def _trade(name: str, ticker: str, source_id: str) -> PoliticianTrade:
    return PoliticianTrade(
        politician_name=name,
        chamber=Chamber.HOUSE,
        ticker=ticker,
        transaction_type=TransactionType.PURCHASE,
        transaction_date=date(2026, 6, 15),
        filing_date=date(2026, 6, 20),
        amount_range="$15,001 - $50,000",
        source="house_clerk",
        source_id=source_id,
    )


@pytest.fixture
async def pipeline(tmp_path, monkeypatch):
    from src import config

    db_path = str(tmp_path / "pipeline.db")
    await run_migrations(db_path)

    monkeypatch.setattr(config.settings, "enable_alerts", True)
    monkeypatch.setattr(config.settings, "alert_channel_id", 123)
    monkeypatch.setattr(config.settings, "enable_auto_trade", True)
    monkeypatch.setattr(config.settings, "backfill_silent", True)
    monkeypatch.setattr(config.settings, "enable_weekly_digest", False)
    monkeypatch.setattr(config.settings, "alert_ticker_watchlist", "")
    monkeypatch.setattr(config.settings, "alert_party_filter", "")
    monkeypatch.setattr(config.settings, "alert_min_amount_usd", 0.0)

    source = ScriptedSource()
    bot = FakeBot()
    broker = FakeBroker()
    executor = TradeExecutor(broker=broker, db_path=db_path)
    poller = Poller(
        fetcher=MultiFetcher([source]),
        bot=bot,
        executor=executor,
        db_path=db_path,
    )
    return poller, source, bot, broker, db_path


class TestProductionPipeline:
    async def test_first_cycle_backfills_silently(self, pipeline) -> None:
        poller, source, bot, broker, _ = pipeline
        source.trades = [_trade("Jane Example", "AAPL", "doc1")]

        summary = await poller.poll_once()

        assert summary["backfill"] is True
        assert summary["new"] == 1
        assert bot.alerts == []           # no channel flood
        broker.buy.assert_not_called()    # no stale mirror orders

    async def test_second_cycle_alerts_and_mirrors_only_new(self, pipeline) -> None:
        poller, source, bot, broker, _ = pipeline
        source.trades = [_trade("Jane Example", "AAPL", "doc1")]
        await poller.poll_once()  # backfill

        source.trades.append(_trade("John Sample", "NVDA", "doc2"))
        summary = await poller.poll_once()

        assert summary["backfill"] is False
        assert summary["new"] == 1
        assert [t.ticker for t in bot.alerts] == ["NVDA"]
        broker.buy.assert_awaited_once_with("NVDA", 500.0)
        assert summary["mirrored"] == 1

        # Sources received the already-known filing IDs
        assert "doc1" in source.seen_known_ids[-1]

    async def test_restart_does_not_realert_or_reorder(self, pipeline) -> None:
        poller, source, bot, broker, db_path = pipeline
        source.trades = [_trade("Jane Example", "AAPL", "doc1")]
        await poller.poll_once()
        source.trades.append(_trade("John Sample", "NVDA", "doc2"))
        await poller.poll_once()
        broker.buy.reset_mock()
        bot.alerts.clear()

        # Simulate a process restart: fresh Poller/executor on the same DB.
        executor = TradeExecutor(broker=broker, db_path=db_path)
        restarted = Poller(
            fetcher=MultiFetcher([source]), bot=bot, executor=executor, db_path=db_path
        )
        summary = await restarted.poll_once()

        assert summary["new"] == 0
        assert bot.alerts == []
        broker.buy.assert_not_called()

    async def test_watchlist_filter_blocks_alert_but_not_storage(
        self, pipeline, monkeypatch
    ) -> None:
        from src import config

        poller, source, bot, broker, _ = pipeline
        source.trades = [_trade("Jane Example", "AAPL", "doc1")]
        await poller.poll_once()  # backfill

        monkeypatch.setattr(config.settings, "alert_ticker_watchlist", "NVDA")
        source.trades.append(_trade("John Sample", "MSFT", "doc2"))
        summary = await poller.poll_once()

        assert summary["new"] == 1        # stored
        assert bot.alerts == []           # but not alerted (off-watchlist)
        broker.buy.assert_awaited_once()  # mirroring is not affected by alert filters
