from __future__ import annotations

import logging

from src.storage.database import init_db

logger = logging.getLogger(__name__)


async def run_migrations(db_path: str) -> None:
    """Apply all database migrations.

    Currently this only ensures the base schema exists via ``init_db``.
    Future schema changes should be added here as sequential migration
    steps so that existing databases are upgraded in place.
    """
    await init_db(db_path)
    logger.info("Migrations complete for %s", db_path)
