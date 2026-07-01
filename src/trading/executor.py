from __future__ import annotations

import logging

import aiosqlite

from src.config import settings
from src.data.models import PoliticianTrade, TransactionType
from src.storage.database import log_executed_trade
from src.trading.base import Broker, OrderResult

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Mirrors politician trades through a configured broker.

    Trades are only executed once -- the ``executed_trades`` table is checked
    before every submission so restarts or duplicate data never cause double
    orders.
    """

    def __init__(self, broker: Broker, db_path: str) -> None:
        self.broker = broker
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def mirror_trade(self, trade: PoliticianTrade) -> OrderResult | None:
        """Attempt to mirror a single politician trade.

        Returns an ``OrderResult`` when the trade is executed, or ``None``
        when it is skipped.

        Skip conditions
        ---------------
        * No ticker on the trade.
        * Transaction type is ``UNKNOWN``.
        * Transaction type is ``SALE`` and ``settings.enable_sell_mirror`` is
          ``False``.
        * The trade was already executed (present in ``executed_trades``).
        """
        # --- Skip checks ---
        if not trade.ticker:
            logger.debug("Skipping trade — no ticker: %s", trade.trade_id)
            return None

        if trade.transaction_type is TransactionType.UNKNOWN:
            logger.debug("Skipping trade — unknown transaction type: %s", trade.trade_id)
            return None

        if (
            trade.transaction_type is TransactionType.SALE
            and not settings.enable_sell_mirror
        ):
            logger.debug(
                "Skipping SALE trade — sell mirroring disabled: %s", trade.trade_id
            )
            return None

        if await self._already_executed(trade.trade_id):
            logger.debug("Skipping trade — already executed: %s", trade.trade_id)
            return None

        # --- Determine side ---
        if trade.transaction_type is TransactionType.PURCHASE:
            side = "buy"
        elif trade.transaction_type is TransactionType.SALE:
            side = "sell"
        elif trade.transaction_type is TransactionType.EXCHANGE:
            # Treat exchanges as buys (acquiring the new position).
            side = "buy"
        else:
            logger.warning(
                "Unhandled transaction type %s for %s — skipping",
                trade.transaction_type,
                trade.trade_id,
            )
            return None

        amount_usd = settings.trade_amount_usd

        # --- Execute ---
        logger.info(
            "Mirroring trade: %s %s ~$%.2f (%s by %s)",
            side.upper(),
            trade.ticker,
            amount_usd,
            trade.transaction_type.value,
            trade.politician_name,
        )

        try:
            if side == "buy":
                result = await self.broker.buy(trade.ticker, amount_usd)
            else:
                result = await self.broker.sell(trade.ticker, amount_usd)
        except Exception as exc:
            logger.error(
                "Broker error while mirroring %s %s: %s",
                side,
                trade.ticker,
                exc,
            )
            result = OrderResult(
                order_id="",
                ticker=trade.ticker,
                side=side,
                quantity=0.0,
                status="failed",
                error=str(exc),
            )

        # --- Record ---
        try:
            await log_executed_trade(
                db_path=self.db_path,
                trade_id=trade.trade_id,
                broker=type(self.broker).__name__,
                order_id=result.order_id,
                ticker=result.ticker,
                side=result.side,
                quantity=result.quantity,
                amount_usd=amount_usd,
                status=result.status,
            )
        except Exception as exc:
            logger.error("Failed to log executed trade %s: %s", trade.trade_id, exc)

        logger.info(
            "Trade result: %s %s qty=%.4f status=%s order_id=%s",
            result.side,
            result.ticker,
            result.quantity,
            result.status,
            result.order_id,
        )

        return result

    async def mirror_trades(
        self, trades: list[PoliticianTrade]
    ) -> list[OrderResult]:
        """Mirror a batch of politician trades.

        Individual failures are logged and skipped so one bad trade never
        prevents the rest from executing.

        Returns the list of ``OrderResult`` objects for trades that were
        actually submitted (skipped trades are excluded).
        """
        results: list[OrderResult] = []

        for trade in trades:
            try:
                result = await self.mirror_trade(trade)
                if result is not None:
                    results.append(result)
            except Exception as exc:
                logger.error(
                    "Unexpected error mirroring trade %s (%s): %s",
                    trade.ticker,
                    trade.trade_id,
                    exc,
                )

        logger.info(
            "Batch mirror complete: %d executed out of %d provided",
            len(results),
            len(trades),
        )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _already_executed(self, trade_id: str) -> bool:
        """Check whether a trade has already been submitted to the broker."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM executed_trades WHERE trade_id = ? LIMIT 1",
                (trade_id,),
            )
            row = await cursor.fetchone()
        return row is not None
