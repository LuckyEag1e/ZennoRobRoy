from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import compute_leaderboard, resolve_all_slots
from common import requires_manage_server, resolve_emoji, resolve_tournament, tournament_name_autocomplete
from csv_import import CsvValidationError, import_tournament, parse_csv
from embeds import render_bracket_embed, render_leaderboard_embed


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="tournament", description="Manage bracket prediction tournaments", guild_only=True
    )

    @staticmethod
    async def _advance_status(interaction: discord.Interaction, name: str, new_status: str) -> bool:
        """Move a tournament one step along its lifecycle. Replies with an error and
        returns False if it isn't at the step just before `new_status`."""
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return False
            if not db.is_next_status(tournament["status"], new_status):
                await interaction.response.send_message(
                    f"**{name}** is `{tournament['status']}`, so it can't move to `{new_status}` from here. "
                    f"Use `/admin setstatus` to force a status.",
                    ephemeral=True,
                )
                return False
            db.set_tournament_status(conn, tournament["id"], new_status)
            return True
        finally:
            conn.close()

    @group.command(name="create", description="Create a tournament from a bracket CSV")
    @app_commands.describe(name="Tournament name", csv_file="CSV with team_a,team_b[,seed_a,seed_b] columns")
    @requires_manage_server
    async def create(self, interaction: discord.Interaction, name: str, csv_file: discord.Attachment) -> None:
        conn = db.get_connection()
        try:
            if db.get_tournament_by_name(conn, interaction.guild_id, name) is not None:
                await interaction.response.send_message(
                    f"A tournament named '{name}' already exists in this server.", ephemeral=True
                )
                return

            raw = await csv_file.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                await interaction.response.send_message("CSV file must be UTF-8 encoded text.", ephemeral=True)
                return

            try:
                rows = parse_csv(content)
                import_tournament(conn, interaction.guild_id, name, interaction.user.id, rows)
            except CsvValidationError as e:
                await interaction.response.send_message(f"CSV error: {e}", ephemeral=True)
                return

            await interaction.response.send_message(
                f"Created tournament **{name}** with {len(rows)} round-1 matchups. "
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin."
            )
        finally:
            conn.close()

    @group.command(name="open", description="Open a tournament for predictions")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def open_predictions(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._advance_status(interaction, name, "predictions_open"):
            return
        emoji = resolve_emoji(self.bot, "winningticket")
        await interaction.response.send_message(
            f"{emoji} Predictions are now open for **{name}**! Run `/predict tournament:{name}` to fill out your bracket."
        )

    @group.command(name="lock", description="Lock predictions and start recording real results")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def lock(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._advance_status(interaction, name, "in_progress"):
            return
        await interaction.response.send_message(
            f"🔒 Predictions are locked for **{name}**. Use `/match result` to record real outcomes."
        )

    @group.command(name="bracket", description="Show the current bracket")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def bracket(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return
            matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
            results_by_match = db.get_results(conn, tournament["id"])
            slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
            embed = render_bracket_embed(
                name, tournament["status"], matches_by_id, feeders, slots, results_by_match, teams_by_id
            )
            await interaction.response.send_message(embed=embed)
        finally:
            conn.close()

    @group.command(name="leaderboard", description="Show the current prediction leaderboard")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def leaderboard(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return
            matches_by_id = db.get_matches(conn, tournament["id"])
            results_by_match = db.get_results(conn, tournament["id"])
            predictions_by_user = db.get_all_predictions(conn, tournament["id"])
            scores = compute_leaderboard(matches_by_id, results_by_match, predictions_by_user)
            embed = render_leaderboard_embed(name, scores)
            await interaction.response.send_message(embed=embed)
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
