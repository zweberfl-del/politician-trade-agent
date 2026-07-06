from __future__ import annotations

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
