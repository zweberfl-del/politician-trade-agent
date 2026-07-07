from __future__ import annotations

import sys

from pydantic import ValidationError
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # --- Discord ---
    discord_bot_token: str = ""
    alert_channel_id: int = 0

    # --- Feature Toggles ---
    enable_alerts: bool = True
    enable_auto_trade: bool = False
    enable_sell_mirror: bool = False
    paper_trading: bool = True
    enable_weekly_digest: bool = False

    # --- Polling ---
    poll_interval_minutes: int = 30

    # --- Ingestion behaviour ---
    # When the trades table is empty (first run), ingest history without
    # posting alerts or mirroring orders so the channel isn't flooded.
    backfill_silent: bool = True
    # Fetch per-trade detail (Senate PTR pages, House PTR PDFs). Bounded per
    # cycle to stay polite to the government servers.
    parse_filing_details: bool = True
    max_filings_per_cycle: int = 25
    # Enrich politicians with party/state/committees from the free
    # unitedstates/congress-legislators dataset.
    enable_enrichment: bool = True

    # --- Options flow / market data ---
    # Scan options chains for unusual activity and post alerts (market hours).
    enable_flow_alerts: bool = False
    flow_watchlist: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,TSLA,GOOGL"
    flow_min_premium_usd: float = 250_000.0
    flow_poll_minutes: int = 10
    # Chain source: "auto" uses Tradier (real-time OPRA quotes with a free
    # brokerage-account key) whenever TRADIER_API_KEY is set, otherwise
    # Yahoo (delayed ~15 min, no key). Set "yahoo" or "tradier" to force one.
    options_provider: str = "auto"
    tradier_api_key: str = ""
    tradier_base_url: str = "https://api.tradier.com"

    # --- Web dashboard ---
    enable_dashboard: bool = False
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080

    # --- Alert filters ---
    # Minimum disclosed amount (lower bound of the range) for channel alerts.
    alert_min_amount_usd: float = 0.0
    # Comma-separated tickers; empty means alert on everything.
    alert_ticker_watchlist: str = ""
    # Comma-separated party codes (D,R,I); empty means all parties.
    alert_party_filter: str = ""

    # --- Trading ---
    trade_amount_usd: float = 500.0

    # --- Alpaca (optional) ---
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # --- Data Sources ---
    finnhub_api_key: str = ""

    # --- Storage ---
    database_path: str = "trades.db"
    # Directory for cached downloads (House FD ZIPs). Empty disables caching.
    cache_dir: str = ".cache"

    def watchlist_tickers(self) -> set[str]:
        return {t.strip().upper() for t in self.alert_ticker_watchlist.split(",") if t.strip()}

    def flow_watchlist_tickers(self) -> list[str]:
        return [t.strip().upper() for t in self.flow_watchlist.split(",") if t.strip()]

    def party_filter(self) -> set[str]:
        return {p.strip().upper()[:1] for p in self.alert_party_filter.split(",") if p.strip()}


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid configuration (.env or environment variables):"]
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"])
            lines.append(f"  {field.upper()}: {err['msg']} (got {err.get('input')!r})")
        lines.append(
            "Hint: ALERT_CHANNEL_ID must be the numeric channel ID "
            "(right-click the channel in Discord with Developer Mode on)."
        )
        print("\n".join(lines), file=sys.stderr)
        sys.exit(1)


settings = _load_settings()
