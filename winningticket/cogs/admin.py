from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, bracket_order, matches_with_both_teams, resolve_all_slots
from common import (
    MAX_SELECT_OPTIONS,
    MatchPicker,
    TournamentSession,
    WinnerPicker,
    resolve_tournament,
    tournament_name_autocomplete,
)
from labels import dropped_results_note, team_label

STATUS_CHOICES = [app_commands.Choice(name=status, value=status) for status in db.STATUSES]


async def team_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    tournament_name = getattr(interaction.namespace, "tournament", None)
    if not tournament_name:
        return []
    conn = db.get_connection()
    try:
        tournament_row = db.get_tournament_by_name(conn, interaction.guild_id, tournament_name)
        if tournament_row is None:
            return []
        teams_by_id = db.get_teams(conn, tournament_row["id"])
    finally:
        conn.close()
    return [
        app_commands.Choice(name=team["name"], value=team["name"])
        for team in teams_by_id.values()
        if current.lower() in team["name"].lower()
    ][:MAX_SELECT_OPTIONS]


class ConfirmView(discord.ui.View):
    def __init__(self, on_confirm, timeout: int = 120) -> None:
        super().__init__(timeout=timeout)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="admin", description="Admin troubleshooting/editing commands",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    async def _load_session(self, interaction: discord.Interaction, conn, tournament: str) -> TournamentSession | None:
        tournament_row = await resolve_tournament(interaction, conn, tournament)
        if tournament_row is None:
            return None
        return TournamentSession.load(conn, tournament_row)

    @group.command(name="setstatus", description="Force a tournament's status (bypasses normal open/lock flow)")
    @app_commands.describe(tournament="Tournament name", status="New status")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @app_commands.choices(status=STATUS_CHOICES)
    async def setstatus(self, interaction: discord.Interaction, tournament: str, status: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            db.set_tournament_status(conn, tournament_row["id"], status)
        finally:
            conn.close()
        await interaction.response.send_message(f"Status for **{tournament}** set to `{status}`.", ephemeral=True)

    @group.command(name="deletetournament", description="Permanently delete a tournament and all its data")
    @app_commands.describe(tournament="Tournament name", confirm_name="Retype the tournament name to confirm deletion")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def delete_tournament(self, interaction: discord.Interaction, tournament: str, confirm_name: str) -> None:
        if confirm_name != tournament:
            await interaction.response.send_message(
                "Confirmation name didn't match. No changes made.", ephemeral=True
            )
            return
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            db.delete_tournament(conn, tournament_row["id"])
        finally:
            conn.close()
        await interaction.response.send_message(f"🗑️ Deleted tournament **{tournament}** and all its data.", ephemeral=True)

    @group.command(name="editteam", description="Fix a team's name or seed")
    @app_commands.describe(tournament="Tournament name", team="Team to edit", new_name="New name (optional)", new_seed="New seed (optional)")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete, team=team_name_autocomplete)
    async def edit_team(self, interaction: discord.Interaction, tournament: str, team: str,
                         new_name: str | None = None, new_seed: int | None = None) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            team_row = db.get_team_by_name(conn, tournament_row["id"], team)
            if team_row is None:
                await interaction.response.send_message(f"No team named '{team}' in **{tournament}**.", ephemeral=True)
                return
            if new_name is None and new_seed is None:
                await interaction.response.send_message("Provide new_name and/or new_seed.", ephemeral=True)
                return
            db.update_team(conn, team_row["id"], new_name, new_seed, seed_given=new_seed is not None)
        finally:
            conn.close()
        await interaction.response.send_message(f"Updated team '{team}'.", ephemeral=True)

    @group.command(name="setresult", description="Force-set (or overwrite) a match's real result")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def setresult(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            session = await self._load_session(interaction, conn, tournament)
            if session is None:
                return
            results_by_match = db.get_results(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, results_by_match.get, session.feeders_by_target)
        known = matches_with_both_teams(session.matches_by_id, slots)
        if not known:
            await interaction.response.send_message("No matches have both teams known yet.", ephemeral=True)
            return

        async def ask_for_winner(pick_interaction: discord.Interaction, match: Match) -> None:
            team_a, team_b = slots[match.id]

            async def choose(winner_interaction: discord.Interaction, winner_id: int, loser_id: int) -> None:
                winner_label = team_label(session.teams_by_id, winner_id)

                async def record(confirm_interaction: discord.Interaction) -> None:
                    conn = db.get_connection()
                    try:
                        dropped = db.record_result(conn, session.tournament_id, match.id, winner_id,
                                                   confirm_interaction.user.id)
                    finally:
                        conn.close()
                    await confirm_interaction.response.edit_message(
                        content=f"✅ Set Round {match.round} winner to **{winner_label}**.{dropped_results_note(dropped)}",
                        view=None,
                    )

                if match.id in results_by_match:
                    await winner_interaction.response.edit_message(
                        content=f"This match already has a recorded result. Overwrite with **{winner_label}**? "
                                f"Later results that depended on the old winner will be cleared.",
                        view=ConfirmView(on_confirm=record),
                    )
                else:
                    await record(winner_interaction)

            await pick_interaction.response.edit_message(
                content=f"Round {match.round}: **{team_label(session.teams_by_id, team_a)}** vs "
                        f"**{team_label(session.teams_by_id, team_b)}** — force winner to:",
                view=WinnerPicker(team_a, team_b, session.teams_by_id, choose),
            )

        await interaction.response.send_message(
            "Select a match to force a result for:",
            view=MatchPicker(known, slots, session.teams_by_id, ask_for_winner, results_by_match),
            ephemeral=True,
        )

    @group.command(name="clearresult", description="Delete a recorded match result")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def clearresult(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            session = await self._load_session(interaction, conn, tournament)
            if session is None:
                return
            results_by_match = db.get_results(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, results_by_match.get, session.feeders_by_target)
        decided = sorted((m for m in session.matches_by_id.values() if m.id in results_by_match), key=bracket_order)
        if not decided:
            await interaction.response.send_message("No recorded results to clear.", ephemeral=True)
            return

        async def confirm_clear(pick_interaction: discord.Interaction, match: Match) -> None:
            async def clear(confirm_interaction: discord.Interaction) -> None:
                conn = db.get_connection()
                try:
                    dropped = db.clear_result(conn, session.tournament_id, match.id)
                finally:
                    conn.close()
                await confirm_interaction.response.edit_message(
                    content=f"✅ Result cleared.{dropped_results_note(dropped)}", view=None
                )

            await pick_interaction.response.edit_message(
                content="Clear this match's recorded result? Later results that depended on it will be cleared too.",
                view=ConfirmView(on_confirm=clear),
            )

        await interaction.response.send_message(
            "Select a result to clear:",
            view=MatchPicker(decided, slots, session.teams_by_id, confirm_clear),
            ephemeral=True,
        )

    @group.command(name="clearpredictions", description="Wipe one user's saved predictions for a tournament")
    @app_commands.describe(tournament="Tournament name", user="The user whose predictions should be cleared")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def clearpredictions(self, interaction: discord.Interaction, tournament: str, user: discord.Member) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            existing = db.get_user_predictions(conn, tournament_row["id"], user.id)
        finally:
            conn.close()

        if not existing:
            await interaction.response.send_message(f"{user.display_name} has no saved predictions for **{tournament}**.", ephemeral=True)
            return

        async def clear_predictions(confirm_interaction: discord.Interaction) -> None:
            conn = db.get_connection()
            try:
                db.clear_user_predictions(conn, tournament_row["id"], user.id)
            finally:
                conn.close()
            await confirm_interaction.response.edit_message(
                content=f"✅ Cleared {user.display_name}'s predictions for **{tournament}**.", view=None
            )

        view = ConfirmView(on_confirm=clear_predictions)
        await interaction.response.send_message(
            f"Clear {user.display_name}'s {len(existing)} saved picks for **{tournament}**?",
            view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
