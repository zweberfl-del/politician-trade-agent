"""Options chain data provider.

The default provider uses Yahoo Finance's public options endpoint (delayed
~15 minutes, no key required). The ``OptionsProvider`` interface exists so a
licensed real-time feed (OPRA via Polygon, Tradier, etc.) can be dropped in
without touching the analysis layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from src.data.http_util import request_with_retry

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


@dataclass
class OptionContract:
    contract_symbol: str
    ticker: str
    option_type: str  # "call" or "put"
    strike: float
    expiration: date
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    open_interest: int = 0
    implied_volatility: float = 0.0

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last_price

    @property
    def premium_notional(self) -> float:
        """Total premium traded today at the mid price (1 contract = 100 shares)."""
        return self.volume * self.mid_price * 100

    def days_to_expiry(self, today: date | None = None) -> int:
        return max(0, (self.expiration - (today or date.today())).days)


@dataclass
class OptionsChain:
    ticker: str
    spot: float
    contracts: list[OptionContract] = field(default_factory=list)

    @property
    def calls(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type == "call"]

    @property
    def puts(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type == "put"]


class OptionsProvider:
    """Interface for options chain sources."""

    async def fetch_chain(self, ticker: str) -> OptionsChain | None:
        raise NotImplementedError


class YahooOptionsProvider(OptionsProvider):
    """Delayed (~15 min) options chains from Yahoo Finance, no key needed."""

    URL = "https://query1.finance.yahoo.com/v7/finance/options/{ticker}"

    def __init__(self, *, max_expirations: int = 4) -> None:
        # Near-dated expirations dominate flow and gamma; fetching every
        # expiry would be slow and rarely changes the picture.
        self.max_expirations = max_expirations

    async def fetch_chain(self, ticker: str) -> OptionsChain | None:
        ticker = ticker.upper()
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=_UA) as client:
                first = await self._fetch_expiry(client, ticker, None)
                if first is None:
                    return None
                payload, chain = first

                expirations = payload.get("expirationDates", [])[: self.max_expirations]
                # The first response already contains the nearest expiry.
                for expiry_ts in expirations[1:]:
                    extra = await self._fetch_expiry(client, ticker, expiry_ts)
                    if extra is not None:
                        chain.contracts.extend(extra[1].contracts)
                return chain
        except Exception:
            logger.warning("YahooOptionsProvider: failed for %s", ticker, exc_info=True)
            return None

    async def _fetch_expiry(
        self, client: httpx.AsyncClient, ticker: str, expiry_ts: int | None
    ) -> tuple[dict, OptionsChain] | None:
        params = {"date": expiry_ts} if expiry_ts else {}
        resp = await request_with_retry(
            client, "GET", self.URL.format(ticker=ticker), params=params
        )
        resp.raise_for_status()
        result = resp.json().get("optionChain", {}).get("result", [])
        if not result:
            return None
        payload = result[0]
        spot = float(payload.get("quote", {}).get("regularMarketPrice") or 0.0)
        chain = OptionsChain(ticker=ticker, spot=spot)
        for options in payload.get("options", []):
            for kind in ("calls", "puts"):
                for raw in options.get(kind, []):
                    contract = parse_yahoo_contract(raw, ticker, kind[:-1])
                    if contract is not None:
                        chain.contracts.append(contract)
        return payload, chain


def parse_yahoo_contract(raw: dict, ticker: str, option_type: str) -> OptionContract | None:
    """Convert one raw Yahoo contract dict; None when malformed."""
    try:
        expiration = datetime.fromtimestamp(
            int(raw["expiration"]), tz=timezone.utc
        ).date()
        return OptionContract(
            contract_symbol=raw.get("contractSymbol", ""),
            ticker=ticker,
            option_type=option_type,
            strike=float(raw["strike"]),
            expiration=expiration,
            last_price=float(raw.get("lastPrice") or 0.0),
            bid=float(raw.get("bid") or 0.0),
            ask=float(raw.get("ask") or 0.0),
            volume=int(raw.get("volume") or 0),
            open_interest=int(raw.get("openInterest") or 0),
            implied_volatility=float(raw.get("impliedVolatility") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None
