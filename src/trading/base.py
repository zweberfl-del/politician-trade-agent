from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    side: str  # "buy" or "sell"
    quantity: float
    filled_price: float | None = None
    status: str = "submitted"  # "submitted", "filled", "failed", "cancelled"
    error: str | None = None


@dataclass
class Position:
    ticker: str
    quantity: float
    market_value: float
    avg_entry_price: float
    unrealized_pl: float


@dataclass
class AccountInfo:
    equity: float
    cash: float
    buying_power: float


class Broker(ABC):
    @abstractmethod
    async def buy(self, ticker: str, amount_usd: float) -> OrderResult:
        """Place a market buy order for approximately `amount_usd` worth of stock."""
        ...

    @abstractmethod
    async def sell(self, ticker: str, amount_usd: float) -> OrderResult:
        """Place a market sell order for approximately `amount_usd` worth of stock."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all current positions."""
        ...

    @abstractmethod
    async def get_account(self) -> AccountInfo:
        """Get account balance info."""
        ...
