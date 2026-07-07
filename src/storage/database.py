from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from src.data.models import PoliticianTrade

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id           TEXT UNIQUE,
    politician_name    TEXT,
    chamber            TEXT,
    state              TEXT,
    party              TEXT,
    ticker             TEXT,
    asset_name         TEXT,
    transaction_type   TEXT,
    transaction_date   TEXT,
    filing_date        TEXT,
    amount_range       TEXT,
    owner              TEXT,
    source             TEXT,
    source_id          TEXT,
    filing_url         TEXT,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS followed_politicians (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id    TEXT,
    politician_name    TEXT,
    UNIQUE(discord_user_id, politician_name)
);

CREATE TABLE IF NOT EXISTS executed_trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id           TEXT,
    broker             TEXT,
    order_id           TEXT,
    ticker             TEXT,
    side               TEXT,
    quantity           REAL,
    amount_usd         REAL,
    status             TEXT,
    submitted_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    filled_at          TEXT,
    error_message      TEXT
);
"""

# Added in schema v2 (see src/storage/migrations.py)
_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS politicians (
    bioguide           TEXT PRIMARY KEY,
    full_name          TEXT,
    name_keys          TEXT,
    party              TEXT,
    state              TEXT,
    chamber            TEXT,
    committees         TEXT,
    updated_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kv_store (
    key                TEXT PRIMARY KEY,
    value              TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    ticker             TEXT,
    date               TEXT,
    close              REAL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_trades_politician ON trades(politician_name);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source, source_id);
"""

# Added in schema v3 (see src/storage/migrations.py)
_SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS flow_alerts (
    contract_key       TEXT,
    alert_date         TEXT,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (contract_key, alert_date)
);
"""

# Added in schema v4: flow alerts with full contract detail, feeding both the
# per-day dedup check and the web dashboard's flow feed.
_SCHEMA_V4_SQL = """
CREATE TABLE IF NOT EXISTS flow_events (
    contract_key       TEXT,
    alert_date         TEXT,
    ticker             TEXT,
    option_type        TEXT,
    strike             REAL,
    expiration         TEXT,
    premium_usd        REAL,
    volume             INTEGER,
    open_interest      INTEGER,
    vol_oi_ratio       REAL,
    sentiment          TEXT,
    spot               REAL,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (contract_key, alert_date)
);
"""

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


async def init_db(db_path: str) -> str:
    """Create all tables if they do not already exist.

    Returns the *db_path* so callers can store it conveniently.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA_SQL)
        await db.executescript(_SCHEMA_V2_SQL)
        await db.executescript(_SCHEMA_V3_SQL)
        await db.executescript(_SCHEMA_V4_SQL)
        await db.commit()
    logger.info("Database initialised at %s", db_path)
    return db_path


# ---------------------------------------------------------------------------
# Trade helpers
# ---------------------------------------------------------------------------


async def is_trade_seen(db_path: str, trade_id: str) -> bool:
    """Return True if a trade with the given *trade_id* already exists."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1",
            (trade_id,),
        )
        row = await cursor.fetchone()
    return row is not None


async def insert_trade(db_path: str, trade: PoliticianTrade) -> None:
    """Insert a single trade, silently skipping if it already exists."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO trades (
                trade_id, politician_name, chamber, state, party,
                ticker, asset_name, transaction_type,
                transaction_date, filing_date, amount_range,
                owner, source, source_id, filing_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _trade_to_row(trade),
        )
        await db.commit()


async def insert_trades(
    db_path: str,
    trades: list[PoliticianTrade],
) -> list[PoliticianTrade]:
    """Insert a batch of trades.

    Returns only the *new* trades that were not previously in the database.
    """
    if not trades:
        return []

    new_trades: list[PoliticianTrade] = []

    async with aiosqlite.connect(db_path) as db:
        for trade in trades:
            cursor = await db.execute(
                "SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1",
                (trade.trade_id,),
            )
            already_exists = await cursor.fetchone() is not None

            if not already_exists:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO trades (
                        trade_id, politician_name, chamber, state, party,
                        ticker, asset_name, transaction_type,
                        transaction_date, filing_date, amount_range,
                        owner, source, source_id, filing_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _trade_to_row(trade),
                )
                new_trades.append(trade)

        await db.commit()

    logger.info(
        "Inserted %d new trades out of %d provided", len(new_trades), len(trades)
    )
    return new_trades


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def get_recent_trades(
    db_path: str,
    limit: int = 20,
    politician: str | None = None,
    ticker: str | None = None,
) -> list[dict]:
    """Return the most recent trades, optionally filtered by politician or ticker."""
    query = "SELECT * FROM trades WHERE 1=1"
    params: list[str | int] = []

    if politician is not None:
        query += " AND LOWER(politician_name) = LOWER(?)"
        params.append(politician)
    if ticker is not None:
        query += " AND UPPER(ticker) = UPPER(?)"
        params.append(ticker)

    query += " ORDER BY COALESCE(transaction_date, filing_date, created_at) DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]


async def get_top_tickers(
    db_path: str,
    days: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Return the most-traded tickers by politicians over the last *days* days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ticker,
                   COUNT(*)                                             AS trade_count,
                   COUNT(DISTINCT politician_name)                      AS politician_count,
                   SUM(CASE WHEN transaction_type = 'purchase' THEN 1 ELSE 0 END) AS buys,
                   SUM(CASE WHEN transaction_type = 'sale'     THEN 1 ELSE 0 END) AS sells
            FROM trades
            WHERE ticker != ''
              AND COALESCE(transaction_date, filing_date) >= ?
            GROUP BY UPPER(ticker)
            ORDER BY trade_count DESC
            LIMIT ?
            """,
            (cutoff, limit),
        )
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Followed-politicians helpers
# ---------------------------------------------------------------------------


async def follow_politician(
    db_path: str,
    discord_user_id: str,
    politician_name: str,
) -> None:
    """Subscribe a Discord user to notifications for a politician."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO followed_politicians (discord_user_id, politician_name)
            VALUES (?, ?)
            """,
            (discord_user_id, politician_name),
        )
        await db.commit()


async def unfollow_politician(
    db_path: str,
    discord_user_id: str,
    politician_name: str,
) -> None:
    """Remove a Discord user's subscription for a politician."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            DELETE FROM followed_politicians
            WHERE discord_user_id = ? AND politician_name = ?
            """,
            (discord_user_id, politician_name),
        )
        await db.commit()


async def get_followed_politicians(
    db_path: str,
    discord_user_id: str,
) -> list[str]:
    """Return the list of politician names a Discord user is following."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT politician_name FROM followed_politicians WHERE discord_user_id = ? ORDER BY politician_name",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()

    return [row[0] for row in rows]


async def get_followers(
    db_path: str,
    politician_name: str,
) -> list[str]:
    """Return Discord user IDs that are following a given politician."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT discord_user_id FROM followed_politicians WHERE LOWER(politician_name) = LOWER(?)",
            (politician_name,),
        )
        rows = await cursor.fetchall()

    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Executed-trades helpers
# ---------------------------------------------------------------------------


async def log_executed_trade(
    db_path: str,
    trade_id: str,
    broker: str,
    order_id: str,
    ticker: str,
    side: str,
    quantity: float,
    amount_usd: float,
    status: str,
) -> None:
    """Record a mirror trade that was submitted to a broker."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO executed_trades (
                trade_id, broker, order_id, ticker, side,
                quantity, amount_usd, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_id, broker, order_id, ticker, side, quantity, amount_usd, status),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Ingestion helpers
# ---------------------------------------------------------------------------


async def count_trades(db_path: str) -> int:
    """Return the total number of stored trades."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM trades")
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def get_known_source_ids(db_path: str) -> dict[str, set[str]]:
    """Return already-ingested filing IDs grouped by source name."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT DISTINCT source, source_id FROM trades WHERE source_id != ''"
        )
        rows = await cursor.fetchall()

    known: dict[str, set[str]] = {}
    for source, source_id in rows:
        known.setdefault(source, set()).add(source_id)
    return known


# ---------------------------------------------------------------------------
# Key/value store
# ---------------------------------------------------------------------------


async def kv_get(db_path: str, key: str) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = await cursor.fetchone()
    return row[0] if row else None


async def kv_set(db_path: str, key: str, value: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO kv_store (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Politician metadata (enrichment dataset)
# ---------------------------------------------------------------------------


async def upsert_politicians(db_path: str, politicians: list[dict]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            """
            INSERT INTO politicians (
                bioguide, full_name, name_keys, party, state, chamber, committees,
                updated_at
            ) VALUES (
                :bioguide, :full_name, :name_keys, :party, :state, :chamber,
                :committees, CURRENT_TIMESTAMP
            )
            ON CONFLICT(bioguide) DO UPDATE SET
                full_name = excluded.full_name,
                name_keys = excluded.name_keys,
                party = excluded.party,
                state = excluded.state,
                chamber = excluded.chamber,
                committees = excluded.committees,
                updated_at = CURRENT_TIMESTAMP
            """,
            politicians,
        )
        await db.commit()


async def get_politicians(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM politicians")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def search_politician_names(db_path: str, prefix: str, limit: int = 25) -> list[str]:
    """Distinct politician names from stored trades matching *prefix* (for autocomplete)."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT politician_name FROM trades
            WHERE politician_name LIKE ? AND politician_name != ''
            ORDER BY politician_name LIMIT ?
            """,
            (f"%{prefix}%", limit),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Price cache
# ---------------------------------------------------------------------------


async def get_price_history(db_path: str, ticker: str, start_iso: str) -> dict[str, float]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT date, close FROM price_history WHERE ticker = ? AND date >= ?",
            (ticker.upper(), start_iso),
        )
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def upsert_price_history(db_path: str, ticker: str, history: dict[str, float]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO price_history (ticker, date, close) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close",
            [(ticker.upper(), day, close) for day, close in history.items()],
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Flow alert dedup
# ---------------------------------------------------------------------------


async def was_flow_alerted(db_path: str, contract_key: str, alert_date: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM flow_events WHERE contract_key = ? AND alert_date = ?",
            (contract_key, alert_date),
        )
        return await cursor.fetchone() is not None


async def record_flow_event(db_path: str, event: dict) -> None:
    """Store one unusual-activity alert with its contract detail.

    *event* keys: contract_key, alert_date, ticker, option_type, strike,
    expiration, premium_usd, volume, open_interest, vol_oi_ratio,
    sentiment, spot.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO flow_events (
                contract_key, alert_date, ticker, option_type, strike,
                expiration, premium_usd, volume, open_interest,
                vol_oi_ratio, sentiment, spot
            ) VALUES (
                :contract_key, :alert_date, :ticker, :option_type, :strike,
                :expiration, :premium_usd, :volume, :open_interest,
                :vol_oi_ratio, :sentiment, :spot
            )
            """,
            event,
        )
        await db.commit()


async def get_recent_flow_events(db_path: str, limit: int = 25) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM flow_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------


async def get_politician_stats(db_path: str, politician: str) -> dict:
    """Aggregate stats for one politician: totals, top tickers, date range."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN transaction_type = 'purchase' THEN 1 ELSE 0 END) AS buys,
                   SUM(CASE WHEN transaction_type = 'sale' THEN 1 ELSE 0 END) AS sells,
                   COUNT(DISTINCT CASE WHEN ticker != '' THEN UPPER(ticker) END) AS tickers,
                   MIN(COALESCE(transaction_date, filing_date)) AS first_date,
                   MAX(COALESCE(transaction_date, filing_date)) AS last_date
            FROM trades WHERE LOWER(politician_name) = LOWER(?)
            """,
            (politician,),
        )
        summary = dict(await cursor.fetchone())

        cursor = await db.execute(
            """
            SELECT UPPER(ticker) AS ticker, COUNT(*) AS trade_count,
                   SUM(CASE WHEN transaction_type = 'purchase' THEN 1 ELSE 0 END) AS buys,
                   SUM(CASE WHEN transaction_type = 'sale' THEN 1 ELSE 0 END) AS sells
            FROM trades
            WHERE LOWER(politician_name) = LOWER(?) AND ticker != ''
            GROUP BY UPPER(ticker) ORDER BY trade_count DESC LIMIT 5
            """,
            (politician,),
        )
        top = [dict(row) for row in await cursor.fetchall()]

    summary["top_tickers"] = top
    return summary


async def get_most_active_politicians(
    db_path: str, days: int = 30, limit: int = 10
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT politician_name, COUNT(*) AS trade_count,
                   SUM(CASE WHEN transaction_type = 'purchase' THEN 1 ELSE 0 END) AS buys,
                   SUM(CASE WHEN transaction_type = 'sale' THEN 1 ELSE 0 END) AS sells
            FROM trades
            WHERE COALESCE(transaction_date, filing_date) >= ?
            GROUP BY LOWER(politician_name)
            ORDER BY trade_count DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_purchases_since(db_path: str, days: int = 180, limit: int = 300) -> list[dict]:
    """Purchases with tickers, newest first — input for performance tracking."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT politician_name, ticker, transaction_date, filing_date, amount_range
            FROM trades
            WHERE transaction_type = 'purchase' AND ticker != ''
              AND COALESCE(filing_date, transaction_date) >= ?
            ORDER BY COALESCE(filing_date, transaction_date) DESC
            LIMIT ?
            """,
            (cutoff, limit),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _trade_to_row(trade: PoliticianTrade) -> tuple:
    """Convert a PoliticianTrade model instance into a tuple matching the INSERT column order."""
    return (
        trade.trade_id,
        trade.politician_name,
        trade.chamber.value,
        trade.state,
        trade.party,
        trade.ticker,
        trade.asset_name,
        trade.transaction_type.value,
        trade.transaction_date.isoformat() if trade.transaction_date else None,
        trade.filing_date.isoformat() if trade.filing_date else None,
        trade.amount_range,
        trade.owner,
        trade.source,
        trade.source_id,
        trade.filing_url,
    )
