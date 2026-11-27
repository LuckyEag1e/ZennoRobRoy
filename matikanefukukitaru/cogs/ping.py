import math

import discord
from discord import app_commands
from discord.ext import commands


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check that Fuku is awake")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = self.bot.latency
        if math.isfinite(latency):
            message = f"Pong! {round(latency * 1000)} ms"
        else:
            # discord.py reports inf until the first heartbeat is acknowledged.
            message = "Pong! Latency not measured yet."
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PingCog(bot))
