import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from store import Store

INITIAL_COGS = [
    "cogs.ping",
    "cogs.banners",
]

log = logging.getLogger("fuku")


class Fuku(commands.Bot):
    def __init__(self, dev_guild_id: int | None, store: Store, approver_role_id: int | None) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.dev_guild_id = dev_guild_id
        self.store = store
        # Besides admins and the server owner, members with this role can create banners
        # and approve pulls.
        self.approver_role_id = approver_role_id

    async def setup_hook(self) -> None:
        for cog in INITIAL_COGS:
            await self.load_extension(cog)
        if self.dev_guild_id:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %s", len(synced), self.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d commands globally", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")


def main() -> None:
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    dev_guild = os.environ.get("DEV_GUILD_ID")
    approver_role = os.environ.get("FUKU_APPROVER_ROLE_ID")
    Fuku(int(dev_guild) if dev_guild else None, Store(),
         int(approver_role) if approver_role else None).run(token, root_logger=True)


if __name__ == "__main__":
    main()
