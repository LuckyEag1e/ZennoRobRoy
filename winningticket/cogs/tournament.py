from __future__ import annotations

import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

import db
from common import (
    MemberCooldown,
    load_bracket_embed,
    requires_manage_server,
    resolve_emoji,
    resolve_tournament,
    tournament_name_autocomplete,
)
from csv_import import CsvValidationError, import_tournament, parse_csv
from embeds import render_final_results_embed, render_leaderboard_embed


# How often one member can put a bracket or leaderboard in the channel.
PUBLIC_LOOKUP_COOLDOWN_SECONDS = 60


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.public_lookups = MemberCooldown(PUBLIC_LOOKUP_COOLDOWN_SECONDS)

    group = app_commands.Group(
        name="tournament", description="Manage bracket prediction tournaments", guild_only=True
    )

    async def _post_lookup(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        """Post a bracket or leaderboard in the channel, where everyone can see it, unless
        this member posted one in the last minute; then it's shown just to them, so no one
        can fill the channel with them."""
        if self.public_lookups.seconds_left(interaction) > 0:
            await interaction.response.send_message(
                "Shown just to you, since you posted one in the channel a moment ago.", embed=embed, ephemeral=True
            )
            return
        self.public_lookups.record(interaction)
        await interaction.response.send_message(embed=embed)

    @staticmethod
    async def _resolve_for_next_status(interaction: discord.Interaction, conn: sqlite3.Connection,
                                       name: str, new_status: str) -> sqlite3.Row | None:
        """Fetch a tournament that's at the step just before `new_status`, or reply with an
        error and return None."""
        tournament = await resolve_tournament(interaction, conn, name)
        if tournament is None:
            return None
        if not db.is_next_status(tournament["status"], new_status):
            await interaction.response.send_message(
                f"**{name}** is `{tournament['status']}`, so it can't move to `{new_status}` from here. "
                f"Use `/admin setstatus` to force a status.",
                ephemeral=True,
            )
            return None
        return tournament

    async def _advance_status(self, interaction: discord.Interaction, name: str, new_status: str) -> bool:
        """Move a tournament one step along its lifecycle. Replies with an error and
        returns False if it isn't at the step just before `new_status`."""
        conn = db.get_connection()
        try:
            tournament = await self._resolve_for_next_status(interaction, conn, name, new_status)
            if tournament is None:
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

            # Announcements are public, so the organizer's next step goes to them privately.
            await interaction.response.send_message(f"Created tournament **{name}** with {len(rows)} round-1 matchups.")
            await interaction.followup.send(
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin.", ephemeral=True
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
        await interaction.response.send_message(f"🔒 Predictions are locked for **{name}**.")
        await interaction.followup.send("Use `/match result` to record real outcomes.", ephemeral=True)

    @group.command(name="conclude", description="Archive a finished tournament, keeping its results")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def conclude(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await self._resolve_for_next_status(interaction, conn, name, "concluded")
            if tournament is None:
                return
            result, teams_by_id, scores = db.load_final_results(conn, tournament["id"])
            if result.champion is None:
                await interaction.response.send_message(
                    f"**{name}**'s final has no result yet, so it can't be concluded. "
                    f"Record it with `/match result` first.",
                    ephemeral=True,
                )
                return
            # Post before archiving: once concluded, nothing can find the tournament to post again.
            await interaction.response.send_message(embed=render_final_results_embed(name, result, teams_by_id, scores))
            db.set_tournament_status(conn, tournament["id"], "concluded")
        finally:
            conn.close()
        await interaction.followup.send(
            f"**{name}** is concluded. Its results are saved, and the name is free for a new tournament.",
            ephemeral=True,
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
            await self._post_lookup(interaction, load_bracket_embed(conn, tournament))
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
            await self._post_lookup(interaction, render_leaderboard_embed(name, db.load_scores(conn, tournament["id"])))
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
