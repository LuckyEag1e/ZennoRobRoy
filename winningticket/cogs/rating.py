from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from labels import rating_lines
from ratings import compute_ratings


class RatingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="rating", description="See your Elo rating, which decides your seed")
    @app_commands.describe(user="Organizers only: a member to look up (leave blank for yourself)")
    @app_commands.guild_only()
    async def rating(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        member = user or interaction.user
        # Ratings are private: members see their own, and only organizers can look anyone up.
        if member.id != interaction.user.id and not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "Ratings are private, so you can only see your own. Leave `user` blank.", ephemeral=True
            )
            return
        conn = db.get_connection()
        try:
            ratings = compute_ratings(db.load_match_history(conn, interaction.guild_id))
        finally:
            conn.close()
        embed = discord.Embed(title=f"Elo rating: {member.display_name}", description="\n".join(rating_lines(member.id, ratings)))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RatingCog(bot))
