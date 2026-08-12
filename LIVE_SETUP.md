# Live Test Runbook

How to take this project from a fresh clone to a running live test, in order,
with a verification gate at every step. Nothing here requires paid services;
the two optional keys (Tradier, Alpaca) come from free accounts.

**The golden rule:** every external integration in this codebase was built and
unit-tested against documented data formats, but the government/market APIs
could not be reached from the development sandbox. The phases below are
ordered so each data source proves itself before anything depends on it.
When a step fails, capture the log output — every source degrades to a
warning rather than a crash, so failures are visible but not fatal.

---

## Phase 0 — Prerequisites

- **Python 3.11+** (`python --version`)
- A machine that can stay on for the duration of the test (your PC is fine
  for the dry run; see Phase 7 for always-on hosting)
- Network access to: `disclosures-clerk.house.gov`, `efdsearch.senate.gov`,
  `unitedstates.github.io`, `query1.finance.yahoo.com`,
  `cdn.finra.org`, `api.finra.org`, `data-api.polymarket.com`,
  and (optional) `api.tradier.com`, Alpaca, Discord

```bash
git clone <your-repo-url>
cd politician-trade-agent
git checkout main                # everything below is merged to main
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,trading]"
pytest -q                        # expect: all tests pass before you start
cp .env.example .env
```

---

## Phase 1 — Congressional data smoke test (no credentials needed)

This is the most important single check: it exercises the House ZIP download,
House PTR PDF parsing, the Senate EFD agreement handshake, Senate PTR HTML
parsing, and the politician-enrichment dataset — with no Discord and no
broker.

```bash
python -m src.main --once
```

**Pass criteria:**
- Log shows `Applied migration` lines, then fetch activity from both sources
- `--once complete: fetched=N new=N` with N > 0 (Congress files constantly;
  a zero on a weekday usually means a source failed — check warnings above)
- At least some `NEW` lines show **real tickers and amount ranges** (e.g.
  `Jane Example | NVDA | purchase | $15,001 - $50,000`) — this proves detail
  parsing, not just metadata scraping
- `(no ticker)` rows are fine: those are paper filings or unparseable PDFs,
  which fall back to metadata-only by design

**Known first-contact risks:**
- *Senate returns nothing:* the EFD agreement handshake is session/IP
  sensitive. Re-run once; if it persists, save the warning traceback.
- *House PDFs parse to 0 rows:* PDF layouts vary. The filing URL is in each
  warning — send a failing PDF link so the regex can be extended.
- Run `--once` a second time: `new=0` expected (dedup proof).

## Phase 2 — Market data smoke test (still no credentials)

```bash
python - <<'EOF'
import asyncio
from src.data.options import YahooOptionsProvider
from src.data.darkpool import DarkPoolService
from src.data.polymarket import PolymarketClient

async def main():
    chain = await YahooOptionsProvider().fetch_chain("SPY")
    print("Yahoo chain:", "OK," if chain and chain.contracts else "FAILED,",
          len(chain.contracts) if chain else 0, "contracts, spot", chain.spot if chain else "-")
    short = await DarkPoolService().get_short_volume("AAPL", days=2)
    print("FINRA short volume:", "OK," if short else "FAILED,", [f"{d.day}:{d.short_ratio:.0%}" for d in short])
    trades = await PolymarketClient().get_recent_trades(limit=25)
    print("Polymarket feed:", "OK," if trades else "FAILED,", len(trades), "trades")

asyncio.run(main())
EOF
```

**Pass criteria:** all three print OK.
- *Yahoo FAILED:* Yahoo occasionally gates its unofficial API behind
  cookie/crumb checks. This only affects the keyless fallback — set up
  Tradier (Phase 5) and the problem disappears.
- *Polymarket FAILED or fields look wrong:* the public data API is
  undocumented; if parse warnings appear, capture one raw response
  (`curl 'https://data-api.polymarket.com/trades?limit=2'`) for a field fix.

## Phase 3 — Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   New Application → **Bot** → copy token → `DISCORD_BOT_TOKEN`
2. OAuth2 URL Generator: scopes `bot` + `applications.commands`; permissions
   Send Messages, Embed Links, Use Slash Commands → invite to your server
3. Enable Developer Mode in Discord → right-click your alerts channel →
   Copy Channel ID → `ALERT_CHANNEL_ID`
4. For fast iteration set `POLL_INTERVAL_MINUTES=2` (restore to 30 after)

```bash
python -m src.main
```

**Pass criteria:**
- Bot shows online; typing `/` lists all commands: `trades`, `top`,
  `politician`, `leaderboard`, `follow`, `unfollow`, `following`, `flow`,
  `gex`, `darkpool`, `polymarket`, `surges`, `portfolio`, `settings`
- First cycle logs `silent backfill` and posts **nothing** (by design —
  history is ingested quietly)
- `/trades` returns rows; `/politician` autocompletes a name from them;
  `/flow SPY` and `/gex SPY` return live embeds; `/leaderboard` computes
  (first run fetches prices; give it ~30s)
- Next cycles alert only on genuinely new filings

## Phase 4 — Dashboard + phone app

In `.env`:
```
ENABLE_DASHBOARD=true
DASHBOARD_AUTH_TOKEN=<generate one: python -c "import secrets;print(secrets.token_urlsafe(24))">
```

Restart the bot (or run `python -m src.main --dashboard-only` for UI-only).
On your PC find your LAN IP (`ipconfig` / `ip a`), then on your phone
(same Wi-Fi): `http://<lan-ip>:8080/?token=<your-token>`.

**Pass criteria:**
- 401 without the token; loads with it; subsequent visits need no token
  (cookie set)
- All panels populate; ticker tools return JSON
- Browser menu → **Add to Home Screen** installs it as a standalone app
- `curl http://<lan-ip>:8080/api/health -H "Authorization: Bearer <token>"`
  shows `last_poll_age_s` under your poll interval — wire this URL into any
  uptime monitor (UptimeRobot free tier works)

Windows: allow Python through Defender Firewall when prompted.

## Phase 5 — Real-time options data (optional, recommended, $0)

1. Open a free brokerage account at [tradier.com](https://tradier.com)
   (no funding required for API access)
2. Dashboard → API Access → create a **production** token
3. `.env`: `TRADIER_API_KEY=<token>` — that's it; `OPTIONS_PROVIDER=auto`
   upgrades every consumer to real-time automatically

**Verify:** `/gex SPY` during market hours; spot should match a live quote
to the penny. Note: real-time quotes are licensed for your personal use —
don't expose the dashboard publicly with a Tradier key attached.

## Phase 6 — Scanners (flow + prediction markets)

```
ENABLE_FLOW_ALERTS=true
FLOW_WATCHLIST=SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,TSLA,GOOGL
FLOW_MIN_PREMIUM_USD=250000

ENABLE_PREDICTION_ALERTS=true
PREDICTION_MIN_BET_USD=10000

# Insider-surge detection runs inside the prediction scanner (on by default).
# Leave these at defaults for the first run; tune after a day of real data.
ENABLE_SURGE_ALERTS=true
SURGE_MIN_WALLETS=5
SURGE_MIN_TOTAL_USD=100000
SURGE_MIN_BUY_RATIO=0.7
# Optional: a free Polygonscan key adds on-chain funder-graph clustering
# (the "nine connected accounts" pattern). Behavioral clustering runs without it.
POLYGONSCAN_API_KEY=
```

**Pass criteria & tuning (give this a full trading day):**
- Flow alerts appear during market hours only; each contract alerts at most
  once per day
- Polymarket alerts show wallet age / record / PnL and at least one signal
- Surge alerts (many wallets converging one-sided) show crowd size,
  one-sidedness, informed-wallet counts, and any cluster note; `/surges`
  lists recent hits and the dashboard panel (`/api/surges`) mirrors them
- **Too noisy?** Raise `FLOW_MIN_PREMIUM_USD` (500K–1M for index-heavy
  watchlists), `PREDICTION_MIN_BET_USD` (25K+), and `SURGE_MIN_WALLETS` /
  `SURGE_MIN_TOTAL_USD`. **Too quiet?** Lower them. Expect one tuning pass
  after the first day and another after a week.

## Phase 7 — Paper trading

```
ENABLE_AUTO_TRADE=true
PAPER_TRADING=true
ALPACA_API_KEY=<paper key>       # from alpaca.markets, free
ALPACA_SECRET_KEY=<paper secret>
TRADE_AMOUNT_USD=500
ENABLE_SELL_MIRROR=false         # buys only until trusted
```

**Pass criteria (run for at least two full weeks):**
- New disclosed purchases with tickers produce ~$500 notional market orders
  visible in the Alpaca paper dashboard
- Restarting the bot never duplicates an order (`executed_trades` dedup)
- Filings without tickers are skipped with a debug log, not errored
- `/portfolio` matches the Alpaca dashboard

## Phase 8 — Always-on deployment

Local dry run done? Move it somewhere that stays up:

- **VPS + systemd** (recommended, ~$4–6/mo: Hetzner CX11, DO basic):
  clone, repeat Phases 0–7, then `deploy/politician-trade-agent.service`
  (edit paths/user) → `systemctl enable --now politician-trade-agent`
- **Docker:** `docker build -t pta . && docker run -d --env-file .env
  -p 8080:8080 -v pta-data:/data --restart unless-stopped pta`
- Set `CACHE_DIR` and `DATABASE_PATH` on a persistent volume; back up
  `trades.db` (it is the entire memory of the system — dedup, alerts,
  executed orders): a nightly `sqlite3 trades.db ".backup backup.db"` cron
  is enough
- CI is included (`.github/workflows/ci.yml`): lint + full test suite on
  every push

## Phase 9 — Going live with real money (only after weeks of clean paper)

1. Generate **live** Alpaca keys; fund the account with an amount you can
   lose without pain
2. `.env`: `PAPER_TRADING=false`, `ALPACA_BASE_URL=https://api.alpaca.markets`
3. Start small: `TRADE_AMOUNT_USD=100`, buys only
4. Check `/portfolio` and the Alpaca dashboard daily for the first week

---

## Considerations before you rely on it

**Strategy reality.** The STOCK Act allows up to 45 days between a
politician's trade and its disclosure; you always enter late. Disclosure
amounts are ranges, not exact sizes. Treat mirroring as one signal input,
not an arbitrage. Mirror orders are *market* orders placed at whatever time
the poll runs.

**Data licensing.** Tradier real-time quotes are for your personal use —
sharing a dashboard that displays them counts as redistribution under
exchange agreements. Keep the dashboard token-protected (Phase 4) and
private, or run keyless (delayed data) if you must share it.

**Source fragility.** house.gov / senate.gov change markup without notice;
Yahoo's endpoints are unofficial; Polymarket's data API is undocumented.
Watch `/api/health` and the logs weekly; budget occasional parser fixes.

**Security.** `.env` holds every secret — never commit it (gitignored),
`chmod 600` it on servers, use the host's secret store where available. If
the dashboard faces the internet, put it behind HTTPS (Caddy/nginx reverse
proxy) in addition to the token.

**Not included** (would need paid data or new work): per-print dark-pool
tape, streaming (sub-minute) options flow, news feeds, earnings calendars,
screeners. The provider interfaces (`OptionsProvider`, `DarkPoolService`)
are where paid feeds would plug in.

## Cost summary

| Item | Cost |
|---|---|
| All data sources used | $0 |
| Tradier account (real-time options data) | $0 |
| Alpaca (paper and live trading) | $0 commissions |
| Discord | $0 |
| VPS hosting (optional until Phase 8) | ~$4–12/mo |
| **Total** | **≈ $5/mo** |

## Troubleshooting quick reference

| Symptom | Likely cause | Action |
|---|---|---|
| Startup exits with config error | `ALERT_CHANNEL_ID` not numeric | Copy the real channel ID (Developer Mode) |
| Senate source fetches 0 | Agreement handshake rejected | Retry; capture warning traceback if persistent |
| House trades all `(no ticker)` | PDF parse failures / paper filings | Send a failing PDF URL from the logs |
| `/flow` says no chain | Yahoo gating | Set `TRADIER_API_KEY` |
| Flow alerts silent all day | Outside market hours, or premium floor too high | Check clock/timezone; lower `FLOW_MIN_PREMIUM_USD` |
| Polymarket panel empty | No bets above floor yet, or API shape drift | Lower floor; check logs for parse warnings |
| Dashboard 401 on phone | Cookie not set yet | Open `/?token=...` once |
| `last_poll_age_s` growing unbounded | Poller crashed silently | Check logs; restart; report traceback |
| Duplicate alerts after restart | Should never happen | Report immediately — dedup regression |
