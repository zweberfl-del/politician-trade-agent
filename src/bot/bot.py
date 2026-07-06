from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.config import settings
from src.bot.embeds import trade_alert_embed, weekly_digest_embed
from src.storage.database import get_followers

if TYPE_CHECKING:
    from src.data.enrichment import EnrichmentService
    from src.data.models import PoliticianTrade
    from src.trading.base import Broker

log = logging.getLogger(__name__)


class TradeBot(discord.Client):
    """Discord bot that sends politician-trade alerts and exposes slash commands."""

    def __init__(
        self,
        broker: Broker | None = None,
        enrichment: EnrichmentService | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = False  # we do not need message content
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._broker = broker
        self._enrichment = enrichment

    # -- lifecycle hooks --------------------------------------------------

    async def setup_hook(self) -> None:
        """Called once the bot has connected but before it starts receiving events."""
        from src.bot.commands import setup_commands

        await setup_commands(
            self, self.tree, broker=self._broker, enrichment=self._enrichment
        )
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self) -> None:
        if self.user is not None:
            log.info("Bot ready as %s (ID: %s)", self.user, self.user.id)
        else:
            log.info("Bot ready (user identity not yet available)")

    # -- trade alerts -----------------------------------------------------

    async def send_trade_alert(self, trade: PoliticianTrade) -> None:
        """Send a trade-alert embed to the configured channel.

        Also DMs every Discord user who follows the politician.
        """
        if not settings.enable_alerts or not settings.alert_channel_id:
            return

        channel = self.get_channel(settings.alert_channel_id)
        if channel is None:
            log.warning(
                "Alert channel %s not found; skipping alert", settings.alert_channel_id
            )
            return

        embed = trade_alert_embed(trade)

        # Type-narrow: we expect a messageable text channel.
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(embed=embed)
        else:
            log.warning(
                "Alert channel %s is not a text channel (type=%s); skipping",
                settings.alert_channel_id,
                type(channel).__name__,
            )
            return

        # Notify followers via DM
        follower_ids = await get_followers(
            settings.database_path, trade.politician_name
        )
        for user_id in follower_ids:
            try:
                user = await self.fetch_user(int(user_id))
                await user.send(
                    f"**{trade.politician_name}** just made a trade!", embed=embed
                )
            except Exception:
                log.debug("Could not DM user %s", user_id)

    async def send_weekly_digest(
        self, top_tickers: list[dict], most_active: list[dict]
    ) -> None:
        """Post the weekly digest embed to the alert channel."""
        if not settings.alert_channel_id:
            return
        channel = self.get_channel(settings.alert_channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(embed=weekly_digest_embed(top_tickers, most_active))
        else:
            log.warning("Digest channel %s unavailable; skipping", settings.alert_channel_id)

    # -- convenience runner -----------------------------------------------

    def run_bot(self) -> None:
        """Start the bot using the token from settings.

        This is a blocking call and will not return until the bot shuts down.
        """
        token = settings.discord_bot_token
        if not token:
            log.error("DISCORD_BOT_TOKEN is not set; cannot start bot")
            return
        self.run(token, log_handler=None)  # let the app-level handler manage logs
