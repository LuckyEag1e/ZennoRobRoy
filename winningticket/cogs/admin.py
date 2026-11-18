from __future__ import annotations

import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, bracket_order, matches_with_both_teams, resolve_all_slots
from bracket_changes import (
    REINSTATABLE_STATUSES,
    WITHDRAWABLE_STATUSES,
    clear_result_and_describe,
    later_results_problem,
    reinstate_problem,
    set_result_and_describe,
    skip_and_describe,
    withdraw_and_describe,
)
from cogs.results import announce_match_change
from common import (
    BotView,
    MatchPicker,
    TournamentSession,
    WinnerPicker,
    announce,
    any_entrant_autocomplete,
    any_tournament_autocomplete,
    edit_signups_message,
    outdated_pick_message,
    playing_entrant_autocomplete,
    resolve_tournament,
    team_name_autocomplete,
    tournament_name_autocomplete,
    withdrawn_entrant_autocomplete,
)
from embeds import EMBED_DESCRIPTION_LIMIT, mention
from labels import ban_lines, dropped_results_note, feedback_page, join_within, reinstated_line, team_label

# A walkover follows from a withdrawal, so a played result there would mean the game was
# played after all. Reinstating the entrant reopens the match.
WALKOVER_NOT_SETTABLE = ("An entrant in this match has left, so it was settled without being played. "
                         "Use `/admin reinstate` first if it was played after all.")

STATUS_CHOICES = [app_commands.Choice(name=status, value=status) for status in db.FORCEABLE_STATUSES]
# Dropping out is the player's own call, through /match dropout.
ORGANIZER_WITHDRAWALS = [app_commands.Choice(name=reason, value=reason) for reason in ("withdrawn", "disqualified")]


class ConfirmView(BotView):
    def __init__(self, on_confirm, timeout: int = 120) -> None:
        super().__init__(timeout=timeout)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


def later_results_now(session: TournamentSession, match: Match) -> str | None:
    """later_results_problem as things stand, for checking before an organizer confirms."""
    conn = db.get_connection()
    try:
        return later_results_problem(conn, db.get_tournament(conn, session.tournament_id), [match.id])
    finally:
        conn.close()


async def _announce_or_say(interaction: discord.Interaction, match_id: int, text: str) -> None:
    """Announce an organizer's change to a match, or tell them privately if it couldn't be posted."""
    if not await announce_match_change(interaction.client, interaction.channel, match_id, text):
        await interaction.followup.send(f"This couldn't be posted in the channel:\n{text}", ephemeral=True)


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
            status = db.set_tournament_status(conn, tournament_row["id"], status)
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
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None:
                return
            db.delete_tournament(conn, tournament_row["id"])
        finally:
            conn.close()
        await interaction.response.send_message(f"🗑️ Deleted tournament **{tournament}** and all its data.", ephemeral=True)
        if tournament_row["status"] == "registration":
            await edit_signups_message(self.bot, tournament_row, f"📝 Sign-ups for **{tournament}** were cancelled.")

    @group.command(name="linkplayer", description="Link an entrant to the member who plays as them")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant the member plays as", user="Member to link")
    @app_commands.autocomplete(tournament=any_tournament_autocomplete, entrant=any_entrant_autocomplete)
    async def linkplayer(self, interaction: discord.Interaction, tournament: str, entrant: str, user: discord.Member) -> None:
        conn = db.get_connection()
        try:
            tournament_row = db.get_tournament_by_name_any_status(conn, interaction.guild_id, tournament)
            if tournament_row is None:
                await interaction.response.send_message(f"No tournament named '{tournament}' in this server.", ephemeral=True)
                return
            label = tournament_row["name"]
            if not db.has_agreed(conn, interaction.guild_id, user.id):
                await interaction.response.send_message(
                    f"{user.mention} hasn't agreed to link their data to their account, so entries "
                    f"can't be linked to them. They're asked when they next use `/register`.",
                    ephemeral=True,
                )
                return
            entrant_row = db.get_team_by_name(conn, tournament_row["id"], entrant)
            if entrant_row is None:
                await interaction.response.send_message(f"No entrant named '{entrant}' in **{label}**.", ephemeral=True)
                return
            # Entrants are one player each, and a second member would make its games team games.
            existing = db.team_member_ids(conn, entrant_row["id"])
            if existing:
                await interaction.response.send_message(
                    f"**{entrant}** in **{label}** is already linked to {mention(existing[0])}.", ephemeral=True
                )
                return
            linked = db.link_team_member(conn, tournament_row["id"], entrant_row["id"], user.id)
        finally:
            conn.close()
        if not linked:
            await interaction.response.send_message(
                f"{user.mention} is already linked to an entrant in **{label}**.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔗 Linked {user.mention} to **{entrant}** in **{label}**. Its games against other linked members count toward their rating.",
            ephemeral=True,
        )

    @group.command(name="unlinkplayer", description="Unlink the member behind an entrant, keeping their agreement and other entries")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant to unlink")
    @app_commands.autocomplete(tournament=any_tournament_autocomplete, entrant=any_entrant_autocomplete)
    async def unlinkplayer(self, interaction: discord.Interaction, tournament: str, entrant: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = db.get_tournament_by_name_any_status(conn, interaction.guild_id, tournament)
            if tournament_row is None:
                await interaction.response.send_message(f"No tournament named '{tournament}' in this server.", ephemeral=True)
                return
            entrant_row = db.get_team_by_name(conn, tournament_row["id"], entrant)
            if entrant_row is None:
                await interaction.response.send_message(f"No entrant named '{entrant}' in **{tournament}**.", ephemeral=True)
                return
            user_id = db.unlink_entrant(conn, entrant_row["id"])
        finally:
            conn.close()
        if user_id is None:
            await interaction.response.send_message(f"**{entrant}** isn't linked to anyone.", ephemeral=True)
            return
        content = (f"Unlinked {mention(user_id)} from **{entrant}** in **{tournament}**. "
                   f"Their agreement and other entries are unchanged.")
        if tournament_row["status"] == "registration":
            content += " To enter them separately, use `/registration add` with them and another name."
        await interaction.response.send_message(content, ephemeral=True)

    @group.command(name="editteam", description="Fix an entrant's name or seed")
    @app_commands.describe(tournament="Tournament name", team="Entrant to edit", new_name="New name (optional)", new_seed="New seed (optional)")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete, team=team_name_autocomplete)
    async def edit_team(self, interaction: discord.Interaction, tournament: str, team: str,
                         new_name: str | None = None, new_seed: int | None = None) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None:
                return
            team_row = db.get_team_by_name(conn, tournament_row["id"], team)
            if team_row is None:
                await interaction.response.send_message(f"No entrant named '{team}' in **{tournament}**.", ephemeral=True)
                return
            if new_name is None and new_seed is None:
                await interaction.response.send_message("Provide new_name and/or new_seed.", ephemeral=True)
                return
            if new_name is not None and db.team_name_taken(conn, tournament_row["id"], new_name, team_row["id"]):
                await interaction.response.send_message(
                    f"**{tournament}** already has an entrant named '{new_name}'.", ephemeral=True
                )
                return
            db.update_team(conn, team_row["id"], new_name, new_seed, seed_given=new_seed is not None)
        finally:
            conn.close()
        await interaction.response.send_message(f"Updated entrant '{team}'.", ephemeral=True)

    @group.command(name="setresult", description="Force-set (or overwrite) a match's real result")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def setresult(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            session = await self._load_session(interaction, conn, tournament)
            if session is None:
                return
            outcomes = db.get_outcomes(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, outcomes.winners.get, session.feeders_by_target)
        known = [m for m in matches_with_both_teams(session.matches_by_id, slots) if m.id not in outcomes.unplayed]
        if not known:
            await interaction.response.send_message("No matches have both teams known yet.", ephemeral=True)
            return

        async def ask_for_winner(pick_interaction: discord.Interaction, match: Match) -> None:
            team_a, team_b = slots[match.id]

            async def choose(winner_interaction: discord.Interaction, winner_id: int, loser_id: int) -> None:
                winner_label = team_label(session.teams_by_id, winner_id)

                def current_match() -> db.MatchNow | None:
                    conn = db.get_connection()
                    try:
                        return db.match_now(conn, session.tournament_id, match.id)
                    finally:
                        conn.close()

                async def record(confirm_interaction: discord.Interaction) -> None:
                    conn = db.get_connection()
                    try:
                        # Checked again here: the confirm button can sit unclicked while others edit results.
                        tournament_row = db.get_tournament(conn, session.tournament_id)
                        now = db.match_now(conn, session.tournament_id, match.id)
                        refusal = (outdated_pick_message(now, (winner_id, loser_id))
                                   or (WALKOVER_NOT_SETTABLE if now.unplayed else None)
                                   or later_results_problem(conn, tournament_row, [match.id]))
                        if refusal is None:
                            text, dropped = set_result_and_describe(conn, tournament_row, match, winner_id, loser_id,
                                                                    confirm_interaction.user.id, mention)
                    finally:
                        conn.close()
                    if refusal is not None:
                        await confirm_interaction.response.edit_message(content=refusal, view=None)
                        return
                    await confirm_interaction.response.edit_message(
                        content=f"✅ Set Round {match.played_round} winner to **{winner_label}**.{dropped_results_note(dropped)}",
                        view=None,
                    )
                    await _announce_or_say(confirm_interaction, match.id, text)

                now = current_match()
                refusal = (outdated_pick_message(now, (winner_id, loser_id))
                           or (WALKOVER_NOT_SETTABLE if now.unplayed else None)
                           or later_results_now(session, match))
                if refusal is not None:
                    await winner_interaction.response.edit_message(content=refusal, view=None)
                elif now.winner == winner_id:
                    await winner_interaction.response.edit_message(
                        content=f"**{winner_label}** already won this match, so there's nothing to change.", view=None
                    )
                elif now.winner is not None or now.skipped:
                    await winner_interaction.response.edit_message(
                        content=f"This match already has a recorded result. Overwrite with **{winner_label}**?",
                        view=ConfirmView(on_confirm=record),
                    )
                else:
                    await record(winner_interaction)

            await pick_interaction.response.edit_message(
                content=f"Round {match.played_round}: **{team_label(session.teams_by_id, team_a)}** vs "
                        f"**{team_label(session.teams_by_id, team_b)}** — force winner to:",
                view=WinnerPicker(team_a, team_b, session.teams_by_id, choose),
            )

        await interaction.response.send_message(
            "Select a match to force a result for:",
            view=MatchPicker(known, slots, session.teams_by_id, ask_for_winner, outcomes.decided),
            ephemeral=True,
        )

    @group.command(name="clearresult", description="Delete a recorded match result or skip")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def clearresult(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            session = await self._load_session(interaction, conn, tournament)
            if session is None:
                return
            outcomes = db.get_outcomes(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, outcomes.winners.get, session.feeders_by_target)
        # Walkovers follow from withdrawals, so they're undone with /admin reinstate instead.
        clearable = set(outcomes.played) | outcomes.skipped
        decided = sorted((m for m in session.matches_by_id.values() if m.id in clearable), key=bracket_order)
        if not decided:
            await interaction.response.send_message("No recorded results to clear.", ephemeral=True)
            return

        async def confirm_clear(pick_interaction: discord.Interaction, match: Match) -> None:
            shown = (outcomes.winners.get(match.id), match.id in outcomes.skipped)

            async def clear(confirm_interaction: discord.Interaction) -> None:
                conn = db.get_connection()
                try:
                    # Only clear the result this menu showed: someone may have cleared it,
                    # re-recorded it, or changed the matchup since it opened.
                    now = db.match_now(conn, session.tournament_id, match.id)
                    refusal = outdated_pick_message(now, slots[match.id])
                    tournament_row = db.get_tournament(conn, session.tournament_id)
                    if refusal is None and now.winner is None and not now.skipped:
                        refusal = "That result was already cleared."
                    elif refusal is None and (now.winner, now.skipped) != shown:
                        refusal = "That result has changed since the menu was opened. Run the command again."
                    refusal = refusal or later_results_problem(conn, tournament_row, [match.id])
                    if refusal is None:
                        text, dropped = clear_result_and_describe(conn, tournament_row, match, slots[match.id],
                                                                  confirm_interaction.user.id, mention)
                finally:
                    conn.close()
                if refusal is not None:
                    await confirm_interaction.response.edit_message(content=refusal, view=None)
                    return
                await confirm_interaction.response.edit_message(
                    content=f"✅ Result cleared.{dropped_results_note(dropped)}", view=None
                )
                await _announce_or_say(confirm_interaction, match.id, text)

            refusal = later_results_now(session, match)
            if refusal is not None:
                await pick_interaction.response.edit_message(content=refusal, view=None)
                return
            await pick_interaction.response.edit_message(
                content="Clear this match's recorded result?", view=ConfirmView(on_confirm=clear),
            )

        await interaction.response.send_message(
            "Select a result to clear:",
            view=MatchPicker(decided, slots, session.teams_by_id, confirm_clear),
            ephemeral=True,
        )

    async def _find_entrant(self, interaction: discord.Interaction, conn, tournament: str, entrant: str,
                            allowed_statuses: tuple[str, ...]) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        """The tournament and entrant named, if the tournament's status is one of
        `allowed_statuses`, or None after saying why not."""
        tournament_row = await resolve_tournament(interaction, conn, tournament)
        if tournament_row is None:
            return None
        if tournament_row["status"] not in allowed_statuses:
            await interaction.response.send_message(f"**{tournament}** has finished.", ephemeral=True)
            return None
        team = db.get_team_by_name(conn, tournament_row["id"], entrant)
        if team is None:
            await interaction.response.send_message(f"No entrant named '{entrant}' in **{tournament}**.", ephemeral=True)
            return None
        return tournament_row, team

    @group.command(name="withdraw", description="Withdraw or disqualify an entrant, giving their remaining matches to their opponents")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant to take out", reason="Shown publicly")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete, entrant=playing_entrant_autocomplete)
    @app_commands.choices(reason=ORGANIZER_WITHDRAWALS)
    async def withdraw(self, interaction: discord.Interaction, tournament: str, entrant: str, reason: str) -> None:
        conn = db.get_connection()
        try:
            found = await self._find_entrant(interaction, conn, tournament, entrant, WITHDRAWABLE_STATUSES)
            if found is None:
                return
            tournament_row, team = found
            if team["withdrawn"]:
                await interaction.response.send_message(f"**{entrant}** has already left **{tournament}**.", ephemeral=True)
                return
            lines, dropped = withdraw_and_describe(conn, tournament_row, team, reason, interaction.user.id, mention)
        finally:
            conn.close()
        await announce(interaction, "\n".join(lines),
                       f"Done. Undo it with `/admin reinstate`.{dropped_results_note(dropped)}", ping=False)

    @group.command(name="reinstate", description="Undo a dropout, withdrawal or disqualification")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant to bring back")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete, entrant=withdrawn_entrant_autocomplete)
    async def reinstate(self, interaction: discord.Interaction, tournament: str, entrant: str) -> None:
        conn = db.get_connection()
        try:
            found = await self._find_entrant(interaction, conn, tournament, entrant, REINSTATABLE_STATUSES)
            if found is None:
                return
            tournament_row, team = found
            if not team["withdrawn"]:
                await interaction.response.send_message(f"**{entrant}** is still playing.", ephemeral=True)
                return
            refusal = reinstate_problem(conn, tournament_row, team["id"])
            if refusal is not None:
                await interaction.response.send_message(refusal, ephemeral=True)
                return
            dropped = db.reinstate_entrant(conn, tournament_row["id"], team["id"])
        finally:
            conn.close()
        await announce(interaction, reinstated_line(team["name"], tournament_row["name"], mention(interaction.user.id)),
                       f"Reinstated **{entrant}**. The walkovers they gave are undone.{dropped_results_note(dropped)}",
                       ping=False)

    @group.command(name="skipmatch", description="Call a match off with no winner")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def skipmatch(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            session = await self._load_session(interaction, conn, tournament)
            if session is None:
                return
            outcomes = db.get_outcomes(conn, session.tournament_id)
        finally:
            conn.close()

        slots = resolve_all_slots(session.matches_by_id, outcomes.winners.get, session.feeders_by_target)
        skippable = [m for m in matches_with_both_teams(session.matches_by_id, slots) if m.id not in outcomes.skipped]
        if not skippable:
            await interaction.response.send_message("No matches have both teams known yet.", ephemeral=True)
            return

        async def confirm_skip(pick_interaction: discord.Interaction, match: Match) -> None:
            teams = slots[match.id]
            names = " vs ".join(f"**{team_label(session.teams_by_id, team)}**" for team in teams)

            async def skip(confirm_interaction: discord.Interaction) -> None:
                conn = db.get_connection()
                try:
                    now = db.match_now(conn, session.tournament_id, match.id)
                    refusal = outdated_pick_message(now, teams)
                    if refusal is None and now.skipped:
                        refusal = "That match was already called off."
                    tournament_row = db.get_tournament(conn, session.tournament_id)
                    refusal = refusal or later_results_problem(conn, tournament_row, [match.id])
                    if refusal is None:
                        lines, dropped = skip_and_describe(conn, tournament_row, match.id, teams,
                                                           confirm_interaction.user.id, mention)
                finally:
                    conn.close()
                if refusal is not None:
                    await confirm_interaction.response.edit_message(content=refusal, view=None)
                    return
                await confirm_interaction.response.edit_message(
                    content=f"✅ Called off.{dropped_results_note(dropped)}", view=None
                )
                await _announce_or_say(confirm_interaction, match.id, "\n".join(lines))

            refusal = later_results_now(session, match)
            if refusal is not None:
                await pick_interaction.response.edit_message(content=refusal, view=None)
                return
            await pick_interaction.response.edit_message(
                content=f"Call off {names} with no winner? Picks on it won't score, and "
                        f"whoever they'd have played next advances by walkover.",
                view=ConfirmView(on_confirm=skip),
            )

        await interaction.response.send_message(
            "Select a match to call off:",
            view=MatchPicker(skippable, slots, session.teams_by_id, confirm_skip, outcomes.decided),
            ephemeral=True,
        )

    @group.command(name="clearpredictions", description="Wipe one user's saved predictions for a tournament")
    @app_commands.describe(tournament="Tournament name", user="The user whose predictions should be cleared")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def clearpredictions(self, interaction: discord.Interaction, tournament: str, user: discord.Member) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
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

    @group.command(name="feedback", description="Read the anonymous feedback members have sent, newest first")
    @app_commands.describe(page="Page to show (5 per page)")
    async def feedback(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
        conn = db.get_connection()
        try:
            entries, page, page_count = feedback_page(db.get_feedback(conn, interaction.guild_id), page)
        finally:
            conn.close()
        embed = discord.Embed(title="Anonymous feedback")
        if not entries:
            embed.description = "No feedback yet."
        for entry in entries:
            embed.add_field(name=f"#{entry['id']} · {entry['created_on']}", value=entry["message"], inline=False)
        embed.set_footer(text=f"Page {page} of {page_count}. Remove handled feedback with /admin deletefeedback.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="deletefeedback", description="Remove a piece of feedback once it's been dealt with")
    @app_commands.describe(number="The feedback's # number, from /admin feedback")
    async def deletefeedback(self, interaction: discord.Interaction, number: int) -> None:
        conn = db.get_connection()
        try:
            deleted = db.delete_feedback(conn, interaction.guild_id, number)
        finally:
            conn.close()
        message = f"Deleted feedback #{number}." if deleted else f"There's no feedback #{number}."
        await interaction.response.send_message(message, ephemeral=True)

    @group.command(name="ban", description="Block a member from using the bot in this server")
    @app_commands.describe(user="Member to block", reason="Why (only admins see this)")
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        # Otherwise one admin could lock another out of /admin unban. This assumes only
        # Manage Server members can use /admin, as its default_permissions say. A server that
        # opens /admin to others with permission overrides can still undo a ban as long as
        # someone with Manage Server is around.
        if user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "Members with the Manage Server permission can't be banned from the bot.", ephemeral=True
            )
            return
        conn = db.get_connection()
        try:
            db.ban_user(conn, interaction.guild_id, user.id, interaction.user.id, reason)
        finally:
            conn.close()
        await interaction.response.send_message(
            f"🚫 {user.mention} can no longer use the bot in this server. Undo with `/admin unban`.", ephemeral=True
        )

    @group.command(name="unban", description="Let a banned member use the bot again")
    @app_commands.describe(user="Member to unblock")
    async def unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        conn = db.get_connection()
        try:
            unbanned = db.unban_user(conn, interaction.guild_id, user.id)
        finally:
            conn.close()
        message = f"{user.mention} can use the bot again." if unbanned else f"{user.mention} isn't banned."
        await interaction.response.send_message(message, ephemeral=True)

    @group.command(name="bans", description="List members banned from using the bot")
    async def bans(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            bans = db.get_bans(conn, interaction.guild_id)
        finally:
            conn.close()
        embed = discord.Embed(title="Banned from the bot")
        embed.description = join_within(ban_lines(bans, mention), EMBED_DESCRIPTION_LIMIT) if bans else "Nobody is banned."
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
