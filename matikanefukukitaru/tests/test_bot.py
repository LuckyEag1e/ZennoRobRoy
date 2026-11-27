import asyncio

import bot
from store import Store


def test_every_cog_in_the_list_loads():
    """A typo in INITIAL_COGS or a broken setup() would otherwise only show up at startup."""

    async def load_all() -> bot.Fuku:
        client = bot.Fuku(dev_guild_id=None, store=Store(":memory:"), approver_role_id=None)
        for cog in bot.INITIAL_COGS:
            await client.load_extension(cog)
        return client

    client = asyncio.run(load_all())
    assert len(client.cogs) == len(bot.INITIAL_COGS)


def test_importing_bot_does_not_need_a_token(monkeypatch):
    # Tests import bot.py, so it must only read DISCORD_BOT_TOKEN when it actually starts.
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    import importlib

    importlib.reload(bot)
