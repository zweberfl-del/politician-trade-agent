# Setup Guide — Dry Run to Deployment

> **Note:** this was the original Windows-oriented bootstrap guide. The
> canonical, up-to-date runbook covering every feature (dashboard, scanners,
> Polymarket, real-time data, paper→live trading) is
> **[LIVE_SETUP.md](LIVE_SETUP.md)** — use that for a live test.

This bot has never been run. Follow these phases in order: get it working locally
against paper trading first, then deploy once it's proven stable.

---

## Phase 0 — Install Python

Not currently installed on this machine (`python`/`python3` resolve to Microsoft
Store install shims, not a real interpreter). Needs **3.11+**. Pick one:

**Option A — winget (fastest, from a terminal)**
```powershell
winget install Python.Python.3.11
```
Close and reopen your terminal afterward so PATH updates take effect.

**Option B — Microsoft Store**
Run `python` in a terminal — Windows will prompt to open the Store listing.
Install from there.

**Option C — Already have Python elsewhere**
If you have it via pyenv, conda, or a specific install path, just make sure
that interpreter is what `python`/`python3` resolves to, or use its full path
in the commands below.

**Verify:**
```powershell
python --version   # should print 3.11.x or higher
```

---

## Phase 1 — Install the project

```powershell
cd C:\Users\zwebc\politician-trade-agent

# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install the project + dev tools + trading (Alpaca) extras
pip install -e ".[dev,trading]"
```

---

## Phase 2 — Get your credentials

### Discord bot token + channel ID

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Add Bot** → copy the **Token** → this is `DISCORD_BOT_TOKEN`.
3. **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands`.
   Bot permissions: **Send Messages**, **Embed Links**, **Use Slash Commands**.
4. Open the generated URL, invite the bot to a test server (create one if you
   don't have a throwaway server for this).
5. In Discord: enable **Developer Mode** (User Settings → Advanced), then
   right-click the channel for alerts → **Copy Channel ID** → this is
   `ALERT_CHANNEL_ID`.

### Alpaca paper trading keys (no real money involved)

1. Sign up at [alpaca.markets](https://alpaca.markets/) — free.
2. Dashboard → generate API keys under the **paper trading** tab (not live).
3. Copy the **Key ID** (`ALPACA_API_KEY`) and **Secret Key** (`ALPACA_SECRET_KEY`).
4. Leave `ALPACA_BASE_URL` as the paper endpoint (already the default).

### Finnhub key (optional, for richer trade data)

Free tier at [finnhub.io](https://finnhub.io/) — not required to run.

---

## Phase 3 — Configure `.env`

```powershell
copy .env.example .env
```

Edit `.env`:

```
DISCORD_BOT_TOKEN=<from Phase 2>
ALERT_CHANNEL_ID=<from Phase 2>

ENABLE_ALERTS=true
ENABLE_AUTO_TRADE=true
ENABLE_SELL_MIRROR=false
PAPER_TRADING=true          # keep true until you fully trust the bot

POLL_INTERVAL_MINUTES=30
TRADE_AMOUNT_USD=500

ALPACA_API_KEY=<from Phase 2>
ALPACA_SECRET_KEY=<from Phase 2>
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Keep `PAPER_TRADING=true` and `ALPACA_BASE_URL` pointed at the paper endpoint
for all dry runs. Do not flip to live trading until you've watched it behave
correctly over multiple polling cycles.

---

## Phase 4 — Dry run

Start with a single fetch cycle that skips Discord and trading entirely —
it verifies the government data sources, detail parsing, and database work
before you wire up the bot:

```powershell
python -m src.main --once
```

Then run the full bot:

```powershell
python -m src.main
```

What to check, in order:
1. **Startup** — no exceptions, bot logs in and shows as online in your test server.
2. **Slash commands registered** — type `/` in the server, confirm `/trades`,
   `/top`, `/follow`, `/unfollow`, `/following`, `/portfolio`, `/settings` show up.
3. **First poll** — after `POLL_INTERVAL_MINUTES` (lower this to `1` in `.env`
   temporarily for faster iteration), confirm it fetches from House Clerk /
   Senate EFD without errors.
4. **Alert posting** — if new trades are found, confirm an embed posts to the
   alert channel.
5. **Paper order** — if `ENABLE_AUTO_TRADE=true` and a buy trade fires, check
   the Alpaca paper dashboard for the resulting order.
6. **Persistence** — stop and restart the bot, confirm it doesn't re-alert or
   re-order on trades it already processed (dedup via `trades.db`).

Run the test suite too:
```powershell
pytest
```

Expect to hit and fix real bugs here — this code has not been executed before.
Report errors back and they'll get fixed directly.

---

## Phase 5 — Deploy (after dry run is stable)

Once it runs cleanly for a few polling cycles without babysitting, it just
needs to run continuously somewhere. This is a lightweight polling bot, not a
web service — no need for anything heavy.

Options, roughly in order of simplicity:

- **Small always-on VPS** (DigitalOcean droplet, Fly.io, Hetzner) — clone the
  repo, repeat Phases 1–3, run under `systemd`, `pm2`, or a simple `screen`/
  `tmux` session with auto-restart.
- **Docker container** — wrap `python -m src.main` in a container, deploy to
  any container host (Fly.io, Railway, a VPS with Docker).
- **Scheduled/managed compute** — a small cloud VM instance kept running.

Whichever host you pick, `.env` holds real secrets — never commit it (already
gitignored) and set the values via the host's secret/env-var mechanism instead
of a checked-in file.

Only flip `PAPER_TRADING=false` and point `ALPACA_BASE_URL` at the live
endpoint once you've watched enough paper cycles to trust the logic with real
money.
