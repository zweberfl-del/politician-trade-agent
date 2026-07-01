from __future__ import annotations

from datetime import date

import pytest

from src.data.models import Chamber, PoliticianTrade, TransactionType


# ---------------------------------------------------------------------------
# TransactionType.from_raw
# ---------------------------------------------------------------------------


class TestTransactionTypeFromRaw:
    """Verify that raw strings from various data sources normalise correctly."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Purchase", TransactionType.PURCHASE),
            ("buy", TransactionType.PURCHASE),
            ("P", TransactionType.PURCHASE),
            ("Sale", TransactionType.SALE),
            ("sell", TransactionType.SALE),
            ("S", TransactionType.SALE),
            ("sale (full)", TransactionType.SALE),
            ("sale (partial)", TransactionType.SALE),
            ("Exchange", TransactionType.EXCHANGE),
            ("exchange", TransactionType.EXCHANGE),
            ("E", TransactionType.EXCHANGE),
        ],
    )
    def test_known_values(self, raw: str, expected: TransactionType) -> None:
        assert TransactionType.from_raw(raw) is expected

    def test_garbage_returns_unknown(self) -> None:
        assert TransactionType.from_raw("garbage") is TransactionType.UNKNOWN

    def test_empty_string_returns_unknown(self) -> None:
        assert TransactionType.from_raw("") is TransactionType.UNKNOWN

    def test_whitespace_is_stripped(self) -> None:
        assert TransactionType.from_raw("  purchase  ") is TransactionType.PURCHASE

    def test_case_insensitive(self) -> None:
        assert TransactionType.from_raw("PURCHASE") is TransactionType.PURCHASE
        assert TransactionType.from_raw("BUY") is TransactionType.PURCHASE
        assert TransactionType.from_raw("SALE") is TransactionType.SALE


# ---------------------------------------------------------------------------
# PoliticianTrade.trade_id
# ---------------------------------------------------------------------------


class TestTradeId:
    """The trade_id property must be deterministic and stable."""

    def _make_trade(self, **overrides) -> PoliticianTrade:
        defaults = dict(
            politician_name="Nancy Pelosi",
            chamber=Chamber.HOUSE,
            ticker="AAPL",
            transaction_type=TransactionType.PURCHASE,
            transaction_date=date(2024, 6, 15),
            amount_range="$1,001 - $15,000",
        )
        defaults.update(overrides)
        return PoliticianTrade(**defaults)

    def test_deterministic(self) -> None:
        """Identical inputs must produce the same trade_id every time."""
        a = self._make_trade()
        b = self._make_trade()
        assert a.trade_id == b.trade_id

    def test_components_included(self) -> None:
        trade = self._make_trade()
        tid = trade.trade_id
        assert "nancy pelosi" in tid
        assert "AAPL" in tid
        assert "purchase" in tid
        assert "2024-06-15" in tid
        assert "$1,001 - $15,000" in tid

    def test_different_ticker_gives_different_id(self) -> None:
        a = self._make_trade(ticker="AAPL")
        b = self._make_trade(ticker="MSFT")
        assert a.trade_id != b.trade_id

    def test_different_type_gives_different_id(self) -> None:
        a = self._make_trade(transaction_type=TransactionType.PURCHASE)
        b = self._make_trade(transaction_type=TransactionType.SALE)
        assert a.trade_id != b.trade_id

    def test_none_date_handled(self) -> None:
        trade = self._make_trade(transaction_date=None)
        # Should not raise and should contain an empty segment
        assert "|" in trade.trade_id


# ---------------------------------------------------------------------------
# PoliticianTrade.display_amount
# ---------------------------------------------------------------------------


class TestDisplayAmount:
    def test_returns_amount_range_when_set(self) -> None:
        trade = PoliticianTrade(
            politician_name="Test",
            chamber=Chamber.HOUSE,
            transaction_type=TransactionType.PURCHASE,
            amount_range="$1,001 - $15,000",
        )
        assert trade.display_amount == "$1,001 - $15,000"

    def test_returns_unknown_when_empty(self) -> None:
        trade = PoliticianTrade(
            politician_name="Test",
            chamber=Chamber.HOUSE,
            transaction_type=TransactionType.PURCHASE,
            amount_range="",
        )
        assert trade.display_amount == "Unknown"

    def test_returns_unknown_when_default(self) -> None:
        trade = PoliticianTrade(
            politician_name="Test",
            chamber=Chamber.HOUSE,
            transaction_type=TransactionType.PURCHASE,
        )
        assert trade.display_amount == "Unknown"
