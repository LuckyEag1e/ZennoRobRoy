from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks

import coordination
import db
import reports
from bracket import Match, matches_with_both_teams, pick_distribution, resolvable_matches, resolve_all_slots
from bracket_changes import WITHDRAWABLE_STATUSES, new_walkovers, set_result_and_describe, withdraw_and_describe
from common import (
    MatchPicker,
    TournamentSession,
    WinnerCallback,
    WinnerPicker,
    announce,
    outdated_pick_message,
    requires_manage_server,
    resolve_tournament,
    tournament_name_autocomplete,
)
from embeds import mention, render_match_stats_embed
from labels import team_label
from posting import ReadyUpButton, announce_match_change, delete_replaced_post, match_rooms, sync_match_post


log = logging.getLogger("winningticket")


async def _record_and_announce(interaction: discord.Interaction, session: TournamentSession,
                               match: Match, winner_id: int, loser_id: int) -> None:
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
            text, _ = set_result_and_describe(conn, db.get_tournament(conn, session.tournament_id), match,
                                              winner_id, loser_id, interaction.user.id, mention)
    finally:
        conn.close()
    if refusal is not None:
        await interaction.response.edit_message(content=refusal, view=None)
        return

    # The announcement tells the organizer everything, so their menu just goes away,
    # unless the announcement can't be posted.
    await interaction.response.defer()
    if await announce_match_change(interaction.client, interaction.channel, match.id, text):
        await interaction.delete_original_response()
    else:
        await interaction.edit_original_response(
            content=f"✅ Result recorded, but it couldn't be announced here:\n{text}", view=None
        )


class ReportPicker(WinnerPicker):
    """A player's vote on who won their match, plus Take Back Vote once their side has voted."""

    def __init__(self, team_a: int, team_b: int, teams_by_id: dict, on_pick: WinnerCallback,
                 on_take_back: Callable[[discord.Interaction], Awaitable[None]] | None) -> None:
        super().__init__(team_a, team_b, teams_by_id, on_pick)
        if on_take_back is not None:
            button = discord.ui.Button(label="Take Back Vote", style=discord.ButtonStyle.secondary)
            button.callback = on_take_back
            self.add_item(button)


class ResultsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    match_group = app_commands.Group(
        name="match", description="Record and view match results/stats", guild_only=True
    )

    async def cog_load(self) -> None:
        self.tend_matches.start()

    async def cog_unload(self) -> None:
        self.tend_matches.cancel()

    @tasks.loop(seconds=5)
    async def tend_matches(self) -> None:
        """Move coordinated tournaments' calls along, warn about and then give unanswered
        votes as they fall due, keep every match post up to date, which also catches
        results organizers record, and delete posts new messages replaced. An exception
        escaping a tasks.loop stops it until the bot restarts, so every error is logged,
        one tournament or match at a time."""
        try:
            conn = db.get_connection()
            try:
                now = datetime.now(timezone.utc)
                for tournament in db.get_coordinated_tournaments(conn):
                    try:
                        coordination.tend(conn, tournament, now, match_rooms(self.bot, tournament["guild_id"], conn))
                    except Exception:
                        log.exception("Couldn't call matches for tournament %s", tournament["id"])
                for step in reports.due_steps(conn, now):
                    try:
                        await self._take_step(conn, step)
                    except Exception:
                        log.exception("Couldn't %s the vote on match %s", step.kind, step.vote["match_id"])
                for post in db.get_match_posts(conn):
                    try:
                        await sync_match_post(self.bot, conn, post["match_id"])
                    except Exception:
                        log.exception("Couldn't update the post for match %s", post["match_id"])
                for replaced in db.get_replaced_match_posts(conn):
                    try:
                        await delete_replaced_post(self.bot, conn, replaced)
                    except Exception:
                        log.exception("Couldn't delete the replaced match post %s", replaced["message_id"])
            finally:
                conn.close()
        except Exception:
            log.exception("Couldn't check on matches")

    @tend_matches.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _take_step(self, conn: sqlite3.Connection, step: reports.DueStep) -> None:
        vote = step.vote
        if step.kind == "warn":
            reports.warn(conn, vote)
            await sync_match_post(self.bot, conn, vote["match_id"])
            return
        text = reports.give_win(conn, vote)
        if text is None:
            return  # the votes changed since they were checked
        await announce_match_change(self.bot, self.bot.get_channel(vote["channel_id"]), vote["match_id"], text)

    @match_group.command(name="report", description="Report who won your match")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def report(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            found = reports.find_reportable_match(conn, tournament_row, interaction.user.id)
            teams_by_id = db.get_teams(conn, tournament_row["id"])
        finally:
            conn.close()
        if isinstance(found, str):
            await interaction.response.send_message(found, ephemeral=True)
            return

        team_a, team_b = found.teams
        prompt = (f"Round {found.match.played_round}: **{team_label(teams_by_id, team_a)}** vs "
                  f"**{team_label(teams_by_id, team_b)}**. Who won?")
        if found.vote is not None:
            prompt += (f" Your side reported that **{team_label(teams_by_id, found.vote['winner_team_id'])}** won. "
                       f"Pick again to change it.")

        async def vote(vote_interaction: discord.Interaction, winner_id: int, loser_id: int) -> None:
            await self._vote(vote_interaction, tournament_row["id"], found, winner_id)

        async def take_back(take_back_interaction: discord.Interaction) -> None:
            await self._take_back(take_back_interaction, tournament_row["id"], found)

        await interaction.response.send_message(
            prompt, view=ReportPicker(team_a, team_b, teams_by_id, vote, take_back if found.vote else None),
            ephemeral=True,
        )

    @staticmethod
    def _still_reportable(conn: sqlite3.Connection, tournament_id: int, user_id: int,
                          shown: reports.ReportableMatch) -> reports.ReportableMatch | str:
        """The voter's match as it is now, or why the menu they opened earlier no longer applies."""
        now = reports.find_reportable_match(conn, db.get_tournament(conn, tournament_id), user_id)
        if not isinstance(now, str) and (now.match.id, now.team_id, now.teams) != (shown.match.id, shown.team_id, shown.teams):
            return "This match has changed since the menu was opened. Run `/match report` again."
        return now

    async def _vote(self, interaction: discord.Interaction, tournament_id: int, shown: reports.ReportableMatch,
                    winner_id: int) -> None:
        conn = db.get_connection()
        try:
            # Checked and saved with no await in between, so nothing can change the match in the gap.
            found = self._still_reportable(conn, tournament_id, interaction.user.id, shown)
            if isinstance(found, str):
                await interaction.response.edit_message(content=found, view=None)
                return
            tournament = db.get_tournament(conn, tournament_id)
            before = db.get_outcomes(conn, tournament_id)
            outcome = db.submit_report(conn, tournament_id, found.match.id, found.team_id, winner_id,
                                       interaction.user.id, interaction.channel_id)
            teams_by_id = db.get_teams(conn, tournament_id)
            private = reports.vote_reply(conn, outcome, found, winner_id, teams_by_id)
            await interaction.response.edit_message(content=private, view=None)

            if outcome in ("agreed", "skipped"):
                lines = [reports.ending_text(conn, tournament, found.match)]
                if outcome == "skipped":
                    lines += new_walkovers(conn, tournament, before)
                await announce_match_change(self.bot, interaction.channel, found.match.id, "\n".join(lines))
                return
            post = db.get_match_post(conn, found.match.id)
            if post is not None and post["message_id"] is not None:
                await sync_match_post(self.bot, conn, found.match.id)
            elif outcome == "disputed":
                db.claim_match_post(conn, found.match.id, interaction.channel_id, found.teams)
                await sync_match_post(self.bot, conn, found.match.id)
        finally:
            conn.close()

    async def _take_back(self, interaction: discord.Interaction, tournament_id: int,
                         shown: reports.ReportableMatch) -> None:
        conn = db.get_connection()
        try:
            found = self._still_reportable(conn, tournament_id, interaction.user.id, shown)
            if isinstance(found, str) or not db.take_back_report(conn, found.match.id, found.team_id):
                await interaction.response.edit_message(
                    content=found if isinstance(found, str) else "Your side has no vote to take back.", view=None)
                return
            await interaction.response.edit_message(content="Your vote is taken back.", view=None)
            await sync_match_post(self.bot, conn, found.match.id)
        finally:
            conn.close()

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
                lines, _ = withdraw_and_describe(conn, tournament_row, team, "dropped", interaction.user.id, mention)
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
    bot.add_dynamic_items(ReadyUpButton)
    await bot.add_cog(ResultsCog(bot))
