#!/usr/bin/env python3
"""One-off utility: wipes any globally-registered slash commands.

Run this if commands show up duplicated in Discord. That happens when the
bot has been synced both globally (no DEV_GUILD_ID set) and to a specific
guild (DEV_GUILD_ID set) at different points -- leaving two registrations of
every command. This clears only the global set; guild-scoped commands are
left alone and will still work normally.
"""

from __future__ import annotations

import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_BOT_TOKEN"]


def main() -> None:
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())

    @bot.event
    async def on_ready() -> None:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared global commands. Restart bot.py normally -- the duplicates should be gone "
              "(you may need to fully restart your Discord client to clear its local cache).")
        await bot.close()

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
