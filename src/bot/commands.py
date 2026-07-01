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
)
from src.bot.embeds import (
    trades_list_embed,
    top_tickers_embed,
    portfolio_embed,
    settings_embed,
)

if TYPE_CHECKING:
    from src.trading.base import Broker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slash command definitions
# ---------------------------------------------------------------------------


async def setup_commands(
    bot: discord.Client,
    tree: app_commands.CommandTree,
    broker: Broker | None = None,
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
    """

    # -- /trades [politician] [ticker] ------------------------------------

    @tree.command(name="trades", description="Show recent politician stock trades")
    @app_commands.describe(
        politician="Filter by politician name",
        ticker="Filter by stock ticker",
    )
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
