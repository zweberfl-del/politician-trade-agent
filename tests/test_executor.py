from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.storage.database import init_db
from src.trading.base import Broker, OrderResult
from src.trading.executor import TradeExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(
    ticker: str = "AAPL",
    txn_type: TransactionType = TransactionType.PURCHASE,
    name: str = "Nancy Pelosi",
) -> PoliticianTrade:
    return PoliticianTrade(
        politician_name=name,
        chamber=Chamber.HOUSE,
        ticker=ticker,
        transaction_type=txn_type,
        transaction_date=date(2024, 6, 15),
        amount_range="$1,001 - $15,000",
    )


def _make_order(ticker: str = "AAPL", side: str = "buy") -> OrderResult:
    return OrderResult(
        order_id="order-123",
        ticker=ticker,
        side=side,
        quantity=5.0,
        status="submitted",
    )


class MockBroker(Broker):
    """A broker that records calls and returns canned results."""

    def __init__(self) -> None:
        self.buy = AsyncMock(return_value=_make_order("AAPL", "buy"))
        self.sell = AsyncMock(return_value=_make_order("AAPL", "sell"))
        self.get_positions = AsyncMock(return_value=[])
        self.get_account = AsyncMock()


@pytest.fixture
async def executor(tmp_path) -> tuple[TradeExecutor, MockBroker]:
    db_path = str(tmp_path / "exec_test.db")
    await init_db(db_path)
    broker = MockBroker()
    return TradeExecutor(broker=broker, db_path=db_path), broker


# ---------------------------------------------------------------------------
# mirror_trade — skip conditions
# ---------------------------------------------------------------------------


class TestMirrorTradeSkips:
    async def test_skips_no_ticker(self, executor) -> None:
        exe, broker = executor
        trade = _make_trade(ticker="")
        result = await exe.mirror_trade(trade)
        assert result is None
        broker.buy.assert_not_called()
        broker.sell.assert_not_called()

    async def test_skips_unknown_transaction_type(self, executor) -> None:
        exe, broker = executor
        trade = _make_trade(txn_type=TransactionType.UNKNOWN)
        result = await exe.mirror_trade(trade)
        assert result is None
        broker.buy.assert_not_called()

    async def test_skips_sale_when_sell_mirror_disabled(self, executor, monkeypatch) -> None:
        exe, broker = executor
        from src import config
        monkeypatch.setattr(config.settings, "enable_sell_mirror", False)

        trade = _make_trade(txn_type=TransactionType.SALE)
        result = await exe.mirror_trade(trade)
        assert result is None
        broker.sell.assert_not_called()


# ---------------------------------------------------------------------------
# mirror_trade — execution paths
# ---------------------------------------------------------------------------


class TestMirrorTradeExecutes:
    async def test_purchase_calls_buy(self, executor) -> None:
        exe, broker = executor
        trade = _make_trade(txn_type=TransactionType.PURCHASE, ticker="MSFT")
        broker.buy.return_value = _make_order("MSFT", "buy")

        result = await exe.mirror_trade(trade)
        assert result is not None
        assert result.side == "buy"
        broker.buy.assert_awaited_once()

    async def test_sale_calls_sell_when_enabled(self, executor, monkeypatch) -> None:
        exe, broker = executor
        from src import config
        monkeypatch.setattr(config.settings, "enable_sell_mirror", True)

        trade = _make_trade(txn_type=TransactionType.SALE, ticker="TSLA")
        broker.sell.return_value = _make_order("TSLA", "sell")

        result = await exe.mirror_trade(trade)
        assert result is not None
        assert result.side == "sell"
        broker.sell.assert_awaited_once()

    async def test_exchange_treated_as_buy(self, executor) -> None:
        exe, broker = executor
        trade = _make_trade(txn_type=TransactionType.EXCHANGE)
        result = await exe.mirror_trade(trade)
        assert result is not None
        broker.buy.assert_awaited_once()

    async def test_already_executed_trade_skipped(self, executor) -> None:
        exe, broker = executor
        trade = _make_trade()
        # Execute once
        await exe.mirror_trade(trade)
        broker.buy.reset_mock()

        # Second attempt should be skipped
        result = await exe.mirror_trade(trade)
        assert result is None
        broker.buy.assert_not_called()

    async def test_broker_error_returns_failed_result(self, executor) -> None:
        exe, broker = executor
        broker.buy.side_effect = RuntimeError("Connection lost")
        trade = _make_trade()
        result = await exe.mirror_trade(trade)
        assert result is not None
        assert result.status == "failed"
        assert "Connection lost" in (result.error or "")


# ---------------------------------------------------------------------------
# mirror_trades — batch processing
# ---------------------------------------------------------------------------


class TestMirrorTradesBatch:
    async def test_processes_batch(self, executor) -> None:
        exe, broker = executor
        trades = [
            _make_trade(name="A", ticker="AAPL"),
            _make_trade(name="B", ticker="MSFT"),
        ]
        broker.buy.return_value = _make_order()
        results = await exe.mirror_trades(trades)
        assert len(results) == 2

    async def test_skipped_trades_excluded_from_results(self, executor) -> None:
        exe, broker = executor
        trades = [
            _make_trade(ticker=""),           # skipped: no ticker
            _make_trade(ticker="AAPL"),       # executed
        ]
        results = await exe.mirror_trades(trades)
        assert len(results) == 1

    async def test_continues_after_individual_failure(self, executor) -> None:
        exe, broker = executor

        call_count = 0

        async def buy_side_effect(ticker, amount):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Temporary failure")
            return _make_order(ticker, "buy")

        broker.buy.side_effect = buy_side_effect

        trades = [
            _make_trade(name="A", ticker="FAIL"),
            _make_trade(name="B", ticker="WORK"),
        ]
        results = await exe.mirror_trades(trades)
        # Both trades were attempted — first fails (returns failed result),
        # second succeeds
        assert len(results) == 2

    async def test_empty_batch(self, executor) -> None:
        exe, broker = executor
        results = await exe.mirror_trades([])
        assert results == []
