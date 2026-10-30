from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, matches_with_both_teams, pick_distribution, resolvable_matches, resolve_all_slots
from bracket_changes import WITHDRAWABLE_STATUSES, withdraw_and_describe
from common import (
    MatchPicker,
    TournamentSession,
    WinnerPicker,
    announce,
    outdated_pick_message,
    requires_manage_server,
    resolve_tournament,
    tournament_name_autocomplete,
)
from embeds import render_match_stats_embed
from labels import advance_text, team_label


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
        if refusal is None and now.skipped:
            refusal = "This match was called off. Use `/admin setresult` to record a winner anyway."
        if refusal is None:
            db.record_result(conn, session.tournament_id, match.id, winner_id, interaction.user.id)
            outcomes = db.get_outcomes(conn, session.tournament_id)
    finally:
        conn.close()
    if refusal is not None:
        await interaction.response.edit_message(content=refusal, view=None)
        return
    advance = advance_text(match, winner_id, session.matches_by_id, session.feeders_by_target, outcomes,
                           session.teams_by_id, session.tournament_name)

    # The announcement tells the organizer everything, so their menu just goes away,
    # unless the announcement can't be posted.
    await interaction.response.defer()
    announced = False
    if interaction.channel is not None:
        try:
            await interaction.channel.send(
                f"📣 Round {match.played_round}: **{winner_label}** defeats **{loser_label}**. {advance}"
            )
            announced = True
        except discord.HTTPException:
            pass
    if announced:
        await interaction.delete_original_response()
    else:
        await interaction.edit_original_response(
            content=f"✅ Result recorded, but it couldn't be announced here. {advance}", view=None
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
            outcomes = db.get_outcomes(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, outcomes.winners.get, session.feeders_by_target)
        pending = resolvable_matches(
            session.matches_by_id, session.feeders_by_target, outcomes.winners.get, outcomes.decided
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

    @match_group.command(name="dropout", description="Drop out of a tournament, giving your remaining matches to your opponents")
    @app_commands.describe(tournament="Tournament name", confirm_name="Retype the tournament name to confirm")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def dropout(self, interaction: discord.Interaction, tournament: str, confirm_name: str) -> None:
        if confirm_name != tournament:
            await interaction.response.send_message("Confirmation name didn't match. You're still in.", ephemeral=True)
            return
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            team_id = db.get_entrant_team_id(conn, tournament_row["id"], interaction.user.id)
            team = db.get_teams(conn, tournament_row["id"]).get(team_id)
            if team is None:
                refusal = f"You aren't an entrant in **{tournament}**."
            elif team["withdrawn"]:
                refusal = f"You've already left **{tournament}**."
            elif tournament_row["status"] not in WITHDRAWABLE_STATUSES:
                refusal = f"**{tournament}** has finished, so there's nothing left to drop out of."
            else:
                refusal = None
                lines, _ = withdraw_and_describe(conn, tournament_row, team, "dropped", interaction.user.id)
        finally:
            conn.close()
        if refusal is not None:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await announce(interaction, "\n".join(lines), f"You've dropped out of **{tournament}**.")

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
