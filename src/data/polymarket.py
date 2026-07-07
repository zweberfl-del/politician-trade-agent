"""Polymarket public data API client.

Polymarket's data API (data-api.polymarket.com) is free and unauthenticated:

- ``/trades`` — recent fills across all markets, including the taker's proxy
  wallet, pseudonym, share size, price, market title, and outcome.
- ``/activity?user=<wallet>`` — a wallet's on-platform history; the earliest
  entry dates the account, the count sizes its track record.
- ``/positions?user=<wallet>`` — the wallet's positions with realized cash
  PnL, which is what a win/loss record is computed from.

Parsing is split into pure functions so detection logic is testable without
a network.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from src.data.http_util import request_with_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://data-api.polymarket.com"


@dataclass
class PredictionTrade:
    """One fill on a prediction market."""

    wallet: str
    pseudonym: str = ""
    market_title: str = ""
    market_slug: str = ""
    outcome: str = ""
    side: str = ""  # "BUY" or "SELL"
    shares: float = 0.0
    price: float = 0.0  # implied probability, 0..1
    timestamp: int = 0
    tx_hash: str = ""

    @property
    def usd_size(self) -> float:
        """Dollars committed (shares are $1-if-right claims priced 0..1)."""
        return self.shares * self.price

    @property
    def event_key(self) -> str:
        return self.tx_hash or f"{self.wallet}|{self.market_slug}|{self.outcome}|{self.timestamp}"

    @property
    def market_url(self) -> str:
        return f"https://polymarket.com/event/{self.market_slug}" if self.market_slug else ""


@dataclass
class WalletProfile:
    """A wallet's track record, as far as public data shows."""

    wallet: str
    first_seen_ts: int | None = None
    activity_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0

    def age_hours(self, now: float | None = None) -> float | None:
        if not self.first_seen_ts:
            return None
        return max(0.0, ((now or time.time()) - self.first_seen_ts) / 3600)

    @property
    def resolved_positions(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.resolved_positions if self.resolved_positions else 0.0


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def parse_trade(raw: dict) -> PredictionTrade | None:
    """Convert one raw ``/trades`` row; None when malformed."""
    try:
        wallet = (raw.get("proxyWallet") or raw.get("wallet") or "").lower()
        if not wallet:
            return None
        return PredictionTrade(
            wallet=wallet,
            pseudonym=raw.get("pseudonym") or raw.get("name") or "",
            market_title=raw.get("title") or "",
            market_slug=raw.get("eventSlug") or raw.get("slug") or "",
            outcome=raw.get("outcome") or "",
            side=(raw.get("side") or "").upper(),
            shares=float(raw.get("size") or 0.0),
            price=float(raw.get("price") or 0.0),
            timestamp=int(raw.get("timestamp") or 0),
            tx_hash=raw.get("transactionHash") or "",
        )
    except (TypeError, ValueError):
        return None


def build_wallet_profile(
    wallet: str, activity: list[dict], positions: list[dict]
) -> WalletProfile:
    """Assemble a WalletProfile from raw ``/activity`` and ``/positions`` rows.

    A position counts as resolved when it carries non-zero realized cash PnL
    or is flagged redeemable (market resolved); wins are positive PnL.
    """
    timestamps = [int(a.get("timestamp") or 0) for a in activity if a.get("timestamp")]
    wins = losses = 0
    total_pnl = 0.0
    for pos in positions:
        try:
            pnl = float(pos.get("cashPnl") or 0.0)
        except (TypeError, ValueError):
            continue
        total_pnl += pnl
        resolved = bool(pos.get("redeemable")) or abs(pnl) > 0.01
        if not resolved:
            continue
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    return WalletProfile(
        wallet=wallet.lower(),
        first_seen_ts=min(timestamps) if timestamps else None,
        activity_count=len(activity),
        wins=wins,
        losses=losses,
        total_pnl_usd=total_pnl,
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class PolymarketClient:
    """Read-only client for the public data API."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def get_recent_trades(self, *, limit: int = 200) -> list[PredictionTrade]:
        rows = await self._get_json("/trades", {"limit": limit, "takerOnly": "true"})
        trades = [t for t in (parse_trade(r) for r in rows or []) if t is not None]
        return trades

    async def get_wallet_profile(self, wallet: str) -> WalletProfile:
        activity = await self._get_json(
            "/activity", {"user": wallet, "limit": 500, "sortDirection": "ASC"}
        )
        positions = await self._get_json("/positions", {"user": wallet, "limit": 500})
        return build_wallet_profile(wallet, activity or [], positions or [])

    async def _get_json(self, path: str, params: dict) -> list[dict] | None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(
                    client, "GET", f"{self.base_url}{path}", params=params
                )
                resp.raise_for_status()
                payload = resp.json()
            return payload if isinstance(payload, list) else payload.get("data", [])
        except Exception:
            logger.warning("PolymarketClient: GET %s failed", path, exc_info=True)
            return None
