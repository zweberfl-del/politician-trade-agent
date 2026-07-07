"""Unusual options activity detection.

Scores each contract in a chain snapshot the way flow platforms do: big
premium, volume far above open interest (fresh positioning, not churn), and
near-dated contracts weighted up. Pure functions over an OptionsChain so the
logic is testable without a network.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.data.options import OptionContract, OptionsChain


@dataclass
class UnusualActivity:
    contract: OptionContract
    spot: float
    score: float
    vol_oi_ratio: float

    @property
    def sentiment(self) -> str:
        """Naive direction read: call buying bullish, put buying bearish."""
        return "bullish" if self.contract.option_type == "call" else "bearish"

    @property
    def otm_pct(self) -> float:
        """How far out of the money, as % of spot (negative = ITM)."""
        if self.spot <= 0:
            return 0.0
        if self.contract.option_type == "call":
            return (self.contract.strike - self.spot) / self.spot * 100
        return (self.spot - self.contract.strike) / self.spot * 100


def detect_unusual_activity(
    chain: OptionsChain,
    *,
    min_premium_usd: float = 100_000.0,
    min_volume: int = 100,
    min_vol_oi_ratio: float = 2.0,
    max_days_to_expiry: int = 90,
    limit: int = 10,
) -> list[UnusualActivity]:
    """Return the most unusual contracts in a chain, highest score first.

    A contract qualifies when today's volume is a multiple of existing open
    interest (new positioning) AND the premium traded is large. Open interest
    of 0 with real volume counts as maximally fresh.
    """
    hits: list[UnusualActivity] = []
    for contract in chain.contracts:
        if contract.volume < min_volume:
            continue
        if contract.days_to_expiry() > max_days_to_expiry:
            continue
        premium = contract.premium_notional
        if premium < min_premium_usd:
            continue

        vol_oi = contract.volume / contract.open_interest if contract.open_interest else float(
            contract.volume
        )
        if vol_oi < min_vol_oi_ratio:
            continue

        # Premium carries the score; the vol/OI multiple boosts it, capped so
        # a tiny-OI contract doesn't dwarf a genuinely huge print.
        score = premium * min(vol_oi, 10.0)
        hits.append(
            UnusualActivity(
                contract=contract, spot=chain.spot, score=score, vol_oi_ratio=vol_oi
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def chain_sentiment(chain: OptionsChain) -> dict:
    """Aggregate call/put volume and premium for a ticker snapshot."""
    call_vol = sum(c.volume for c in chain.calls)
    put_vol = sum(c.volume for c in chain.puts)
    call_prem = sum(c.premium_notional for c in chain.calls)
    put_prem = sum(c.premium_notional for c in chain.puts)
    return {
        "call_volume": call_vol,
        "put_volume": put_vol,
        "put_call_ratio": (put_vol / call_vol) if call_vol else 0.0,
        "call_premium": call_prem,
        "put_premium": put_prem,
    }
