from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
import lobby_flow
from common import concluded_tournament_autocomplete, load_bracket_embed, resolve_concluded_tournament
from embeds import EMBED_DESCRIPTION_LIMIT, mention, render_leaderboard_embed
from labels import history_line, join_within, seeding_lines


class HistoryCog(commands.Cog):
    """Concluded tournaments, which the other commands leave out. Replies are private, so
    looking back doesn't add posts to the channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="history", description="Look back at concluded tournaments", guild_only=True)

    @group.command(name="list", description="See every concluded tournament, newest first")
    async def list_tournaments(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            summaries = db.get_concluded_summaries(conn, interaction.guild_id)
        finally:
            conn.close()
        lines = [history_line(summary) for summary in summaries] or ["No tournaments have concluded yet."]
        embed = discord.Embed(title="Concluded tournaments", description=join_within(lines, EMBED_DESCRIPTION_LIMIT))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="bracket", description="See a concluded tournament's final bracket, or a 5v5 event's lobbies")
    @app_commands.describe(tournament="Concluded tournament")
    @app_commands.autocomplete(tournament=concluded_tournament_autocomplete)
    async def bracket(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_concluded_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            if tournament_row["format"] == "lobbies":
                embed = discord.Embed(title=f"Lobbies: {tournament_row['name']}", description=join_within(
                    lobby_flow.history_lines(conn, tournament_row, mention), EMBED_DESCRIPTION_LIMIT))
            else:
                embed = load_bracket_embed(conn, tournament_row)
        finally:
            conn.close()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="seeding", description="See how a concluded tournament's bracket was seeded")
    @app_commands.describe(tournament="Concluded tournament")
    @app_commands.autocomplete(tournament=concluded_tournament_autocomplete)
    async def seeding(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_concluded_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            if tournament_row["format"] == "lobbies":
                await interaction.response.send_message(
                    f"**{tournament_row['name']}** was a 5v5 event, so it had no seeding or predictions. "
                    f"`/history bracket` shows its lobbies.", ephemeral=True)
                return
            lines = seeding_lines(tournament_row, db.get_teams(conn, tournament_row["id"]))
        finally:
            conn.close()
        embed = discord.Embed(title=f"Seeding: {tournament_row['name']}",
                              description=join_within(lines, EMBED_DESCRIPTION_LIMIT))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="leaderboard", description="See a concluded tournament's final prediction leaderboard")
    @app_commands.describe(tournament="Concluded tournament")
    @app_commands.autocomplete(tournament=concluded_tournament_autocomplete)
    async def leaderboard(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_concluded_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            if tournament_row["format"] == "lobbies":
                await interaction.response.send_message(
                    f"**{tournament_row['name']}** was a 5v5 event, so it had no seeding or predictions. "
                    f"`/history bracket` shows its lobbies.", ephemeral=True)
                return
            embed = render_leaderboard_embed(tournament_row["name"], db.load_scores(conn, tournament_row["id"]))
        finally:
            conn.close()
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HistoryCog(bot))
