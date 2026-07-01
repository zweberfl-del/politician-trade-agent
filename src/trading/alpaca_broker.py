from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.config import settings
from src.trading.base import AccountInfo, Broker, OrderResult, Position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful handling when alpaca-py is not installed
# ---------------------------------------------------------------------------

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest


def _require_alpaca() -> None:
    """Raise a clear error if alpaca-py is not installed."""
    if not _ALPACA_AVAILABLE:
        raise RuntimeError(
            "alpaca-py is not installed. "
            "Install it with:  pip install alpaca-py"
        )


class AlpacaBroker(Broker):
    """Default Broker implementation backed by Alpaca Markets.

    Supports both paper and live trading based on ``settings.paper_trading``.
    Uses *notional* (dollar-amount) market orders so fractional shares are
    handled automatically by Alpaca.
    """

    def __init__(self) -> None:
        _require_alpaca()

        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=settings.paper_trading,
            url_override=settings.alpaca_base_url or None,
        )
        mode = "paper" if settings.paper_trading else "LIVE"
        logger.info("AlpacaBroker initialised in %s mode", mode)

    # ------------------------------------------------------------------
    # Broker interface
    # ------------------------------------------------------------------

    async def buy(self, ticker: str, amount_usd: float) -> OrderResult:
        """Submit a notional market buy order."""
        return await self._submit_order(ticker, amount_usd, OrderSide.BUY)

    async def sell(self, ticker: str, amount_usd: float) -> OrderResult:
        """Submit a notional market sell order.

        If the current position is worth less than *amount_usd*, the entire
        position is sold instead.
        """
        # Check if we hold this ticker and cap to position value
        positions = await self.get_positions()
        held = next((p for p in positions if p.ticker.upper() == ticker.upper()), None)

        if held is None or held.quantity <= 0:
            return OrderResult(
                order_id="",
                ticker=ticker,
                side="sell",
                quantity=0.0,
                status="failed",
                error=f"No position in {ticker} to sell",
            )

        effective_amount = min(amount_usd, held.market_value)
        return await self._submit_order(ticker, effective_amount, OrderSide.SELL)

    async def get_positions(self) -> list[Position]:
        """Return all open positions."""
        raw_positions = await asyncio.to_thread(self._client.get_all_positions)
        return [
            Position(
                ticker=p.symbol,
                quantity=float(p.qty),
                market_value=float(p.market_value),
                avg_entry_price=float(p.avg_entry_price),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in raw_positions
        ]

    async def get_account(self) -> AccountInfo:
        """Return account equity, cash, and buying power."""
        acct = await asyncio.to_thread(self._client.get_account)
        return AccountInfo(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _submit_order(
        self,
        ticker: str,
        amount_usd: float,
        side: OrderSide,
    ) -> OrderResult:
        """Submit a notional market order and return an ``OrderResult``."""
        side_label = "buy" if side == OrderSide.BUY else "sell"
        logger.info(
            "Submitting %s order for ~$%.2f of %s", side_label, amount_usd, ticker
        )

        request = MarketOrderRequest(
            symbol=ticker,
            notional=round(amount_usd, 2),
            side=side,
            time_in_force=TimeInForce.DAY,
        )

        try:
            order = await asyncio.to_thread(self._client.submit_order, request)
        except Exception as exc:
            logger.error("Order failed for %s: %s", ticker, exc)
            return OrderResult(
                order_id="",
                ticker=ticker,
                side=side_label,
                quantity=0.0,
                status="failed",
                error=str(exc),
            )

        filled_price = float(order.filled_avg_price) if order.filled_avg_price else None
        qty = float(order.filled_qty) if order.filled_qty else float(order.qty or 0)

        return OrderResult(
            order_id=str(order.id),
            ticker=ticker,
            side=side_label,
            quantity=qty,
            filled_price=filled_price,
            status=order.status.value if order.status else "submitted",
        )
