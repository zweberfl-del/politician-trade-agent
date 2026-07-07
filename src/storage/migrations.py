from __future__ import annotations

import logging

import aiosqlite

from src.storage.database import (
    _SCHEMA_SQL,
    _SCHEMA_V2_SQL,
    _SCHEMA_V3_SQL,
    _SCHEMA_V4_SQL,
    _SCHEMA_V5_SQL,
)

logger = logging.getLogger(__name__)

# Sequential migration scripts. A database at user_version N gets every
# script after index N applied, then its user_version bumped. All scripts
# use IF NOT EXISTS so pre-versioning databases (user_version 0 with tables
# already present) upgrade cleanly.
_MIGRATIONS: list[str] = [
    _SCHEMA_SQL,     # v1 — base schema
    _SCHEMA_V2_SQL,  # v2 — politicians, kv_store, price_history, indexes
    _SCHEMA_V3_SQL,  # v3 — flow_alerts (options flow alert dedup)
    _SCHEMA_V4_SQL,  # v4 — flow_events (alert detail for dedup + dashboard)
    _SCHEMA_V5_SQL,  # v5 — prediction_events (Polymarket unusual bets)
]


async def run_migrations(db_path: str) -> None:
    """Apply all pending database migrations."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row else 0

        for target, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            await db.executescript(script)
            await db.execute(f"PRAGMA user_version = {target}")
            await db.commit()
            logger.info("Applied migration v%d to %s", target, db_path)

    logger.info("Migrations complete for %s", db_path)
