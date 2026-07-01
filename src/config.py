from __future__ import annotations

from pydantic_settings import BaseSettings
from pydantic import Field


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

    # --- Polling ---
    poll_interval_minutes: int = 30

    # --- Trading ---
    trade_amount_usd: float = 500.0

    # --- Alpaca (optional) ---
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # --- Data Sources ---
    finnhub_api_key: str = ""

    # --- Database ---
    database_path: str = "trades.db"


settings = Settings()
