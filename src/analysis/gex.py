"""Gamma exposure (GEX) from an options chain.

Black-Scholes gamma per contract, aggregated with the standard dealer
convention (dealers long calls / short puts): call gamma adds, put gamma
subtracts. Values are dollars of delta-hedging flow per 1% move in the
underlying — the same units flow platforms chart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from src.data.options import OptionsChain

_RISK_FREE_RATE = 0.04
_CONTRACT_MULTIPLIER = 100


def bs_gamma(spot: float, strike: float, days_to_expiry: float, iv: float) -> float:
    """Black-Scholes gamma. Returns 0 for degenerate inputs."""
    if spot <= 0 or strike <= 0 or days_to_expiry <= 0 or iv <= 0:
        return 0.0
    t = days_to_expiry / 365.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (_RISK_FREE_RATE + iv**2 / 2) * t) / (iv * sqrt_t)
    pdf = math.exp(-(d1**2) / 2) / math.sqrt(2 * math.pi)
    return pdf / (spot * iv * sqrt_t)


@dataclass
class GexProfile:
    ticker: str
    spot: float
    net_gex: float  # $ per 1% move, dealer-positioning convention
    call_gex: float
    put_gex: float
    by_strike: dict[float, float] = field(default_factory=dict)

    def top_strikes(self, n: int = 5) -> list[tuple[float, float]]:
        """Strikes with the largest absolute gamma exposure."""
        ranked = sorted(self.by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return ranked[:n]

    @property
    def zero_gamma_estimate(self) -> float | None:
        """Rough flip level: strike where cumulative GEX changes sign."""
        cumulative = 0.0
        previous_strike: float | None = None
        for strike in sorted(self.by_strike):
            next_cumulative = cumulative + self.by_strike[strike]
            if cumulative != 0 and (cumulative < 0) != (next_cumulative < 0):
                return previous_strike if previous_strike is not None else strike
            cumulative = next_cumulative
            previous_strike = strike
        return None


def compute_gex(chain: OptionsChain, today: date | None = None) -> GexProfile:
    """Aggregate gamma exposure for a chain snapshot."""
    call_gex = 0.0
    put_gex = 0.0
    by_strike: dict[float, float] = {}

    for contract in chain.contracts:
        dte = contract.days_to_expiry(today)
        gamma = bs_gamma(chain.spot, contract.strike, dte, contract.implied_volatility)
        if gamma == 0.0 or contract.open_interest <= 0:
            continue
        # $ of dealer hedge flow per 1% underlying move
        exposure = (
            gamma
            * contract.open_interest
            * _CONTRACT_MULTIPLIER
            * chain.spot**2
            * 0.01
        )
        if contract.option_type == "call":
            call_gex += exposure
            by_strike[contract.strike] = by_strike.get(contract.strike, 0.0) + exposure
        else:
            put_gex -= exposure
            by_strike[contract.strike] = by_strike.get(contract.strike, 0.0) - exposure

    return GexProfile(
        ticker=chain.ticker,
        spot=chain.spot,
        net_gex=call_gex + put_gex,
        call_gex=call_gex,
        put_gex=put_gex,
        by_strike=by_strike,
    )
