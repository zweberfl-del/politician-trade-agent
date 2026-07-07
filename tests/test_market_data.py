from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.analysis.flow import chain_sentiment, detect_unusual_activity
from src.analysis.gex import bs_gamma, compute_gex
from src.data.darkpool import parse_regsho_text
from src.data.flow_scanner import is_market_hours
from src.data.options import OptionContract, OptionsChain, parse_yahoo_contract


def _contract(
    option_type: str = "call",
    strike: float = 105.0,
    volume: int = 5000,
    oi: int = 500,
    mid: float = 2.0,
    dte: int = 14,
    iv: float = 0.3,
) -> OptionContract:
    return OptionContract(
        contract_symbol=f"TST{strike:g}{option_type[0].upper()}",
        ticker="TST",
        option_type=option_type,
        strike=strike,
        expiration=date.today() + timedelta(days=dte),
        bid=mid - 0.05,
        ask=mid + 0.05,
        last_price=mid,
        volume=volume,
        open_interest=oi,
        implied_volatility=iv,
    )


def _chain(contracts: list[OptionContract], spot: float = 100.0) -> OptionsChain:
    return OptionsChain(ticker="TST", spot=spot, contracts=contracts)


# ---------------------------------------------------------------------------
# Unusual activity detection
# ---------------------------------------------------------------------------


class TestUnusualActivity:
    def test_detects_high_premium_fresh_positioning(self) -> None:
        # 5000 contracts * $2 mid * 100 = $1M premium, 10x vol/OI
        hits = detect_unusual_activity(_chain([_contract()]))
        assert len(hits) == 1
        assert hits[0].sentiment == "bullish"
        assert hits[0].vol_oi_ratio == 10.0

    def test_ignores_small_premium(self) -> None:
        small = _contract(volume=200, mid=1.0)  # $20K premium
        assert detect_unusual_activity(_chain([small])) == []

    def test_ignores_churn_with_high_oi(self) -> None:
        churn = _contract(volume=5000, oi=50_000)  # 0.1x vol/OI
        assert detect_unusual_activity(_chain([churn])) == []

    def test_ignores_far_dated(self) -> None:
        leap = _contract(dte=400)
        assert detect_unusual_activity(_chain([leap])) == []

    def test_put_is_bearish_and_otm_math(self) -> None:
        put = _contract(option_type="put", strike=90.0)
        hits = detect_unusual_activity(_chain([put]))
        assert hits[0].sentiment == "bearish"
        assert abs(hits[0].otm_pct - 10.0) < 1e-9

    def test_zero_oi_counts_as_fresh(self) -> None:
        fresh = _contract(oi=0)
        hits = detect_unusual_activity(_chain([fresh]))
        assert len(hits) == 1

    def test_ranked_by_score(self) -> None:
        big = _contract(strike=110.0, volume=20_000)
        small = _contract(strike=105.0, volume=1_000)
        hits = detect_unusual_activity(_chain([small, big]))
        assert hits[0].contract.strike == 110.0


class TestChainSentiment:
    def test_put_call_ratio(self) -> None:
        chain = _chain(
            [
                _contract(option_type="call", volume=4000),
                _contract(option_type="put", volume=2000, strike=95.0),
            ]
        )
        s = chain_sentiment(chain)
        assert s["call_volume"] == 4000
        assert s["put_volume"] == 2000
        assert abs(s["put_call_ratio"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# Black-Scholes gamma and GEX
# ---------------------------------------------------------------------------


class TestGamma:
    def test_atm_gamma_matches_closed_form(self) -> None:
        # S=K=100, iv=20%, T=1y, r=4%: d1=(r+iv^2/2)/iv = 0.3
        expected = math.exp(-0.045) / math.sqrt(2 * math.pi) / (100 * 0.2)
        assert abs(bs_gamma(100, 100, 365, 0.2) - expected) < 1e-9

    def test_degenerate_inputs_return_zero(self) -> None:
        assert bs_gamma(0, 100, 30, 0.2) == 0.0
        assert bs_gamma(100, 100, 0, 0.2) == 0.0
        assert bs_gamma(100, 100, 30, 0.0) == 0.0

    def test_deep_otm_gamma_smaller_than_atm(self) -> None:
        assert bs_gamma(100, 100, 30, 0.2) > bs_gamma(100, 160, 30, 0.2)


class TestGex:
    def test_calls_positive_puts_negative(self) -> None:
        chain = _chain(
            [
                _contract(option_type="call", strike=100.0, oi=1000),
                _contract(option_type="put", strike=100.0, oi=1000),
            ]
        )
        profile = compute_gex(chain)
        assert profile.call_gex > 0
        assert profile.put_gex < 0
        # Same strike/IV/OI: exposures offset exactly
        assert abs(profile.net_gex) < 1e-6

    def test_put_heavy_chain_is_net_negative(self) -> None:
        chain = _chain(
            [
                _contract(option_type="call", strike=105.0, oi=100),
                _contract(option_type="put", strike=95.0, oi=10_000),
            ]
        )
        assert compute_gex(chain).net_gex < 0

    def test_zero_oi_contracts_ignored(self) -> None:
        chain = _chain([_contract(oi=0)])
        assert compute_gex(chain).net_gex == 0.0


# ---------------------------------------------------------------------------
# FINRA Reg SHO parsing
# ---------------------------------------------------------------------------

REGSHO_FIXTURE = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20260702|AAPL|1200000|4000|3000000|B,Q,N
20260702|TSLA|900000|100|1500000|B,Q,N
Trailer record count: 2
"""


class TestRegShoParsing:
    def test_parses_rows(self) -> None:
        parsed = parse_regsho_text(REGSHO_FIXTURE)
        assert set(parsed) == {"AAPL", "TSLA"}
        aapl = parsed["AAPL"]
        assert aapl.day == date(2026, 7, 2)
        assert aapl.short_volume == 1_200_000
        assert abs(aapl.short_ratio - 0.4) < 1e-9

    def test_garbage_lines_skipped(self) -> None:
        assert parse_regsho_text("not|a|real|file") == {}


# ---------------------------------------------------------------------------
# Yahoo contract parsing
# ---------------------------------------------------------------------------


class TestYahooParsing:
    def test_parses_contract(self) -> None:
        raw = {
            "contractSymbol": "AAPL260918C00200000",
            "strike": 200.0,
            "expiration": 1789689600,  # 2026-09-18 UTC
            "lastPrice": 5.5,
            "bid": 5.4,
            "ask": 5.6,
            "volume": 1234,
            "openInterest": 5000,
            "impliedVolatility": 0.31,
        }
        c = parse_yahoo_contract(raw, "AAPL", "call")
        assert c is not None
        assert c.strike == 200.0
        assert c.volume == 1234
        assert abs(c.mid_price - 5.5) < 1e-9

    def test_missing_fields_default(self) -> None:
        c = parse_yahoo_contract(
            {"strike": 10.0, "expiration": 1789689600}, "X", "put"
        )
        assert c is not None
        assert c.volume == 0 and c.open_interest == 0

    def test_malformed_returns_none(self) -> None:
        assert parse_yahoo_contract({"strike": "??"}, "X", "call") is None


# ---------------------------------------------------------------------------
# Market-hours gate
# ---------------------------------------------------------------------------


class TestMarketHours:
    ET = ZoneInfo("America/New_York")

    def test_open_midday_weekday(self) -> None:
        # Monday 2026-07-06 12:00 ET
        assert is_market_hours(datetime(2026, 7, 6, 12, 0, tzinfo=self.ET))

    def test_closed_premarket_and_after_hours(self) -> None:
        assert not is_market_hours(datetime(2026, 7, 6, 9, 15, tzinfo=self.ET))
        assert not is_market_hours(datetime(2026, 7, 6, 16, 30, tzinfo=self.ET))

    def test_closed_weekend(self) -> None:
        assert not is_market_hours(datetime(2026, 7, 5, 12, 0, tzinfo=self.ET))

    def test_utc_input_converted(self) -> None:
        # 13:00 UTC in July == 9:00 ET (premarket)
        assert not is_market_hours(datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc))
