from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.config import settings
from src.storage.database import (
    get_recent_trades,
    get_top_tickers,
    follow_politician,
    unfollow_politician,
    get_followed_politicians,
    get_politician_stats,
    get_purchases_since,
    search_politician_names,
)
from src.bot.embeds import (
    trades_list_embed,
    top_tickers_embed,
    portfolio_embed,
    settings_embed,
    politician_profile_embed,
    leaderboard_embed,
    flow_summary_embed,
    gex_embed,
    darkpool_embed,
    prediction_list_embed,
)

if TYPE_CHECKING:
    from src.data.enrichment import EnrichmentService
    from src.trading.base import Broker

log = logging.getLogger(__name__)


async def _politician_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest politician names already seen in the trades table."""
    try:
        names = await search_politician_names(settings.database_path, current)
    except Exception:
        log.debug("Autocomplete lookup failed", exc_info=True)
        return []
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# ---------------------------------------------------------------------------
# Slash command definitions
# ---------------------------------------------------------------------------


async def setup_commands(
    bot: discord.Client,
    tree: app_commands.CommandTree,
    broker: Broker | None = None,
    enrichment: EnrichmentService | None = None,
) -> None:
    """Register all slash commands on the given command tree.

    Parameters
    ----------
    bot:
        The Discord client instance.
    tree:
        The ``CommandTree`` to register commands on.
    broker:
        An optional broker instance used by the ``/portfolio`` command.
    enrichment:
        Optional politician-metadata service used by ``/politician``.
    """

    # -- /trades [politician] [ticker] ------------------------------------

    @tree.command(name="trades", description="Show recent politician stock trades")
    @app_commands.describe(
        politician="Filter by politician name",
        ticker="Filter by stock ticker",
    )
    @app_commands.autocomplete(politician=_politician_autocomplete)
    async def trades_cmd(
        interaction: discord.Interaction,
        politician: str | None = None,
        ticker: str | None = None,
    ) -> None:
        await interaction.response.defer()

        results = await get_recent_trades(
            settings.database_path,
            limit=15,
            politician=politician,
            ticker=ticker,
        )

        title = "Recent Trades"
        if politician and ticker:
            title = f"Recent Trades \u2014 {politician} ({ticker.upper()})"
        elif politician:
            title = f"Recent Trades \u2014 {politician}"
        elif ticker:
            title = f"Recent Trades \u2014 {ticker.upper()}"

        embed = trades_list_embed(results, title=title)
        await interaction.followup.send(embed=embed)

    # -- /top [days] ------------------------------------------------------

    @tree.command(
        name="top", description="Show the most-traded tickers by politicians"
    )
    @app_commands.describe(days="Number of days to look back (default: 30)")
    async def top_cmd(
        interaction: discord.Interaction,
        days: int = 30,
    ) -> None:
        await interaction.response.defer()

        results = await get_top_tickers(settings.database_path, days=days)
        embed = top_tickers_embed(results, days)
        await interaction.followup.send(embed=embed)

    # -- /follow <politician> ---------------------------------------------

    @tree.command(
        name="follow",
        description="Get DM alerts when a politician makes a trade",
    )
    @app_commands.describe(politician="Name of the politician to follow")
    @app_commands.autocomplete(politician=_politician_autocomplete)
    async def follow_cmd(
        interaction: discord.Interaction,
        politician: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        await follow_politician(
            settings.database_path,
            str(interaction.user.id),
            politician,
        )
        await interaction.followup.send(
            f"You are now following **{politician}**. "
            f"You will receive a DM when they disclose a new trade.",
            ephemeral=True,
        )

    # -- /unfollow <politician> -------------------------------------------

    @tree.command(
        name="unfollow",
        description="Stop receiving DM alerts for a politician",
    )
    @app_commands.describe(politician="Name of the politician to unfollow")
    @app_commands.autocomplete(politician=_politician_autocomplete)
    async def unfollow_cmd(
        interaction: discord.Interaction,
        politician: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        await unfollow_politician(
            settings.database_path,
            str(interaction.user.id),
            politician,
        )
        await interaction.followup.send(
            f"You have unfollowed **{politician}**.",
            ephemeral=True,
        )

    # -- /following -------------------------------------------------------

    @tree.command(
        name="following",
        description="List politicians you are currently following",
    )
    async def following_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        names = await get_followed_politicians(
            settings.database_path,
            str(interaction.user.id),
        )

        if not names:
            await interaction.followup.send(
                "You are not following any politicians. "
                "Use `/follow <name>` to start.",
                ephemeral=True,
            )
            return

        listing = "\n".join(f"\u2022 {name}" for name in names)
        await interaction.followup.send(
            f"**Politicians you follow:**\n{listing}",
            ephemeral=True,
        )

    # -- /politician <name> -------------------------------------------------

    @tree.command(
        name="politician",
        description="Profile of a politician's disclosed trading",
    )
    @app_commands.describe(name="Politician to look up")
    @app_commands.autocomplete(name=_politician_autocomplete)
    async def politician_cmd(interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()

        stats = await get_politician_stats(settings.database_path, name)
        if not (stats.get("total") or 0):
            await interaction.followup.send(
                f"No disclosed trades on record for **{name}**.", ephemeral=True
            )
            return

        recent = await get_recent_trades(settings.database_path, limit=5, politician=name)
        info = enrichment.lookup(name) if enrichment is not None else None
        await interaction.followup.send(
            embed=politician_profile_embed(name, stats, recent, info)
        )

    # -- /leaderboard [days] --------------------------------------------------

    @tree.command(
        name="leaderboard",
        description="Politicians ranked by return since disclosure vs SPY",
    )
    @app_commands.describe(days="Look-back window in days (default: 180)")
    async def leaderboard_cmd(interaction: discord.Interaction, days: int = 180) -> None:
        from src.data.prices import (
            PriceService,
            compute_performance,
            leaderboard_from_performance,
        )

        await interaction.response.defer()
        days = max(7, min(days, 365))

        try:
            purchases = await get_purchases_since(settings.database_path, days=days)
            performance = await compute_performance(
                PriceService(settings.database_path), purchases
            )
            board = leaderboard_from_performance(performance)
        except Exception:
            log.exception("Leaderboard computation failed")
            await interaction.followup.send(
                "Could not compute the leaderboard (price data unavailable). "
                "Try again later.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(embed=leaderboard_embed(board, days))

    # -- /flow <ticker> -------------------------------------------------------

    @tree.command(
        name="flow",
        description="Options flow snapshot: sentiment and unusual contracts",
    )
    @app_commands.describe(ticker="Underlying ticker, e.g. SPY")
    async def flow_cmd(interaction: discord.Interaction, ticker: str) -> None:
        from src.analysis.flow import chain_sentiment, detect_unusual_activity
        from src.data.options import build_options_provider

        await interaction.response.defer()
        chain = await build_options_provider().fetch_chain(ticker)
        if chain is None or not chain.contracts:
            await interaction.followup.send(
                f"No options chain available for **{ticker.upper()}**.", ephemeral=True
            )
            return
        hits = detect_unusual_activity(
            chain, min_premium_usd=settings.flow_min_premium_usd
        )
        await interaction.followup.send(
            embed=flow_summary_embed(ticker, hits, chain_sentiment(chain))
        )

    # -- /gex <ticker> --------------------------------------------------------

    @tree.command(
        name="gex",
        description="Gamma exposure profile for a ticker",
    )
    @app_commands.describe(ticker="Underlying ticker, e.g. SPY")
    async def gex_cmd(interaction: discord.Interaction, ticker: str) -> None:
        from src.analysis.gex import compute_gex
        from src.data.options import build_options_provider

        await interaction.response.defer()
        chain = await build_options_provider().fetch_chain(ticker)
        if chain is None or not chain.contracts or chain.spot <= 0:
            await interaction.followup.send(
                f"No options chain available for **{ticker.upper()}**.", ephemeral=True
            )
            return
        await interaction.followup.send(embed=gex_embed(compute_gex(chain)))

    # -- /darkpool <ticker> -----------------------------------------------------

    @tree.command(
        name="darkpool",
        description="Off-exchange activity: FINRA short volume and dark pool (ATS) data",
    )
    @app_commands.describe(ticker="Ticker, e.g. AAPL")
    async def darkpool_cmd(interaction: discord.Interaction, ticker: str) -> None:
        from src.data.darkpool import DarkPoolService

        await interaction.response.defer()
        service = DarkPoolService()
        short_days = await service.get_short_volume(ticker)
        ats_weeks = await service.get_ats_weekly(ticker)
        if not short_days and not ats_weeks:
            await interaction.followup.send(
                f"No off-exchange data available for **{ticker.upper()}** right now.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=darkpool_embed(ticker, short_days, ats_weeks))

    # -- /polymarket ------------------------------------------------------------

    @tree.command(
        name="polymarket",
        description="Recent unusual prediction-market bets (fresh/sharp wallets, big size)",
    )
    async def polymarket_cmd(interaction: discord.Interaction) -> None:
        from src.storage.database import get_recent_prediction_events

        await interaction.response.defer()
        events = await get_recent_prediction_events(settings.database_path)
        await interaction.followup.send(embed=prediction_list_embed(events))

    # -- /settings --------------------------------------------------------

    @tree.command(name="settings", description="Show current bot settings")
    async def settings_cmd(interaction: discord.Interaction) -> None:
        embed = settings_embed(settings)
        await interaction.response.send_message(embed=embed)

    # -- /portfolio -------------------------------------------------------

    @tree.command(
        name="portfolio",
        description="Show the current mirrored trading portfolio",
    )
    async def portfolio_cmd(interaction: discord.Interaction) -> None:
        if broker is None or not settings.enable_auto_trade:
            await interaction.response.send_message(
                "Auto-trading is not enabled.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            positions = await broker.get_positions()
            account = await broker.get_account()
        except Exception:
            log.exception("Failed to fetch portfolio data from broker")
            await interaction.followup.send(
                "Failed to retrieve portfolio data. Check the logs for details.",
                ephemeral=True,
            )
            return

        embed = portfolio_embed(positions, account)
        await interaction.followup.send(embed=embed)

    log.info("Registered %d slash commands", len(tree.get_commands()))
