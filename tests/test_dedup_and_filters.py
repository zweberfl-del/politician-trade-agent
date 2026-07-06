from __future__ import annotations

from datetime import date

from src.data.enrichment import normalize_name
from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.data.parsers import amount_range_bounds
from src.data.poller import Poller


def _metadata_filing(name: str, source_id: str) -> PoliticianTrade:
    """A filing-level row as produced by House/Senate metadata fallback."""
    return PoliticianTrade(
        politician_name=name,
        chamber=Chamber.HOUSE,
        transaction_type=TransactionType.UNKNOWN,
        source="house_clerk",
        source_id=source_id,
        filing_date=date(2026, 6, 1),
    )


def _detail_trade(**overrides) -> PoliticianTrade:
    defaults = dict(
        politician_name="Nancy Pelosi",
        chamber=Chamber.HOUSE,
        ticker="AAPL",
        transaction_type=TransactionType.PURCHASE,
        transaction_date=date(2026, 6, 15),
        amount_range="$1,001 - $15,000",
        source="house_clerk",
        source_id="20026000",
        party="D",
    )
    defaults.update(overrides)
    return PoliticianTrade(**defaults)


# ---------------------------------------------------------------------------
# trade_id fallback for metadata-only filings
# ---------------------------------------------------------------------------


class TestTradeIdFallback:
    def test_two_filings_by_same_politician_are_distinct(self) -> None:
        a = _metadata_filing("Nancy Pelosi", "20026001")
        b = _metadata_filing("Nancy Pelosi", "20026002")
        assert a.trade_id != b.trade_id

    def test_same_filing_is_stable(self) -> None:
        a = _metadata_filing("Nancy Pelosi", "20026001")
        b = _metadata_filing("Nancy Pelosi", "20026001")
        assert a.trade_id == b.trade_id

    def test_detail_trades_ignore_source_id(self) -> None:
        # The same trade reported by two sources must still collapse.
        a = _detail_trade(source="house_clerk", source_id="20026000")
        b = _detail_trade(source="finnhub", source_id="finnhub|AAPL|xyz")
        assert a.trade_id == b.trade_id


# ---------------------------------------------------------------------------
# Alert filters
# ---------------------------------------------------------------------------


class TestAlertFilters:
    def _poller(self) -> Poller:
        # bot/fetcher are unused by _passes_alert_filters
        return Poller(fetcher=None, bot=None)  # type: ignore[arg-type]

    def test_defaults_pass_everything(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "alert_ticker_watchlist", "")
        monkeypatch.setattr(config.settings, "alert_party_filter", "")
        monkeypatch.setattr(config.settings, "alert_min_amount_usd", 0.0)
        assert self._poller()._passes_alert_filters(_detail_trade())

    def test_watchlist_filters_other_tickers(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "alert_ticker_watchlist", "NVDA, msft")
        poller = self._poller()
        assert not poller._passes_alert_filters(_detail_trade(ticker="AAPL"))
        assert poller._passes_alert_filters(_detail_trade(ticker="MSFT"))

    def test_party_filter(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "alert_party_filter", "R")
        poller = self._poller()
        assert not poller._passes_alert_filters(_detail_trade(party="D"))
        assert poller._passes_alert_filters(_detail_trade(party="R"))
        # Unknown party is not suppressed
        assert poller._passes_alert_filters(_detail_trade(party=""))

    def test_min_amount(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "alert_min_amount_usd", 50000.0)
        poller = self._poller()
        assert not poller._passes_alert_filters(
            _detail_trade(amount_range="$1,001 - $15,000")
        )
        assert poller._passes_alert_filters(
            _detail_trade(amount_range="$50,001 - $100,000")
        )
        # Metadata-only rows (no amount) still alert
        assert poller._passes_alert_filters(_metadata_filing("X", "1"))


# ---------------------------------------------------------------------------
# Name normalization (enrichment join key)
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_basic(self) -> None:
        assert normalize_name("Nancy Pelosi") == "nancy pelosi"

    def test_honorifics_and_suffixes(self) -> None:
        assert normalize_name("Hon. David J. Taylor Jr.") == "david taylor"

    def test_middle_names_dropped(self) -> None:
        assert normalize_name("Thomas H. Tuberville") == "thomas tuberville"

    def test_matches_official_variants(self) -> None:
        assert normalize_name("Sheldon Whitehouse") == normalize_name(
            "Hon. Sheldon Whitehouse"
        )


class TestAmountBoundsIntegration:
    def test_bounds_used_by_filter_match_disclosure_format(self) -> None:
        assert amount_range_bounds("$500,001 - $1,000,000") == (500001.0, 1000000.0)
