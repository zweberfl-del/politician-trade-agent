# Politician Trade Agent

A Discord market-data terminal: congressional trade tracking with optional brokerage mirroring, plus options flow, gamma exposure, and dark pool analytics — built entirely on free public data sources.

## Features

**Congressional trading**
- Polls official disclosure sources (House Clerk, Senate EFD) and parses **per-trade detail** — ticker, buy/sell, dates, amount range, owner — from e-filed PTRs (Senate HTML pages, House PDFs)
- Posts trade alerts to a Discord channel, with party/state tags and disclosure-lag stats
- Configurable alert filters: minimum amount, ticker watchlist, party
- Follow specific politicians and receive DM notifications when they trade
- Politician profiles (`/politician`), most-traded tickers (`/top`), and a copy-trade leaderboard ranking politicians by return since disclosure vs SPY (`/leaderboard`)
- Party/state/committee enrichment from the free [congress-legislators](https://github.com/unitedstates/congress-legislators) dataset, plus name autocomplete in commands
- Optional weekly digest post (most-traded tickers, most active politicians)
- Optional auto-trading: mirror politician trades through Alpaca (paper or live)

**Options flow & market data**
- `/flow <ticker>` — call/put volume, put/call ratio, premium totals, and the most unusual contracts (big premium, volume far above open interest)
- Background **unusual-activity alerts** on a configurable watchlist during market hours, deduplicated per contract per day
- `/gex <ticker>` — Black-Scholes gamma exposure: net/call/put GEX, largest strikes, zero-gamma estimate
- `/darkpool <ticker>` — FINRA daily short-volume ratios and weekly dark pool (ATS) volume
- Pluggable chain source: **Tradier** (real-time OPRA quotes with a free brokerage-account API key) or Yahoo (delayed ~15 min, no key) — set `OPTIONS_PROVIDER`

**Web dashboard**
- `ENABLE_DASHBOARD=true` serves a responsive browser dashboard (works on mobile): live congressional trade feed, unusual options flow feed, top tickers, most active politicians, and per-ticker GEX / dark pool lookups — backed by JSON APIs (`/api/trades`, `/api/flow`, `/api/gex/{ticker}`, `/api/darkpool/{ticker}`, …)

**Plumbing**
- SQLite storage with full deduplication — no duplicate alerts or orders; first run backfills history silently instead of flooding the channel
- HTTP retries with backoff and an ETag-aware disk cache for the House disclosure ZIP

## How this compares to Unusual Whales

| Unusual Whales feature | Here | Data source |
|---|---|---|
| Congressional trade tracker | ✅ alerts, profiles, filters | Official House/Senate filings (free) |
| Politician performance/portfolios | ✅ `/politician`, `/leaderboard` | Yahoo daily closes (free) |
| Options flow feed & unusual alerts | ✅ `/flow` + watchlist alerts + dashboard feed | Tradier (real-time, free brokerage key) or Yahoo (delayed, keyless) |
| Greeks / GEX | ✅ `/gex` + `/api/gex` | Black-Scholes from the configured chain source |
| Dark pool | ✅ `/darkpool` + `/api/darkpool` | FINRA aggregates (daily short volume, weekly ATS) |
| Alerts to Discord | ✅ native | — |
| Web UI / mobile | ✅ responsive dashboard | Same database, served by the bot |
| Trade execution | ✅ Alpaca mirroring | UW has no execution at all |

With `OPTIONS_PROVIDER=tradier` and a brokerage-account key (free account),
options data is real-time — the same license-through-a-vendor model UW uses.
Without any key everything still works on delayed/aggregate public data. UW's
per-print dark-pool tape has no free equivalent; FINRA aggregates answer the
same question (off-exchange share and short bias) at daily/weekly resolution.

## Prerequisites

- Python 3.11+
- A Discord bot token (see [Discord Bot Setup](#discord-bot-setup) below)
- (Optional) An [Alpaca](https://alpaca.markets/) account for auto-trading
- (Optional) A **paid** [Finnhub](https://finnhub.io/) plan for the congressional-trading API (the free tier cannot access this endpoint; the bot works fine without it)

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

# Or run a single fetch+store cycle without Discord (smoke test / cron)
python -m src.main --once
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
| `/politician <name>` | Profile: totals, top tickers, committees, recent trades |
| `/leaderboard [days]` | Rank politicians by avg return since disclosure vs SPY |
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
| `ALERT_CHANNEL_ID` | Yes | -- | Numeric Discord channel ID for trade alerts |
| `ENABLE_ALERTS` | No | `true` | Enable posting trade alerts |
| `ENABLE_AUTO_TRADE` | No | `false` | Enable automatic trade mirroring |
| `ENABLE_SELL_MIRROR` | No | `false` | Mirror sell trades (buys only by default) |
| `PAPER_TRADING` | No | `true` | Use Alpaca paper trading endpoint |
| `ENABLE_WEEKLY_DIGEST` | No | `false` | Post a weekly trading digest to the alert channel |
| `POLL_INTERVAL_MINUTES` | No | `30` | How often to poll for new trades |
| `BACKFILL_SILENT` | No | `true` | First run (empty DB) ingests history without alerts/orders |
| `PARSE_FILING_DETAILS` | No | `true` | Fetch per-trade detail from PTR pages/PDFs |
| `MAX_FILINGS_PER_CYCLE` | No | `25` | Max new filings detail-fetched per source per cycle |
| `ENABLE_ENRICHMENT` | No | `true` | Party/state/committee enrichment (weekly refresh) |
| `ALERT_MIN_AMOUNT_USD` | No | `0` | Suppress channel alerts below this disclosed amount |
| `ALERT_TICKER_WATCHLIST` | No | -- | Comma-separated tickers to alert on (empty = all) |
| `ALERT_PARTY_FILTER` | No | -- | Comma-separated party codes D,R,I (empty = all) |
| `TRADE_AMOUNT_USD` | No | `500` | Dollar amount per mirrored trade |
| `FINNHUB_API_KEY` | No | -- | Finnhub API key (paid plan required for this endpoint) |
| `DATABASE_PATH` | No | `trades.db` | Path to the SQLite database file |
| `CACHE_DIR` | No | `.cache` | Directory for cached downloads (empty disables) |
| `ALPACA_API_KEY` | No | -- | Alpaca API key (required if auto-trade is on) |
| `ALPACA_SECRET_KEY` | No | -- | Alpaca secret key |
| `ALPACA_BASE_URL` | No | `https://paper-api.alpaca.markets` | Alpaca API base URL |

## Data Sources

Trade data is aggregated from multiple public sources:

- **House Clerk Financial Disclosures** — Official XML index of House periodic transaction reports. E-filed PTR PDFs are downloaded and parsed for per-trade detail (ticker, type, dates, amount). Scanned paper filings fall back to a metadata-only alert with a link to the PDF.
- **Senate Electronic Financial Disclosure (EFD)** — The Senate's disclosure system. The bot accepts the EFD access agreement, searches recent PTRs, and parses each e-filed report's transaction table for full detail.
- **Finnhub Congressional Trading API** (optional, paid) — Structured trade data for a curated list of commonly-traded tickers. Cross-source duplicates collapse via content-based trade IDs.

Note on latency: the STOCK Act allows members up to 30–45 days to disclose a
trade, so *every* congressional trade tracker (including commercial ones) is
reporting trades days-to-weeks after they happened. Alerts include the
disclosure lag so you can judge staleness at a glance.

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

## Deployment

- **Docker**: `docker build -t politician-trade-agent .` then run with your
  `.env` (`docker run --env-file .env -v pta-data:/data politician-trade-agent`).
- **systemd**: see `deploy/politician-trade-agent.service`.

Whichever host you pick, `.env` holds real secrets — never commit it, and
prefer the host's secret/env-var mechanism over a file on disk.

## License

MIT
