from __future__ import annotations

import json
import logging
from datetime import date

import discord

from src.data.models import PoliticianTrade, TransactionType, Chamber
from src.trading.base import Position, AccountInfo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------

_COLOR_GREEN = 0x2ECC71
_COLOR_RED = 0xE74C3C
_COLOR_GRAY = 0x95A5A6
_COLOR_BLUE = 0x3498DB
_COLOR_GOLD = 0xF1C40F
_COLOR_PURPLE = 0x9B59B6


def _transaction_color(tx_type: TransactionType) -> int:
    """Return an embed colour based on the transaction type."""
    if tx_type == TransactionType.PURCHASE:
        return _COLOR_GREEN
    if tx_type == TransactionType.SALE:
        return _COLOR_RED
    return _COLOR_GRAY


def _format_date(d: date | None) -> str:
    """Format a date as 'Jan 15, 2024' or return 'N/A'."""
    if d is None:
        return "N/A"
    return d.strftime("%b %d, %Y")


# ---------------------------------------------------------------------------
# Trade alert embed
# ---------------------------------------------------------------------------


def trade_alert_embed(trade: PoliticianTrade) -> discord.Embed:
    """Build a rich embed for a single politician trade alert."""

    who = trade.politician_name
    tags = "/".join(t for t in (trade.party, trade.state) if t)
    if tags:
        who = f"{who} ({tags})"
    title = f"{who} \u2014 {trade.transaction_type.value.upper()}"
    color = _transaction_color(trade.transaction_type)

    embed = discord.Embed(title=title, color=color)

    if trade.filing_url:
        embed.url = trade.filing_url

    embed.add_field(name="Ticker", value=trade.ticker or "N/A", inline=True)
    embed.add_field(name="Asset", value=trade.asset_name or "N/A", inline=True)
    embed.add_field(name="Amount", value=trade.amount_range or "Unknown", inline=True)
    embed.add_field(
        name="Date",
        value=_format_date(trade.transaction_date),
        inline=True,
    )
    embed.add_field(name="Owner", value=trade.owner or "N/A", inline=True)

    chamber_label = "House" if trade.chamber == Chamber.HOUSE else "Senate"
    embed.add_field(name="Chamber", value=chamber_label, inline=True)

    # Footer: source + filing date + disclosure lag
    footer_parts: list[str] = []
    if trade.source:
        footer_parts.append(f"Source: {trade.source}")
    if trade.filing_date is not None:
        footer_parts.append(f"Filed: {_format_date(trade.filing_date)}")
        if trade.transaction_date is not None:
            lag = (trade.filing_date - trade.transaction_date).days
            if lag > 0:
                footer_parts.append(f"{lag} days after the trade")
    if footer_parts:
        embed.set_footer(text=" | ".join(footer_parts))

    return embed


# ---------------------------------------------------------------------------
# Recent trades list embed
# ---------------------------------------------------------------------------


def trades_list_embed(
    trades: list[dict],
    title: str = "Recent Trades",
) -> discord.Embed:
    """Build an embed showing a list of recent trades.

    *trades* should be raw dicts as returned by ``get_recent_trades()``.
    """

    embed = discord.Embed(title=title, color=_COLOR_BLUE)

    if not trades:
        embed.description = "No trades found."
        embed.set_footer(text="0 trades")
        return embed

    lines: list[str] = []
    for t in trades:
        politician = t.get("politician_name", "?")
        ticker = t.get("ticker") or "N/A"
        tx_type = (t.get("transaction_type") or "unknown").upper()
        amount = t.get("amount_range") or "Unknown"
        tx_date = t.get("transaction_date") or "N/A"
        lines.append(f"{politician} | {ticker} | {tx_type} | {amount} | {tx_date}")

    # Discord embeds have a 4096-char description limit; truncate if needed.
    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:4000] + "\n..."

    embed.description = description
    embed.set_footer(text=f"{len(trades)} trade{'s' if len(trades) != 1 else ''}")

    return embed


# ---------------------------------------------------------------------------
# Top tickers embed
# ---------------------------------------------------------------------------


def top_tickers_embed(tickers: list[dict], days: int) -> discord.Embed:
    """Build an embed showing the most-traded tickers.

    *tickers* should be raw dicts from ``get_top_tickers()`` with keys:
    ``ticker``, ``trade_count``, ``buys``, ``sells``, ``politician_count``.
    """

    embed = discord.Embed(
        title=f"Top Traded Tickers \u2014 Last {days} Days",
        color=_COLOR_GOLD,
    )

    if not tickers:
        embed.description = f"No trades in the last {days} days."
        return embed

    lines: list[str] = []
    for idx, t in enumerate(tickers, start=1):
        ticker = t.get("ticker", "?")
        total = t.get("trade_count", 0)
        buys = t.get("buys", 0)
        sells = t.get("sells", 0)
        politicians = t.get("politician_count", 0)
        lines.append(
            f"{idx}. **{ticker}** \u2014 {total} trade{'s' if total != 1 else ''} "
            f"({buys} buy{'s' if buys != 1 else ''}, {sells} sell{'s' if sells != 1 else ''}) "
            f"by {politicians} politician{'s' if politicians != 1 else ''}"
        )

    embed.description = "\n".join(lines)
    return embed


# ---------------------------------------------------------------------------
# Portfolio embed
# ---------------------------------------------------------------------------


def portfolio_embed(
    positions: list[Position],
    account: AccountInfo,
) -> discord.Embed:
    """Build an embed showing the mirrored portfolio."""

    embed = discord.Embed(title="Mirrored Portfolio", color=_COLOR_PURPLE)

    embed.add_field(name="Equity", value=f"${account.equity:,.2f}", inline=True)
    embed.add_field(name="Cash", value=f"${account.cash:,.2f}", inline=True)
    embed.add_field(
        name="Buying Power", value=f"${account.buying_power:,.2f}", inline=True
    )

    if not positions:
        embed.description = "No open positions."
    else:
        lines: list[str] = []
        for p in positions:
            lines.append(
                f"{p.ticker}: {p.quantity} shares "
                f"@ ${p.avg_entry_price:.2f} "
                f"(P&L: ${p.unrealized_pl:+.2f})"
            )
        embed.description = "\n".join(lines)

    return embed


# ---------------------------------------------------------------------------
# Politician profile embed
# ---------------------------------------------------------------------------


def politician_profile_embed(
    name: str,
    stats: dict,
    recent_trades: list[dict],
    info: dict | None = None,
) -> discord.Embed:
    """Profile card for one politician: identity, totals, top tickers, recent trades.

    *stats* comes from ``get_politician_stats()``; *info* (optional) from the
    enrichment dataset with keys party/state/chamber/committees/full_name.
    """
    display = (info or {}).get("full_name") or name
    tags = "/".join(
        t for t in ((info or {}).get("party", ""), (info or {}).get("state", "")) if t
    )
    title = f"{display} ({tags})" if tags else display

    embed = discord.Embed(title=title, color=_COLOR_BLUE)

    total = stats.get("total") or 0
    embed.add_field(name="Disclosed Trades", value=str(total), inline=True)
    embed.add_field(
        name="Buys / Sells",
        value=f"{stats.get('buys') or 0} / {stats.get('sells') or 0}",
        inline=True,
    )
    embed.add_field(name="Distinct Tickers", value=str(stats.get("tickers") or 0), inline=True)

    top = stats.get("top_tickers") or []
    if top:
        lines = [
            f"**{t['ticker']}** — {t['trade_count']} ({t['buys']}B/{t['sells']}S)"
            for t in top
        ]
        embed.add_field(name="Most Traded", value="\n".join(lines), inline=False)

    committees = (info or {}).get("committees") or []
    if committees:
        embed.add_field(
            name="Committees", value="\n".join(f"• {c}" for c in committees[:5]), inline=False
        )

    if recent_trades:
        lines = []
        for t in recent_trades[:5]:
            ticker = t.get("ticker") or "N/A"
            tx_type = (t.get("transaction_type") or "unknown").upper()
            amount = t.get("amount_range") or "Unknown"
            tx_date = t.get("transaction_date") or t.get("filing_date") or "N/A"
            lines.append(f"{tx_date} | {ticker} | {tx_type} | {amount}")
        embed.add_field(name="Recent Trades", value="\n".join(lines), inline=False)

    if stats.get("first_date"):
        embed.set_footer(text=f"Records from {stats['first_date']} to {stats.get('last_date')}")
    return embed


# ---------------------------------------------------------------------------
# Leaderboard embed
# ---------------------------------------------------------------------------


def leaderboard_embed(board: list[dict], days: int) -> discord.Embed:
    """Politicians ranked by average excess return vs SPY since disclosure."""
    embed = discord.Embed(
        title=f"Copy-Trade Leaderboard — Purchases, Last {days} Days",
        description=(
            "Average return since each purchase was *disclosed* (what a mirror "
            "could capture), versus SPY over the same window."
        ),
        color=_COLOR_GOLD,
    )
    if not board:
        embed.description = (
            "Not enough purchase data with resolvable prices yet. "
            "Try again after more disclosures are ingested."
        )
        return embed

    lines = []
    for idx, row in enumerate(board, start=1):
        lines.append(
            f"{idx}. **{row['politician_name']}** — "
            f"{row['avg_return_pct']:+.1f}% avg "
            f"({row['avg_excess_pct']:+.1f}% vs SPY, {row['trades']} buys)"
        )
    embed.add_field(name="Rankings", value="\n".join(lines), inline=False)
    return embed


# ---------------------------------------------------------------------------
# Weekly digest embed
# ---------------------------------------------------------------------------


def weekly_digest_embed(
    top_tickers: list[dict],
    most_active: list[dict],
) -> discord.Embed:
    """Weekly summary: hottest tickers and most active traders."""
    embed = discord.Embed(title="Weekly Congressional Trading Digest", color=_COLOR_PURPLE)

    if top_tickers:
        lines = [
            f"**{t['ticker']}** — {t['trade_count']} trades "
            f"({t['buys']}B/{t['sells']}S) by {t['politician_count']} politicians"
            for t in top_tickers[:8]
        ]
        embed.add_field(name="Most Traded Tickers", value="\n".join(lines), inline=False)

    if most_active:
        lines = [
            f"**{p['politician_name']}** — {p['trade_count']} trades "
            f"({p['buys']}B/{p['sells']}S)"
            for p in most_active[:8]
        ]
        embed.add_field(name="Most Active Politicians", value="\n".join(lines), inline=False)

    if not top_tickers and not most_active:
        embed.description = "No congressional trading activity recorded this week."
    return embed


# ---------------------------------------------------------------------------
# Options flow embeds
# ---------------------------------------------------------------------------


def _fmt_premium(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def flow_alert_embed(hit) -> discord.Embed:
    """Alert for one unusual options contract (analysis.flow.UnusualActivity)."""
    c = hit.contract
    color = _COLOR_GREEN if hit.sentiment == "bullish" else _COLOR_RED
    embed = discord.Embed(
        title=(
            f"Unusual Options Activity — {c.ticker} "
            f"{c.option_type.upper()} ${c.strike:g} {c.expiration:%m/%d/%y}"
        ),
        color=color,
    )
    embed.add_field(name="Premium", value=_fmt_premium(c.premium_notional), inline=True)
    embed.add_field(name="Volume", value=f"{c.volume:,}", inline=True)
    embed.add_field(name="Open Interest", value=f"{c.open_interest:,}", inline=True)
    embed.add_field(name="Vol/OI", value=f"{hit.vol_oi_ratio:.1f}x", inline=True)
    embed.add_field(name="Spot", value=f"${hit.spot:,.2f}", inline=True)
    embed.add_field(
        name="Moneyness",
        value=f"{hit.otm_pct:+.1f}% {'OTM' if hit.otm_pct >= 0 else 'ITM'}",
        inline=True,
    )
    embed.set_footer(text=f"{hit.sentiment.title()} | delayed data (~15 min)")
    return embed


def flow_summary_embed(ticker: str, hits: list, sentiment: dict) -> discord.Embed:
    """Answer for /flow: sentiment snapshot plus the top unusual contracts."""
    embed = discord.Embed(title=f"Options Flow — {ticker.upper()}", color=_COLOR_BLUE)
    embed.add_field(
        name="Call / Put Volume",
        value=f"{sentiment['call_volume']:,} / {sentiment['put_volume']:,}",
        inline=True,
    )
    embed.add_field(
        name="Put/Call Ratio", value=f"{sentiment['put_call_ratio']:.2f}", inline=True
    )
    embed.add_field(
        name="Premium C/P",
        value=f"{_fmt_premium(sentiment['call_premium'])} / "
        f"{_fmt_premium(sentiment['put_premium'])}",
        inline=True,
    )

    if hits:
        lines = []
        for hit in hits[:8]:
            c = hit.contract
            lines.append(
                f"{c.option_type.upper()} ${c.strike:g} {c.expiration:%m/%d} — "
                f"{_fmt_premium(c.premium_notional)} prem, {c.volume:,} vol "
                f"({hit.vol_oi_ratio:.1f}x OI)"
            )
        embed.add_field(name="Unusual Contracts", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="Unusual Contracts", value="Nothing above thresholds right now.", inline=False
        )
    embed.set_footer(text="Delayed data (~15 min)")
    return embed


def gex_embed(profile) -> discord.Embed:
    """Answer for /gex (analysis.gex.GexProfile)."""
    regime = "positive (vol-dampening)" if profile.net_gex >= 0 else "negative (vol-amplifying)"
    embed = discord.Embed(
        title=f"Gamma Exposure — {profile.ticker}",
        description=f"Dealer gamma regime: **{regime}**",
        color=_COLOR_GOLD,
    )
    embed.add_field(name="Net GEX / 1% move", value=_fmt_premium(profile.net_gex), inline=True)
    embed.add_field(name="Call GEX", value=_fmt_premium(profile.call_gex), inline=True)
    embed.add_field(name="Put GEX", value=_fmt_premium(profile.put_gex), inline=True)
    embed.add_field(name="Spot", value=f"${profile.spot:,.2f}", inline=True)

    flip = profile.zero_gamma_estimate
    if flip is not None:
        embed.add_field(name="Zero-Gamma (est.)", value=f"${flip:g}", inline=True)

    strikes = profile.top_strikes()
    if strikes:
        lines = [f"${strike:g}: {_fmt_premium(exposure)}" for strike, exposure in strikes]
        embed.add_field(name="Largest Strikes", value="\n".join(lines), inline=False)
    embed.set_footer(text="Black-Scholes from delayed chains | dealer-positioning convention")
    return embed


def darkpool_embed(ticker: str, short_days: list, ats_weeks: list[dict]) -> discord.Embed:
    """Answer for /darkpool: FINRA short volume + ATS weekly aggregates."""
    embed = discord.Embed(title=f"Off-Exchange Activity — {ticker.upper()}", color=_COLOR_PURPLE)

    if short_days:
        lines = [
            f"{d.day:%m/%d}: {d.short_ratio * 100:.0f}% short "
            f"({d.short_volume:,} / {d.total_volume:,})"
            for d in short_days
        ]
        embed.add_field(name="Daily Short Volume (FINRA)", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="Daily Short Volume (FINRA)", value="No recent data.", inline=False
        )

    if ats_weeks:
        lines = [
            f"{w['week']}: {w['shares']:,} shares in {w['trades']:,} trades"
            for w in ats_weeks
        ]
        embed.add_field(name="Dark Pool (ATS) Weekly Volume", value="\n".join(lines), inline=False)

    embed.set_footer(text="FINRA public data: daily short volume, weekly ATS aggregates")
    return embed


# ---------------------------------------------------------------------------
# Prediction-market embeds
# ---------------------------------------------------------------------------

_SIGNAL_LABELS = {
    "fresh-wallet": "🆕 Fresh wallet",
    "sharp-wallet": "🎯 Proven winner",
    "longshot-conviction": "🎲 Longshot conviction",
    "whale-size": "🐋 Whale size",
}


def prediction_alert_embed(bet) -> discord.Embed:
    """Alert for one unusual prediction-market bet (analysis.prediction.UnusualBet)."""
    t, p = bet.trade, bet.profile
    embed = discord.Embed(
        title=f"Unusual Polymarket Bet — {t.market_title or 'Unknown market'}",
        color=_COLOR_GOLD,
    )
    if t.market_url:
        embed.url = t.market_url

    embed.add_field(
        name="Bet",
        value=f"{t.side} **{t.outcome or '?'}** @ {t.price * 100:.0f}c",
        inline=True,
    )
    embed.add_field(name="Size", value=_fmt_premium(t.usd_size), inline=True)
    who = t.pseudonym or f"{t.wallet[:6]}…{t.wallet[-4:]}"
    embed.add_field(name="Wallet", value=who, inline=True)

    embed.add_field(
        name="Signals",
        value="\n".join(_SIGNAL_LABELS.get(s, s) for s in bet.signals),
        inline=False,
    )

    profile_bits = []
    age = p.age_hours()
    if age is not None:
        profile_bits.append(
            f"{age:.0f}h old" if age < 72 else f"{age / 24:.0f}d old"
        )
    if p.resolved_positions:
        profile_bits.append(
            f"{p.wins}-{p.losses} record ({p.win_rate * 100:.0f}%)"
        )
    if abs(p.total_pnl_usd) >= 1:
        profile_bits.append(f"{_fmt_premium(p.total_pnl_usd)} lifetime PnL")
    if profile_bits:
        embed.add_field(name="Wallet Profile", value=" | ".join(profile_bits), inline=False)

    embed.set_footer(text="Polymarket public data")
    return embed


def prediction_list_embed(events: list[dict]) -> discord.Embed:
    """Answer for /polymarket: recent unusual bets from the database."""
    embed = discord.Embed(title="Unusual Polymarket Activity", color=_COLOR_GOLD)
    if not events:
        embed.description = (
            "No unusual bets recorded yet. The scanner flags large bets from "
            "fresh wallets, proven winners, and longshot conviction."
        )
        return embed

    lines = []
    for e in events[:10]:
        try:
            signals = ", ".join(json.loads(e.get("signals") or "[]"))
        except ValueError:
            signals = ""
        who = e.get("pseudonym") or (e.get("wallet") or "")[:10]
        lines.append(
            f"**{_fmt_premium(e.get('usd_size') or 0)}** {e.get('side', '')} "
            f"{e.get('outcome', '?')} @ {(e.get('price') or 0) * 100:.0f}c — "
            f"{e.get('market_title', '')[:60]}\n"
            f"    {who} ({signals})"
        )
    embed.description = "\n".join(lines)[:4000]
    return embed


# ---------------------------------------------------------------------------
# Insider-surge embeds
# ---------------------------------------------------------------------------

_CATEGORY_LABELS = {
    "military": "🪖 Military",
    "geopolitical": "🌍 Geopolitical",
    "election": "🗳️ Election",
    "economy": "📉 Economy",
    "crypto": "🪙 Crypto",
    "other": "Other",
}


def surge_alert_embed(surge) -> discord.Embed:
    """Alert for one detected insider surge (analysis.surge.Surge)."""
    color = _COLOR_RED if surge.category in ("military", "geopolitical") else _COLOR_GOLD
    embed = discord.Embed(
        title=f"🚨 Insider Surge — {surge.market_title or 'Unknown market'}",
        color=color,
    )
    if surge.market_url:
        embed.url = surge.market_url

    embed.add_field(
        name="Converging on",
        value=f"BUY **{surge.outcome or '?'}** — {int(surge.buy_ratio * 100)}% one-sided",
        inline=True,
    )
    embed.add_field(name="Wallets", value=str(surge.wallet_count), inline=True)
    embed.add_field(name="Total", value=_fmt_premium(surge.total_usd), inline=True)

    embed.add_field(
        name="Category",
        value=_CATEGORY_LABELS.get(surge.category, surge.category or "Other"),
        inline=True,
    )
    informed = []
    if surge.fresh_count:
        informed.append(f"🆕 {surge.fresh_count} fresh")
    if surge.sharp_count:
        informed.append(f"🎯 {surge.sharp_count} proven")
    embed.add_field(
        name="Informed wallets", value=" · ".join(informed) or "—", inline=True
    )

    if surge.cluster_note:
        embed.add_field(name="🔗 Cluster", value=surge.cluster_note, inline=False)

    if surge.reasons:
        embed.add_field(name="Why", value="\n".join(f"• {r}" for r in surge.reasons), inline=False)

    embed.set_footer(text="Polymarket public data · surge detection")
    return embed


def surge_list_embed(surges: list[dict]) -> discord.Embed:
    """Answer for /surges: recent insider surges from the database."""
    embed = discord.Embed(title="🚨 Insider Surges", color=_COLOR_GOLD)
    if not surges:
        embed.description = (
            "No surges recorded yet. The scanner flags many wallets converging "
            "one-sided on the same outcome — strongest on military/geopolitical markets."
        )
        return embed

    lines = []
    for s in surges[:10]:
        cat = _CATEGORY_LABELS.get(s.get("category", ""), s.get("category") or "")
        note = s.get("cluster_note") or ""
        lines.append(
            f"**{_fmt_premium(s.get('total_usd') or 0)}** · "
            f"{int(s.get('wallet_count') or 0)} wallets → "
            f"{s.get('outcome', '?')} — {(s.get('market_title') or '')[:55]}\n"
            f"    {cat}" + (f" · 🔗 {note}" if note else "")
        )
    embed.description = "\n".join(lines)[:4000]
    return embed


# ---------------------------------------------------------------------------
# Settings embed
# ---------------------------------------------------------------------------


def settings_embed(settings_obj) -> discord.Embed:
    """Build an embed showing current bot configuration toggles."""

    embed = discord.Embed(title="Bot Settings", color=_COLOR_GRAY)

    def _toggle(value: bool) -> str:
        return "\u2705 On" if value else "\u274c Off"

    embed.add_field(
        name="Alerts", value=_toggle(settings_obj.enable_alerts), inline=True
    )
    embed.add_field(
        name="Auto-Trade", value=_toggle(settings_obj.enable_auto_trade), inline=True
    )
    embed.add_field(
        name="Sell Mirror", value=_toggle(settings_obj.enable_sell_mirror), inline=True
    )
    embed.add_field(
        name="Paper Trading", value=_toggle(settings_obj.paper_trading), inline=True
    )
    embed.add_field(
        name="Poll Interval",
        value=f"{settings_obj.poll_interval_minutes} min",
        inline=True,
    )
    embed.add_field(
        name="Trade Amount",
        value=f"${settings_obj.trade_amount_usd:,.2f}",
        inline=True,
    )

    return embed
