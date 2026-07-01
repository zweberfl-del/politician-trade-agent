# Politician Trade Agent

A Discord bot that tracks US congressional stock trades in real time and optionally mirrors them through a brokerage account.

## Features

- Polls public disclosure sources (House Clerk, Senate EFD, Finnhub) for new politician trades
- Posts real-time trade alerts to a Discord channel
- Follow specific politicians and receive DM notifications when they trade
- View the most-traded tickers across Congress
- Optional auto-trading: mirror politician trades through Alpaca (paper or live)
- SQLite storage with full deduplication -- no duplicate alerts or orders
- Configurable polling interval, trade amount, and sell-mirroring

## Prerequisites

- Python 3.11+
- A Discord bot token (see [Discord Bot Setup](#discord-bot-setup) below)
- (Optional) An [Alpaca](https://alpaca.markets/) account for auto-trading
- (Optional) A [Finnhub](https://finnhub.io/) API key for enriched trade data

## Quick Start

```bash
# Clone the repository
git clone https://github.com/your-org/politician-trade-agent.git
cd politician-trade-agent

# Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Copy the example env file and fill in your values
cp .env.example .env
# Edit .env with your DISCORD_BOT_TOKEN and ALERT_CHANNEL_ID

# Run the bot
python -m src.main
```

## Discord Bot Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Under **Bot**, click **Add Bot** and copy the **Token** -- this is your `DISCORD_BOT_TOKEN`.
3. Under **Bot**, enable the **Message Content Intent** if you want future message-based features (currently not required).
4. Under **OAuth2 > URL Generator**, select the scopes **bot** and **applications.commands**.
5. In the **Bot Permissions** section, select:
   - Send Messages
   - Embed Links
   - Use Slash Commands
6. Copy the generated URL and open it in your browser to invite the bot to your server.
7. Right-click the channel where you want alerts posted, select **Copy Channel ID** (enable Developer Mode in Discord settings if needed), and set it as `ALERT_CHANNEL_ID` in your `.env`.

## Slash Commands

| Command | Description |
|---|---|
| `/trades [politician] [ticker]` | Show recent politician stock trades, optionally filtered |
| `/top [days]` | Show the most-traded tickers by politicians (default: 30 days) |
| `/follow <politician>` | Get DM alerts when a politician discloses a new trade |
| `/unfollow <politician>` | Stop receiving DM alerts for a politician |
| `/following` | List the politicians you are currently following |
| `/portfolio` | Show the current mirrored trading portfolio (requires auto-trade) |
| `/settings` | Show current bot configuration |

## Configuration

All configuration is done through environment variables (or a `.env` file).

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | -- | Discord bot token |
| `ALERT_CHANNEL_ID` | Yes | -- | Discord channel ID for trade alerts |
| `ENABLE_ALERTS` | No | `true` | Enable posting trade alerts |
| `ENABLE_AUTO_TRADE` | No | `false` | Enable automatic trade mirroring |
| `ENABLE_SELL_MIRROR` | No | `false` | Mirror sell trades (buys only by default) |
| `PAPER_TRADING` | No | `true` | Use Alpaca paper trading endpoint |
| `POLL_INTERVAL_MINUTES` | No | `30` | How often to poll for new trades |
| `TRADE_AMOUNT_USD` | No | `500` | Dollar amount per mirrored trade |
| `FINNHUB_API_KEY` | No | -- | Finnhub API key for enriched trade data |
| `DATABASE_PATH` | No | `trades.db` | Path to the SQLite database file |
| `ALPACA_API_KEY` | No | -- | Alpaca API key (required if auto-trade is on) |
| `ALPACA_SECRET_KEY` | No | -- | Alpaca secret key |
| `ALPACA_BASE_URL` | No | `https://paper-api.alpaca.markets` | Alpaca API base URL |

## Data Sources

Trade data is aggregated from multiple public sources:

- **House Clerk Financial Disclosures** -- Official XML index of House representative periodic transaction reports. Provides filing metadata (filer, date, document ID, PDF link). Updated as filings are submitted.
- **Senate Electronic Financial Disclosure (EFD)** -- The Senate's searchable disclosure system. The bot queries for recent periodic transaction reports and extracts filer information.
- **Finnhub Congressional Trading API** (optional) -- Provides structured trade details (ticker, amount, buy/sell) for a curated list of commonly-traded tickers. Requires a free API key.

The bot merges results from all enabled sources and deduplicates by a deterministic trade ID before storing them in the database.

## Trading Setup (Optional)

To enable automatic trade mirroring through Alpaca:

1. Create an account at [alpaca.markets](https://alpaca.markets/).
2. Generate API keys from the dashboard (start with paper trading).
3. Set these values in your `.env`:
   ```
   ENABLE_AUTO_TRADE=true
   ALPACA_API_KEY=your-key
   ALPACA_SECRET_KEY=your-secret
   ```
4. By default only purchase trades are mirrored. Set `ENABLE_SELL_MIRROR=true` to also mirror sell trades.
5. Adjust `TRADE_AMOUNT_USD` to control how much is invested per trade.

Install the trading extras:
```bash
pip install -e ".[trading]"
```

## License

MIT
