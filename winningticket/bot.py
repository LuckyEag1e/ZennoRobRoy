from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import db

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("winningticket")

INITIAL_COGS = (
    "cogs.tournament",
    "cogs.predictions",
    "cogs.results",
    "cogs.admin",
    "cogs.help",
)


class WinningTicketBot(commands.Bot):
    def __init__(self, *, connector: aiohttp.BaseConnector) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            connector=connector,
        )

    async def setup_hook(self) -> None:
        conn = db.get_connection()
        db.init_db(conn)
        conn.close()

        for cog in INITIAL_COGS:
            await self.load_extension(cog)

        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

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
