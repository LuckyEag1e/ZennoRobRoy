from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, matches_with_both_teams, next_opponent, pick_distribution, resolvable_matches, resolve_all_slots
from common import TournamentSession, match_select_options, resolve_tournament, tournament_name_autocomplete
from embeds import render_match_stats_embed
from labels import team_label


class MatchSelect(discord.ui.Select):
    def __init__(self, session: TournamentSession, pending: list[Match], slots: dict) -> None:
        super().__init__(
            placeholder="Select a match...",
            options=match_select_options(pending, slots, session.teams_by_id),
        )
        self.session = session
        self.slots = slots

    async def callback(self, interaction: discord.Interaction) -> None:
        match_id = int(self.values[0])
        match = self.session.matches_by_id[match_id]
        team_a, team_b = self.slots[match_id]
        a_label = team_label(self.session.teams_by_id, team_a)
        b_label = team_label(self.session.teams_by_id, team_b)

        view = discord.ui.View(timeout=300)
        view.add_item(WinnerButton(self.session, match, team_a, team_b, a_label))
        view.add_item(WinnerButton(self.session, match, team_b, team_a, b_label))
        await interaction.response.edit_message(
            content=f"Round {match.round}: **{a_label}** vs **{b_label}** — who won?", view=view
        )


class WinnerButton(discord.ui.Button):
    def __init__(self, session: TournamentSession, match: Match, winner_id: int, loser_id: int, label: str) -> None:
        super().__init__(label=label[:80], style=discord.ButtonStyle.success)
        self.session = session
        self.match = match
        self.winner_id = winner_id
        self.loser_id = loser_id

    async def callback(self, interaction: discord.Interaction) -> None:
        winner_label = team_label(self.session.teams_by_id, self.winner_id)
        loser_label = team_label(self.session.teams_by_id, self.loser_id)
        conn = db.get_connection()
        try:
            db.upsert_result(conn, self.match.id, self.winner_id, interaction.user.id)
            if self.match.feeds_into_match_id is None:
                db.set_tournament_status(conn, self.session.tournament_id, "completed")
                advance_text = f"🏆 **{winner_label}** is the champion of {self.session.tournament_name}!"
            else:
                results_by_match = db.get_results(conn, self.session.tournament_id)
                opponent = next_opponent(self.match, self.session.feeders_by_target, results_by_match.get)
                advance_text = (
                    f"**{winner_label}** advances to Round {self.match.round + 1} "
                    f"to face {team_label(self.session.teams_by_id, opponent)}."
                )
        finally:
            conn.close()

        await interaction.response.edit_message(content=f"✅ Result recorded. {advance_text}", view=None)
        if interaction.channel is not None:
            await interaction.channel.send(
                f"📣 Round {self.match.round}: **{winner_label}** defeats **{loser_label}**. {advance_text}"
            )


class ResultsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    match_group = app_commands.Group(name="match", description="Record and view match results/stats")

    @match_group.command(name="result", description="Record the real outcome of a ready match")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def result(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            if tournament_row["status"] != "in_progress":
                await interaction.response.send_message(
                    f"**{tournament}** must be locked (`/tournament lock`) before recording results "
                    f"(current status: {tournament_row['status']}).",
                    ephemeral=True,
                )
                return
            session = TournamentSession.load(conn, tournament_row)
            results_by_match = db.get_results(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, results_by_match.get, session.feeders_by_target)
        pending = resolvable_matches(
            session.matches_by_id, session.feeders_by_target, results_by_match.get, set(results_by_match)
        )
        if not pending:
            await interaction.response.send_message(
                "No matches are currently ready to record (either the bracket is fully decided, "
                "or earlier rounds aren't finished yet).",
                ephemeral=True,
            )
            return

        view = discord.ui.View(timeout=300)
        view.add_item(MatchSelect(session, pending, slots))
        await interaction.response.send_message("Select a match to record a result for:", view=view, ephemeral=True)

    @match_group.command(name="stats", description="See the pool's pick breakdown for a matchup")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def stats(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament_row["id"])
            results_by_match = db.get_results(conn, tournament_row["id"])
            predictions_by_user = db.get_all_predictions(conn, tournament_row["id"])
        finally:
            conn.close()

        slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
        known = matches_with_both_teams(matches_by_id, slots)
        if not known:
            await interaction.response.send_message(
                "No matchups are known yet for this tournament (round 1 should always be known right after import).",
                ephemeral=True,
            )
            return

        select = discord.ui.Select(
            placeholder="Select a match...", options=match_select_options(known, slots, teams_by_id)
        )

        async def show_pick_breakdown(select_interaction: discord.Interaction) -> None:
            match = matches_by_id[int(select.values[0])]
            team_a, team_b = slots[match.id]
            counts = pick_distribution(matches_by_id, predictions_by_user, match.round, team_a, team_b)
            embed = render_match_stats_embed(tournament, match, team_a, team_b, counts, teams_by_id)
            await select_interaction.response.edit_message(content=None, embed=embed, view=None)

        select.callback = show_pick_breakdown
        view = discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.response.send_message("Select a matchup to see the pool's picks:", view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ResultsCog(bot))
