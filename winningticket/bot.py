from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db
from common import turn_away_if_banned

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("winningticket")

INITIAL_COGS = (
    "cogs.tournament",
    "cogs.registration",
    "cogs.rating",
    "cogs.predictions",
    "cogs.results",
    "cogs.admin",
    "cogs.feedback",
    "cogs.help",
)


class WinningTicketTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Turn away members an admin has banned, before any command or autocomplete runs.
        Buttons, menus and forms check through common.BotView and BotModal instead."""
        return not await turn_away_if_banned(interaction)


class WinningTicketBot(commands.Bot):
    def __init__(self, *, connector: aiohttp.BaseConnector) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            connector=connector,
            tree_cls=WinningTicketTree,
        )

    async def setup_hook(self) -> None:
        conn = db.get_connection()
        db.init_db(conn)
        conn.close()

        for cog in INITIAL_COGS:
            await self.load_extension(cog)
        self.tree.on_error = self.on_app_command_error

        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the Manage Server permission to use this command.", ephemeral=True
            )
            return
        command = interaction.command.qualified_name if interaction.command else "?"
        log.error("Error in /%s", command, exc_info=error)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        for guild in self.guilds:
            log.info("Connected to server: %s (id=%s)", guild.name, guild.id)


async def main() -> None:
    # Recycle connections idle for over 10s, so a pause between /predict clicks
    # never reuses one a router has silently dropped. Rapid clicks still share
    # a connection; opening a fresh one per request can get throttled.
    # TCPConnector needs a running event loop, so it's built here.
    connector = aiohttp.TCPConnector(keepalive_timeout=10)
    bot = WinningTicketBot(connector=connector)
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
