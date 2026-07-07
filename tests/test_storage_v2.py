from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.storage.database import (
    get_known_source_ids,
    get_most_active_politicians,
    get_politician_stats,
    get_price_history,
    get_purchases_since,
    insert_trades,
    kv_get,
    kv_set,
    search_politician_names,
    upsert_politicians,
    upsert_price_history,
    get_politicians,
    count_trades,
)
from src.storage.migrations import run_migrations


@pytest.fixture
async def db_path(tmp_path) -> str:
    path = str(tmp_path / "v2_test.db")
    await run_migrations(path)
    return path


def _trade(name: str, ticker: str, source_id: str, days_ago: int = 1) -> PoliticianTrade:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date()
    return PoliticianTrade(
        politician_name=name,
        chamber=Chamber.HOUSE,
        ticker=ticker,
        transaction_type=TransactionType.PURCHASE,
        transaction_date=when,
        filing_date=when,
        amount_range="$1,001 - $15,000",
        source="house_clerk",
        source_id=source_id,
    )


class TestMigrations:
    async def test_fresh_db_reaches_latest_version(self, db_path) -> None:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA user_version")
            version = (await cursor.fetchone())[0]
        assert version == 5

    async def test_rerun_is_idempotent(self, db_path) -> None:
        await run_migrations(db_path)
        assert await count_trades(db_path) == 0

    async def test_upgrades_pre_versioning_db(self, tmp_path) -> None:
        # Simulate a database created before migrations existed: base tables
        # present but user_version still 0.
        path = str(tmp_path / "old.db")
        from src.storage.database import _SCHEMA_SQL

        async with aiosqlite.connect(path) as db:
            await db.executescript(_SCHEMA_SQL)
            await db.commit()

        await run_migrations(path)
        await kv_set(path, "k", "v")  # v2 table must now exist
        assert await kv_get(path, "k") == "v"


class TestKvStore:
    async def test_get_missing_returns_none(self, db_path) -> None:
        assert await kv_get(db_path, "nope") is None

    async def test_set_and_overwrite(self, db_path) -> None:
        await kv_set(db_path, "k", "1")
        await kv_set(db_path, "k", "2")
        assert await kv_get(db_path, "k") == "2"


class TestKnownSourceIds:
    async def test_grouped_by_source(self, db_path) -> None:
        await insert_trades(
            db_path,
            [_trade("A", "AAPL", "doc1"), _trade("B", "MSFT", "doc2")],
        )
        known = await get_known_source_ids(db_path)
        assert known == {"house_clerk": {"doc1", "doc2"}}


class TestPoliticians:
    async def test_upsert_and_read(self, db_path) -> None:
        await upsert_politicians(
            db_path,
            [
                {
                    "bioguide": "P000197",
                    "full_name": "Nancy Pelosi",
                    "name_keys": '["nancy pelosi"]',
                    "party": "D",
                    "state": "CA",
                    "chamber": "house",
                    "committees": "[]",
                }
            ],
        )
        rows = await get_politicians(db_path)
        assert rows[0]["party"] == "D"

        # Upsert with new party must update, not duplicate
        await upsert_politicians(
            db_path,
            [
                {
                    "bioguide": "P000197",
                    "full_name": "Nancy Pelosi",
                    "name_keys": '["nancy pelosi"]',
                    "party": "I",
                    "state": "CA",
                    "chamber": "house",
                    "committees": "[]",
                }
            ],
        )
        rows = await get_politicians(db_path)
        assert len(rows) == 1
        assert rows[0]["party"] == "I"


class TestAnalytics:
    async def test_politician_stats_and_search(self, db_path) -> None:
        await insert_trades(
            db_path,
            [
                _trade("Jane Example", "AAPL", "d1"),
                _trade("Jane Example", "AAPL", "d2", days_ago=2),
                _trade("Jane Example", "NVDA", "d3", days_ago=3),
            ],
        )
        stats = await get_politician_stats(db_path, "jane example")
        assert stats["total"] == 3
        assert stats["buys"] == 3
        assert stats["tickers"] == 2
        assert stats["top_tickers"][0]["ticker"] == "AAPL"

        assert await search_politician_names(db_path, "jane") == ["Jane Example"]

    async def test_most_active_and_purchases(self, db_path) -> None:
        await insert_trades(
            db_path,
            [_trade("A", "AAPL", "d1"), _trade("B", "MSFT", "d2"), _trade("B", "NVDA", "d3")],
        )
        active = await get_most_active_politicians(db_path, days=7)
        assert active[0]["politician_name"] == "B"
        assert active[0]["trade_count"] == 2

        purchases = await get_purchases_since(db_path, days=7)
        assert len(purchases) == 3
        assert all(p["ticker"] for p in purchases)


class TestPriceCache:
    async def test_upsert_and_window(self, db_path) -> None:
        await upsert_price_history(db_path, "spy", {"2026-06-01": 500.0, "2026-06-02": 505.0})
        history = await get_price_history(db_path, "SPY", "2026-06-02")
        assert history == {"2026-06-02": 505.0}
