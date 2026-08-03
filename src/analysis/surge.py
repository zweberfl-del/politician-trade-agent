"""Insider-surge detection for prediction markets.

The per-bet classifier (``analysis.prediction``) answers "is *this wallet*
suspicious?". This module answers the question the 60 Minutes / Bubblemaps
reporting actually raised: **are many wallets converging on the same outcome
in a short window** — the "300 bets in the 24h before the strike" pattern.

A surge is a group of large fills on one ``(market, outcome)`` inside a
rolling window that is (a) crowded — enough distinct wallets, (b) big — enough
total money, and (c) one-sided — overwhelmingly buying the same side. Military
and geopolitical markets score highest, because that's where informed money
showed up. Pure functions over lightweight inputs, so it's fully testable
without the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.analysis.prediction import is_fresh_wallet, is_sharp_wallet
from src.data.categories import classify_market, is_high_stakes
from src.data.polymarket import PredictionTrade, WalletProfile

# Defaults (the scanner overrides these from settings).
DEFAULT_WINDOW_SECONDS = 24 * 3600
DEFAULT_MIN_WALLETS = 5
DEFAULT_MIN_TOTAL_USD = 100_000.0
DEFAULT_MIN_BUY_RATIO = 0.7
HIGH_STAKES_BOOST = 3.0
# Bucket the window end to a stable key so the same surge isn't re-alerted
# every cycle as new fills trickle in.
_BUCKET_SECONDS = 6 * 3600


@dataclass
class SurgeTrade:
    """One large fill, flattened for surge grouping (source-agnostic)."""

    wallet: str
    market_slug: str
    market_title: str
    market_url: str
    outcome: str
    side: str  # "BUY" / "SELL"
    usd_size: float
    timestamp: int
    category: str = ""

    @classmethod
    def from_trade(cls, trade: PredictionTrade, category: str | None = None) -> SurgeTrade:
        return cls(
            wallet=trade.wallet,
            market_slug=trade.market_slug,
            market_title=trade.market_title,
            market_url=trade.market_url,
            outcome=trade.outcome,
            side=trade.side,
            usd_size=trade.usd_size,
            timestamp=trade.timestamp,
            category=category if category is not None
            else classify_market(trade.market_title, trade.market_slug),
        )


@dataclass
class Surge:
    market_title: str
    market_slug: str
    market_url: str
    outcome: str
    category: str
    window_start: int
    window_end: int
    wallet_count: int
    total_usd: float
    buy_ratio: float
    fresh_count: int
    sharp_count: int
    top_wallets: list[str] = field(default_factory=list)
    cluster_note: str = ""
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def surge_key(self) -> str:
        bucket = self.window_end // _BUCKET_SECONDS
        return f"{self.market_slug}|{self.outcome}|{bucket}"


def detect_surges(
    trades: list[SurgeTrade],
    profiles_by_wallet: dict[str, WalletProfile] | None = None,
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    min_wallets: int = DEFAULT_MIN_WALLETS,
    min_total_usd: float = DEFAULT_MIN_TOTAL_USD,
    min_buy_ratio: float = DEFAULT_MIN_BUY_RATIO,
    now: float | None = None,
) -> list[Surge]:
    """Group recent fills into surges, highest score first.

    A ``(market_slug, outcome)`` group qualifies when, inside the window, it
    has at least *min_wallets* distinct wallets, at least *min_total_usd* in
    buys, and a buy share (by dollars) of at least *min_buy_ratio*.
    """
    now = time.time() if now is None else now
    profiles_by_wallet = profiles_by_wallet or {}
    cutoff = now - window_seconds

    groups: dict[tuple[str, str], list[SurgeTrade]] = {}
    for t in trades:
        if t.timestamp < cutoff:
            continue
        groups.setdefault((t.market_slug, t.outcome), []).append(t)

    surges: list[Surge] = []
    for (_slug, _outcome), fills in groups.items():
        buys = [f for f in fills if f.side == "BUY"]
        buy_usd = sum(f.usd_size for f in buys)
        total_usd = sum(f.usd_size for f in fills)
        if total_usd <= 0:
            continue
        buy_ratio = buy_usd / total_usd

        wallets = {f.wallet for f in buys}
        if len(wallets) < min_wallets:
            continue
        if buy_usd < min_total_usd:
            continue
        if buy_ratio < min_buy_ratio:
            continue

        fresh = sum(
            1 for w in wallets
            if w in profiles_by_wallet and is_fresh_wallet(profiles_by_wallet[w], now=now)
        )
        sharp = sum(
            1 for w in wallets
            if w in profiles_by_wallet and is_sharp_wallet(profiles_by_wallet[w])
        )

        category = next((f.category for f in fills if f.category), "") or classify_market(
            fills[0].market_title, _slug
        )
        # Top wallets by dollars committed to the winning side.
        by_wallet: dict[str, float] = {}
        for f in buys:
            by_wallet[f.wallet] = by_wallet.get(f.wallet, 0.0) + f.usd_size
        top_wallets = [w for w, _ in sorted(by_wallet.items(), key=lambda kv: kv[1], reverse=True)]

        reasons = [
            f"{len(wallets)} wallets bought {int(buy_ratio * 100)}% one-sided",
            f"{_fmt_usd(buy_usd)} in {int(window_seconds / 3600)}h",
        ]
        if fresh:
            reasons.append(f"{fresh} fresh wallet(s)")
        if sharp:
            reasons.append(f"{sharp} proven winner(s)")
        if is_high_stakes(category):
            reasons.append(f"{category} market")

        # Score: dollars, amplified by informed participants and topic.
        signal_bonus = 1.0 + 0.5 * fresh + 0.5 * sharp
        stakes = HIGH_STAKES_BOOST if is_high_stakes(category) else 1.0
        score = buy_usd * signal_bonus * stakes

        win_ts = [f.timestamp for f in buys]
        surges.append(
            Surge(
                market_title=next((f.market_title for f in fills if f.market_title), ""),
                market_slug=_slug,
                market_url=next((f.market_url for f in fills if f.market_url), ""),
                outcome=_outcome,
                category=category,
                window_start=min(win_ts),
                window_end=max(win_ts),
                wallet_count=len(wallets),
                total_usd=buy_usd,
                buy_ratio=buy_ratio,
                fresh_count=fresh,
                sharp_count=sharp,
                top_wallets=top_wallets,
                reasons=reasons,
                score=score,
            )
        )

    surges.sort(key=lambda s: s.score, reverse=True)
    return surges


def _fmt_usd(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"
