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


class TradierOptionsProvider(OptionsProvider):
    """Options chains from the Tradier brokerage API.

    Data grade follows the account attached to the key: a (free) Tradier
    brokerage account receives **real-time** OPRA quotes; a sandbox key
    receives 15-minute-delayed data. Set ``OPTIONS_PROVIDER=tradier`` and
    ``TRADIER_API_KEY`` to enable.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        max_expirations: int = 4,
    ) -> None:
        from src.config import settings

        self.api_key = api_key or settings.tradier_api_key
        self.base_url = (base_url or settings.tradier_base_url).rstrip("/")
        self.max_expirations = max_expirations

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def fetch_chain(self, ticker: str) -> OptionsChain | None:
        ticker = ticker.upper()
        if not self.api_key:
            logger.warning("TradierOptionsProvider: no API key configured")
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=self._headers) as client:
                quote_resp = await request_with_retry(
                    client,
                    "GET",
                    f"{self.base_url}/v1/markets/quotes",
                    params={"symbols": ticker},
                )
                quote_resp.raise_for_status()
                quote = quote_resp.json().get("quotes", {}).get("quote", {})
                if isinstance(quote, list):
                    quote = quote[0] if quote else {}
                spot = float(quote.get("last") or 0.0)

                exp_resp = await request_with_retry(
                    client,
                    "GET",
                    f"{self.base_url}/v1/markets/options/expirations",
                    params={"symbol": ticker},
                )
                exp_resp.raise_for_status()
                dates = (exp_resp.json().get("expirations") or {}).get("date", [])
                if isinstance(dates, str):
                    dates = [dates]

                chain = OptionsChain(ticker=ticker, spot=spot)
                for expiry in dates[: self.max_expirations]:
                    chain_resp = await request_with_retry(
                        client,
                        "GET",
                        f"{self.base_url}/v1/markets/options/chains",
                        params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
                    )
                    chain_resp.raise_for_status()
                    options = (chain_resp.json().get("options") or {}).get("option", [])
                    for raw in options if isinstance(options, list) else [options]:
                        contract = parse_tradier_contract(raw, ticker)
                        if contract is not None:
                            chain.contracts.append(contract)
                return chain
        except Exception:
            logger.warning("TradierOptionsProvider: failed for %s", ticker, exc_info=True)
            return None


def parse_tradier_contract(raw: dict, ticker: str) -> OptionContract | None:
    """Convert one raw Tradier chain entry; None when malformed."""
    try:
        greeks = raw.get("greeks") or {}
        return OptionContract(
            contract_symbol=raw.get("symbol", ""),
            ticker=ticker,
            option_type=str(raw["option_type"]).lower(),
            strike=float(raw["strike"]),
            expiration=date.fromisoformat(raw["expiration_date"]),
            last_price=float(raw.get("last") or 0.0),
            bid=float(raw.get("bid") or 0.0),
            ask=float(raw.get("ask") or 0.0),
            volume=int(raw.get("volume") or 0),
            open_interest=int(raw.get("open_interest") or 0),
            implied_volatility=float(greeks.get("mid_iv") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_options_provider() -> OptionsProvider:
    """Pick the best available chain source.

    Real-time is the default whenever it's possible: a Tradier key upgrades
    every consumer (flow scanner, /flow, /gex, dashboard) to real-time OPRA
    quotes automatically. Set ``OPTIONS_PROVIDER=yahoo`` to force delayed
    chains even with a key present.
    """
    from src.config import settings

    choice = settings.options_provider.lower()
    if choice == "yahoo":
        return YahooOptionsProvider()
    if settings.tradier_api_key:
        return TradierOptionsProvider()
    if choice == "tradier":
        logger.warning(
            "OPTIONS_PROVIDER=tradier but TRADIER_API_KEY is empty — "
            "falling back to delayed Yahoo chains"
        )
    return YahooOptionsProvider()


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
