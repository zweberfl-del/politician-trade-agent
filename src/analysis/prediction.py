"""Unusual-activity detection for prediction markets.

Flags the wallet-forensics patterns that make a Polymarket bet suspicious:

- **fresh-wallet** — an account created (or first active) within hours of
  placing a large bet: classic profile for someone acting on information
  they don't want tied to their main wallet.
- **sharp-wallet** — an account with an outlier win record placing another
  large bet: whatever they know, it has been right before.
- **longshot-conviction** — big money buying a heavily-discounted outcome:
  size at 10c implies someone disagrees hard with the market.
- **whale-size** — raw size well beyond the alert floor.

Pure functions over ``PredictionTrade`` + ``WalletProfile`` so thresholds
and edge cases are unit-testable without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data.polymarket import PredictionTrade, WalletProfile

FRESH_WALLET_MAX_AGE_HOURS = 48.0
FRESH_WALLET_MAX_ACTIVITY = 5
SHARP_MIN_RESOLVED = 8
SHARP_MIN_WIN_RATE = 0.75
SHARP_MIN_PNL_USD = 100_000.0
SHARP_PNL_MIN_WIN_RATE = 0.60
LONGSHOT_MAX_PRICE = 0.15
WHALE_MULTIPLE = 5.0


@dataclass
class UnusualBet:
    trade: PredictionTrade
    profile: WalletProfile
    signals: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def headline(self) -> str:
        return ", ".join(self.signals)


def wallet_signals(
    trade: PredictionTrade,
    profile: WalletProfile,
    *,
    min_bet_usd: float,
    fresh_max_age_hours: float = FRESH_WALLET_MAX_AGE_HOURS,
    now: float | None = None,
) -> list[str]:
    """Return the list of suspicion signals for one bet (may be empty)."""
    signals: list[str] = []

    age = profile.age_hours(now)
    brand_new = age is not None and age <= fresh_max_age_hours
    barely_used = profile.activity_count <= FRESH_WALLET_MAX_ACTIVITY
    if brand_new or (barely_used and profile.first_seen_ts is not None):
        signals.append("fresh-wallet")

    consistent_winner = (
        profile.resolved_positions >= SHARP_MIN_RESOLVED
        and profile.win_rate >= SHARP_MIN_WIN_RATE
    )
    big_pnl_winner = (
        profile.total_pnl_usd >= SHARP_MIN_PNL_USD
        and profile.win_rate >= SHARP_PNL_MIN_WIN_RATE
    )
    if consistent_winner or big_pnl_winner:
        signals.append("sharp-wallet")

    if trade.side == "BUY" and 0 < trade.price <= LONGSHOT_MAX_PRICE:
        signals.append("longshot-conviction")

    if trade.usd_size >= min_bet_usd * WHALE_MULTIPLE:
        signals.append("whale-size")

    return signals


def classify_bet(
    trade: PredictionTrade,
    profile: WalletProfile,
    *,
    min_bet_usd: float,
    now: float | None = None,
) -> UnusualBet | None:
    """Score one bet; None unless it's large AND carries a wallet signal.

    Size alone doesn't alert (whales exist); a signal alone doesn't alert
    (fresh wallets place $5 bets constantly). The combination is the tell.
    """
    if trade.usd_size < min_bet_usd:
        return None
    signals = wallet_signals(trade, profile, min_bet_usd=min_bet_usd, now=now)
    if not signals:
        return None

    # Dollars carry the score; each independent signal compounds it.
    score = trade.usd_size * (1.5 ** len(signals))
    return UnusualBet(trade=trade, profile=profile, signals=signals, score=score)


def classify_bets(
    trades_with_profiles: list[tuple[PredictionTrade, WalletProfile]],
    *,
    min_bet_usd: float,
    limit: int = 10,
    now: float | None = None,
) -> list[UnusualBet]:
    """Classify a batch, highest score first."""
    hits = []
    for trade, profile in trades_with_profiles:
        bet = classify_bet(trade, profile, min_bet_usd=min_bet_usd, now=now)
        if bet is not None:
            hits.append(bet)
    hits.sort(key=lambda b: b.score, reverse=True)
    return hits[:limit]
