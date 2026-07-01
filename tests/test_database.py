from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.storage.database import (
    follow_politician,
    get_followed_politicians,
    get_followers,
    get_recent_trades,
    get_top_tickers,
    init_db,
    insert_trade,
    insert_trades,
    is_trade_seen,
    unfollow_politician,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(
    name: str = "Nancy Pelosi",
    ticker: str = "AAPL",
    txn_type: TransactionType = TransactionType.PURCHASE,
    txn_date: date | None = None,
    amount: str = "$1,001 - $15,000",
) -> PoliticianTrade:
    return PoliticianTrade(
        politician_name=name,
        chamber=Chamber.HOUSE,
        ticker=ticker,
        transaction_type=txn_type,
        transaction_date=txn_date or date.today(),
        amount_range=amount,
    )


@pytest.fixture
async def db(tmp_path) -> str:
    """Return a path to an initialised temp database."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


class TestInitDb:
    async def test_creates_tables(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh.db")
        result = await init_db(db_path)
        assert result == db_path

        # Inserting a trade should work without error (table exists)
        trade = _make_trade()
        await insert_trade(db_path, trade)
        assert await is_trade_seen(db_path, trade.trade_id)

    async def test_idempotent(self, tmp_path) -> None:
        """Calling init_db twice should not raise."""
        db_path = str(tmp_path / "double.db")
        await init_db(db_path)
        await init_db(db_path)  # second call must not error


# ---------------------------------------------------------------------------
# insert_trade / is_trade_seen
# ---------------------------------------------------------------------------


class TestInsertAndSeen:
    async def test_insert_then_seen(self, db: str) -> None:
        trade = _make_trade()
        assert not await is_trade_seen(db, trade.trade_id)

        await insert_trade(db, trade)
        assert await is_trade_seen(db, trade.trade_id)

    async def test_duplicate_insert_is_silent(self, db: str) -> None:
        trade = _make_trade()
        await insert_trade(db, trade)
        await insert_trade(db, trade)  # should not raise

        # Still only one row — verified via get_recent_trades count
        rows = await get_recent_trades(db, limit=100)
        matching = [r for r in rows if r["trade_id"] == trade.trade_id]
        assert len(matching) == 1

    async def test_unseen_trade_returns_false(self, db: str) -> None:
        assert not await is_trade_seen(db, "nonexistent|id")


# ---------------------------------------------------------------------------
# insert_trades (batch, deduplication)
# ---------------------------------------------------------------------------


class TestInsertTrades:
    async def test_returns_only_new(self, db: str) -> None:
        t1 = _make_trade(name="Pelosi", ticker="AAPL")
        t2 = _make_trade(name="Pelosi", ticker="MSFT")

        # First insert — both are new
        new = await insert_trades(db, [t1, t2])
        assert len(new) == 2

        # Second insert with one new and one duplicate
        t3 = _make_trade(name="Pelosi", ticker="GOOGL")
        new2 = await insert_trades(db, [t1, t3])
        assert len(new2) == 1
        assert new2[0].ticker == "GOOGL"

    async def test_empty_list(self, db: str) -> None:
        result = await insert_trades(db, [])
        assert result == []

    async def test_all_duplicates(self, db: str) -> None:
        t1 = _make_trade()
        await insert_trades(db, [t1])
        result = await insert_trades(db, [t1])
        assert result == []


# ---------------------------------------------------------------------------
# get_recent_trades
# ---------------------------------------------------------------------------


class TestGetRecentTrades:
    async def test_returns_dicts(self, db: str) -> None:
        await insert_trade(db, _make_trade())
        rows = await get_recent_trades(db)
        assert len(rows) == 1
        assert isinstance(rows[0], dict)
        assert rows[0]["ticker"] == "AAPL"

    async def test_filter_by_politician(self, db: str) -> None:
        await insert_trades(db, [
            _make_trade(name="Nancy Pelosi", ticker="AAPL"),
            _make_trade(name="Dan Crenshaw", ticker="MSFT"),
        ])
        rows = await get_recent_trades(db, politician="Nancy Pelosi")
        assert len(rows) == 1
        assert rows[0]["politician_name"] == "Nancy Pelosi"

    async def test_filter_by_ticker(self, db: str) -> None:
        await insert_trades(db, [
            _make_trade(name="Pelosi", ticker="AAPL"),
            _make_trade(name="Crenshaw", ticker="MSFT"),
        ])
        rows = await get_recent_trades(db, ticker="MSFT")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "MSFT"

    async def test_limit(self, db: str) -> None:
        trades = [_make_trade(name=f"Politician {i}", ticker="AAPL") for i in range(5)]
        await insert_trades(db, trades)
        rows = await get_recent_trades(db, limit=3)
        assert len(rows) == 3

    async def test_no_results(self, db: str) -> None:
        rows = await get_recent_trades(db, politician="Nobody")
        assert rows == []


# ---------------------------------------------------------------------------
# get_top_tickers
# ---------------------------------------------------------------------------


class TestGetTopTickers:
    async def test_basic(self, db: str) -> None:
        today = date.today()
        trades = [
            _make_trade(name="A", ticker="AAPL", txn_date=today),
            _make_trade(name="B", ticker="AAPL", txn_date=today),
            _make_trade(name="C", ticker="MSFT", txn_date=today),
        ]
        await insert_trades(db, trades)
        top = await get_top_tickers(db, days=7, limit=10)

        # AAPL should come first with 2 trades
        assert len(top) >= 1
        assert top[0]["ticker"] == "AAPL"
        assert top[0]["trade_count"] == 2

    async def test_excludes_empty_ticker(self, db: str) -> None:
        await insert_trade(db, _make_trade(ticker=""))
        top = await get_top_tickers(db, days=7)
        assert len(top) == 0

    async def test_respects_day_window(self, db: str) -> None:
        old_date = date.today() - timedelta(days=60)
        await insert_trade(db, _make_trade(ticker="OLD", txn_date=old_date))
        await insert_trade(db, _make_trade(ticker="NEW", txn_date=date.today(), name="X"))

        top = await get_top_tickers(db, days=30)
        tickers = [r["ticker"] for r in top]
        assert "NEW" in tickers
        assert "OLD" not in tickers


# ---------------------------------------------------------------------------
# follow / unfollow / get_followed / get_followers
# ---------------------------------------------------------------------------


class TestFollowing:
    async def test_follow_and_list(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        names = await get_followed_politicians(db, "user1")
        assert names == ["Nancy Pelosi"]

    async def test_follow_multiple(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        await follow_politician(db, "user1", "Dan Crenshaw")
        names = await get_followed_politicians(db, "user1")
        assert sorted(names) == ["Dan Crenshaw", "Nancy Pelosi"]

    async def test_follow_idempotent(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        await follow_politician(db, "user1", "Nancy Pelosi")
        names = await get_followed_politicians(db, "user1")
        assert names == ["Nancy Pelosi"]

    async def test_unfollow(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        await unfollow_politician(db, "user1", "Nancy Pelosi")
        names = await get_followed_politicians(db, "user1")
        assert names == []

    async def test_unfollow_nonexistent_is_silent(self, db: str) -> None:
        await unfollow_politician(db, "user1", "Nobody")  # should not raise

    async def test_get_followers(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        await follow_politician(db, "user2", "Nancy Pelosi")
        await follow_politician(db, "user3", "Dan Crenshaw")

        followers = await get_followers(db, "Nancy Pelosi")
        assert sorted(followers) == ["user1", "user2"]

    async def test_get_followers_case_insensitive(self, db: str) -> None:
        await follow_politician(db, "user1", "Nancy Pelosi")
        followers = await get_followers(db, "nancy pelosi")
        assert followers == ["user1"]

    async def test_empty_following(self, db: str) -> None:
        names = await get_followed_politicians(db, "nobody")
        assert names == []
