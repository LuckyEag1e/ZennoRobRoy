from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from labels import OPT_OUT_CONFIRMATION, opted_out_message


class ConsentCog(commands.Cog):
    """Members agree to link their data when they first sign up; this is how they take it back."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="optout", description="Unlink your tournament entries, results and rating from your account")
    @app_commands.describe(confirm=f"Type {OPT_OUT_CONFIRMATION} to confirm. Unlinked entries can't be relinked by you")
    @app_commands.guild_only()
    async def optout(self, interaction: discord.Interaction, confirm: str) -> None:
        if confirm.strip().lower() != OPT_OUT_CONFIRMATION:
            await interaction.response.send_message(
                f"Type `{OPT_OUT_CONFIRMATION}` in `confirm` to unlink your data. Nothing changed.", ephemeral=True
            )
            return
        conn = db.get_connection()
        try:
            unlinked = db.opt_out(conn, interaction.guild_id, interaction.user.id)
        finally:
            conn.close()
        await interaction.response.send_message(opted_out_message(unlinked), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConsentCog(bot))
