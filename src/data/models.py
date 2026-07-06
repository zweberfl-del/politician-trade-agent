from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class Chamber(str, Enum):
    HOUSE = "house"
    SENATE = "senate"


class TransactionType(str, Enum):
    PURCHASE = "purchase"
    SALE = "sale"
    EXCHANGE = "exchange"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, raw: str) -> TransactionType:
        """Normalize raw transaction type strings from various sources."""
        normalized = raw.strip().lower()
        if normalized in ("purchase", "buy", "p"):
            return cls.PURCHASE
        if normalized in (
            "sale", "sell", "s", "sale (full)", "sale (partial)",
            "s (full)", "s (partial)",
        ):
            return cls.SALE
        if normalized in ("exchange", "e"):
            return cls.EXCHANGE
        return cls.UNKNOWN


class PoliticianTrade(BaseModel):
    """A single stock trade disclosed by a politician."""

    # Identity
    politician_name: str
    chamber: Chamber
    state: str = ""
    party: str = ""

    # Trade details
    ticker: str = ""
    asset_name: str = ""
    transaction_type: TransactionType
    transaction_date: date | None = None
    filing_date: date | None = None
    amount_range: str = ""  # e.g. "$1,001 - $15,000"
    owner: str = ""  # "Self", "Spouse", "Joint", "Child"

    # Source tracking
    source: str = ""  # e.g. "house_clerk", "senate_efd", "finnhub"
    source_id: str = ""  # unique ID from the source for deduplication
    filing_url: str = ""

    @property
    def display_amount(self) -> str:
        return self.amount_range or "Unknown"

    @property
    def trade_id(self) -> str:
        """Generate a deterministic ID for deduplication.

        Trades with real detail (a ticker or a transaction date) dedupe on
        their content so the same trade reported by two sources collapses
        into one row. Metadata-only filings (no ticker, no date, no amount)
        would all produce the same key per politician, so they fall back to
        the source document ID to stay distinct.
        """
        has_detail = bool(
            self.ticker.strip() or self.transaction_date or self.amount_range.strip()
        )
        if not has_detail and self.source_id:
            return "|".join(
                [self.politician_name.lower().strip(), self.source, self.source_id]
            )

        parts = [
            self.politician_name.lower().strip(),
            self.ticker.upper().strip(),
            self.transaction_type.value,
            str(self.transaction_date) if self.transaction_date else "",
            self.amount_range,
        ]
        return "|".join(parts)


class Filing(BaseModel):
    """A disclosure filing (may contain multiple trades)."""

    filer_name: str
    filing_date: date | None = None
    filing_type: str = ""  # "PTR", "Annual", "Amendment"
    doc_id: str = ""
    chamber: Chamber
    state: str = ""
    url: str = ""
    trades: list[PoliticianTrade] = Field(default_factory=list)
