from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, matches_with_both_teams, next_opponent, pick_distribution, resolvable_matches, resolve_all_slots
from common import (
    MatchPicker,
    TournamentSession,
    WinnerPicker,
    outdated_pick_message,
    requires_manage_server,
    resolve_tournament,
    tournament_name_autocomplete,
)
from embeds import render_match_stats_embed
from labels import team_label


async def _record_and_announce(interaction: discord.Interaction, session: TournamentSession,
                               match: Match, winner_id: int, loser_id: int) -> None:
    winner_label = team_label(session.teams_by_id, winner_id)
    loser_label = team_label(session.teams_by_id, loser_id)
    conn = db.get_connection()
    try:
        # Checked and saved with no await in between, so nothing can change the match in the gap.
        now = db.match_now(conn, session.tournament_id, match.id)
        refusal = outdated_pick_message(now, (winner_id, loser_id))
        if refusal is None and now.status != "in_progress":
            refusal = f"**{session.tournament_name}** isn't recording results right now (status: {now.status})."
        if refusal is None and now.winner is not None:
            refusal = (f"Someone already recorded this match: **{team_label(session.teams_by_id, now.winner)}** won. "
                       f"Use `/admin setresult` to change it.")
        if refusal is None:
            db.record_result(conn, session.tournament_id, match.id, winner_id, interaction.user.id)
    finally:
        conn.close()
    if refusal is not None:
        await interaction.response.edit_message(content=refusal, view=None)
        return

    conn = db.get_connection()
    try:
        if match.feeds_into_match_id is None:
            advance_text = f"🏆 **{winner_label}** is the champion of {session.tournament_name}!"
        else:
            results_by_match = db.get_results(conn, session.tournament_id)
            opponent = next_opponent(match, session.feeders_by_target, results_by_match.get)
            advance_text = (
                f"**{winner_label}** advances to Round {session.matches_by_id[match.feeds_into_match_id].played_round} "
                f"to face {team_label(session.teams_by_id, opponent)}."
            )
    finally:
        conn.close()

    await interaction.response.edit_message(content=f"✅ Result recorded. {advance_text}", view=None)
    if interaction.channel is not None:
        await interaction.channel.send(
            f"📣 Round {match.played_round}: **{winner_label}** defeats **{loser_label}**. {advance_text}"
        )


class ResultsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    match_group = app_commands.Group(
        name="match", description="Record and view match results/stats", guild_only=True
    )

    @match_group.command(name="result", description="Record the real outcome of a ready match")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @requires_manage_server
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

        async def ask_for_winner(pick_interaction: discord.Interaction, match: Match) -> None:
            team_a, team_b = slots[match.id]

            async def record(winner_interaction: discord.Interaction, winner_id: int, loser_id: int) -> None:
                await _record_and_announce(winner_interaction, session, match, winner_id, loser_id)

            await pick_interaction.response.edit_message(
                content=f"Round {match.played_round}: **{team_label(session.teams_by_id, team_a)}** vs "
                        f"**{team_label(session.teams_by_id, team_b)}** — who won?",
                view=WinnerPicker(team_a, team_b, session.teams_by_id, record),
            )

        await interaction.response.send_message(
            "Select a match to record a result for:",
            view=MatchPicker(pending, slots, session.teams_by_id, ask_for_winner),
            ephemeral=True,
        )

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

        async def show_pick_breakdown(pick_interaction: discord.Interaction, match: Match) -> None:
            team_a, team_b = slots[match.id]
            counts = pick_distribution(matches_by_id, predictions_by_user, match.played_round, team_a, team_b)
            embed = render_match_stats_embed(tournament, match, team_a, team_b, counts, teams_by_id)
            await pick_interaction.response.edit_message(content=None, embed=embed, view=None)

        await interaction.response.send_message(
            "Select a matchup to see the pool's picks:",
            view=MatchPicker(known, slots, teams_by_id, show_pick_breakdown),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ResultsCog(bot))
