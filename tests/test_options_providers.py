from __future__ import annotations

from datetime import date

from src.data.options import (
    TradierOptionsProvider,
    YahooOptionsProvider,
    build_options_provider,
    parse_tradier_contract,
)


class TestTradierParsing:
    RAW = {
        "symbol": "SPY260918C00650000",
        "option_type": "CALL",
        "strike": 650.0,
        "expiration_date": "2026-09-18",
        "last": 12.35,
        "bid": 12.30,
        "ask": 12.40,
        "volume": 4210,
        "open_interest": 380,
        "greeks": {"mid_iv": 0.185, "gamma": 0.012},
    }

    def test_parses_contract(self) -> None:
        c = parse_tradier_contract(self.RAW, "SPY")
        assert c is not None
        assert c.option_type == "call"
        assert c.strike == 650.0
        assert c.expiration == date(2026, 9, 18)
        assert c.volume == 4210
        assert abs(c.implied_volatility - 0.185) < 1e-9
        assert abs(c.mid_price - 12.35) < 1e-9

    def test_null_fields_default(self) -> None:
        raw = dict(self.RAW, last=None, bid=None, ask=None, volume=None, greeks=None)
        c = parse_tradier_contract(raw, "SPY")
        assert c is not None
        assert c.volume == 0
        assert c.implied_volatility == 0.0

    def test_malformed_returns_none(self) -> None:
        assert parse_tradier_contract({"strike": "??"}, "SPY") is None


class TestProviderSelection:
    def test_default_is_yahoo(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "yahoo")
        assert isinstance(build_options_provider(), YahooOptionsProvider)

    def test_auto_upgrades_to_realtime_when_key_present(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "auto")
        monkeypatch.setattr(config.settings, "tradier_api_key", "test-key")
        assert isinstance(build_options_provider(), TradierOptionsProvider)

    def test_auto_without_key_is_yahoo(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "auto")
        monkeypatch.setattr(config.settings, "tradier_api_key", "")
        assert isinstance(build_options_provider(), YahooOptionsProvider)

    def test_forced_yahoo_ignores_key(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "yahoo")
        monkeypatch.setattr(config.settings, "tradier_api_key", "test-key")
        assert isinstance(build_options_provider(), YahooOptionsProvider)

    def test_tradier_with_key(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "tradier")
        monkeypatch.setattr(config.settings, "tradier_api_key", "test-key")
        provider = build_options_provider()
        assert isinstance(provider, TradierOptionsProvider)
        assert provider.api_key == "test-key"

    def test_tradier_without_key_falls_back(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "options_provider", "tradier")
        monkeypatch.setattr(config.settings, "tradier_api_key", "")
        assert isinstance(build_options_provider(), YahooOptionsProvider)
