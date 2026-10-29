from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from bracket import BracketError
from common import (
    BotView,
    edit_signups_message,
    load_bracket_embed,
    refuse_taken_name,
    registration_name_autocomplete,
    requires_manage_server,
    resolve_tournament,
    signups_message,
    team_name_autocomplete,
    tournament_name_autocomplete,
)
from embeds import EMBED_DESCRIPTION_LIMIT, mention
from labels import CONSENT_NOTICE, OPTED_OUT_NOTE, draw_description, entrant_lines, join_within
from tournament_builder import (
    MAX_CLOSES_IN_HOURS,
    MIN_CLOSES_IN_HOURS,
    RegistrationClosedError,
    close_overdue_registration,
    close_registration,
)

log = logging.getLogger("winningticket")

# Shown as-is in the option once picked, so the names stay short; the option's description
# explains them.
SEEDING_CHOICES = [app_commands.Choice(name=mode, value=mode) for mode in db.SEEDING_MODES]
SEEDING_OPTION_HELP = "standard (default): top seeds play the lowest. slide: top seeds play mid-ranked. random: no seeds"


def _close_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, seeding_mode: Optional[str] = None,
                        close: Callable[..., int] = close_registration,
                        ) -> tuple[bool, str, discord.Embed | None]:
    """Close registration with `close` and return (closed, announcement, bracket). If too
    few signed up, the tournament stays open and the announcement says why. The
    announcement is for everyone, so it doesn't mention organizer commands."""
    name = tournament["name"]
    seeding_mode = seeding_mode or tournament["seeding_mode"]
    try:
        count = close(conn, tournament["id"], seeding_mode=seeding_mode)
    except BracketError as e:
        return False, f"Registration for **{name}** couldn't close: {e}", None
    content = f"Registration for **{name}** is closed, and {count} entrants were {draw_description(seeding_mode)}."
    try:
        embed = load_bracket_embed(conn, db.get_tournament(conn, tournament["id"]))
    except Exception:
        # The close is already saved, so still announce it; /tournament bracket shows the draw.
        log.exception("Couldn't draw the bracket for tournament %s", tournament["id"])
        embed = None
    return True, content, embed


async def _require_registration(interaction: discord.Interaction, tournament: sqlite3.Row) -> bool:
    """True if the tournament is taking sign-ups; otherwise says so and returns False."""
    if tournament["status"] == "registration":
        return True
    await interaction.response.send_message(
        f"**{tournament['name']}** isn't taking sign-ups (status: {tournament['status']}).", ephemeral=True
    )
    return False


class ConsentView(BotView):
    """Agree or Opt Out, shown privately to a member signing up who hasn't agreed yet."""

    def __init__(self, cog: RegistrationCog, tournament_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.tournament_id = tournament_id

    @discord.ui.button(label="Agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._sign_up(interaction, linked=True)

    @discord.ui.button(label="Opt Out", style=discord.ButtonStyle.secondary)
    async def opt_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._sign_up(interaction, linked=False)

    async def _sign_up(self, interaction: discord.Interaction, linked: bool) -> None:
        user = interaction.user
        conn = db.get_connection()
        try:
            # The notice can sit open while sign-ups close or a double-click signs them up.
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament is None or tournament["status"] != "registration":
                await interaction.response.edit_message(content="Sign-ups for that tournament have closed.", view=None)
                return
            name = tournament["name"]
            if db.get_entrant_team_id(conn, self.tournament_id, user.id) is not None:
                await interaction.response.edit_message(content=f"You're already signed up for **{name}**.", view=None)
                return
            if linked:
                db.record_agreement(conn, interaction.guild_id, user.id)
                db.register_entrant(conn, self.tournament_id, user.display_name, user.id)
                content = (f"✅ You're signed up for **{name}** ({db.entrant_count(conn, self.tournament_id)} entered "
                           f"so far). If you change your mind, `/optout` unlinks your data.")
            else:
                db.opt_out(conn, interaction.guild_id, user.id)  # unlinks entries from before consent was asked
                # A name-only entry can't be told apart from another with the same name, so it
                # mustn't get a number added the way a linked one would.
                if db.get_team_by_name(conn, self.tournament_id, user.display_name) is not None:
                    await interaction.response.edit_message(
                        content=f"Someone is already signed up for **{name}** as {user.display_name}. If that's you, "
                                f"you're in. If not, ask an organizer to add you under another name.",
                        view=None,
                    )
                    return
                db.register_entrant(conn, self.tournament_id, user.display_name, None)
                content = (f"✅ You're signed up for **{name}** as {user.display_name} "
                           f"({db.entrant_count(conn, self.tournament_id)} entered so far). {OPTED_OUT_NOTE}")
            await interaction.response.edit_message(content=content, view=None)
            await self.cog._refresh_signups_message(conn, self.tournament_id)
        finally:
            conn.close()


class RegistrationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="registration", description="Run a tournament that players sign up for", guild_only=True
    )

    async def cog_load(self) -> None:
        self.close_due_registrations.start()

    async def cog_unload(self) -> None:
        self.close_due_registrations.cancel()

    # Often enough that sign-ups close within seconds of the time players were shown.
    @tasks.loop(seconds=5)
    async def close_due_registrations(self) -> None:
        # An exception escaping a tasks.loop stops it until the bot restarts, so every
        # error is logged here, and one tournament failing doesn't hold up the others.
        try:
            conn = db.get_connection()
            try:
                for tournament in db.get_tournaments_due_to_close(conn):
                    try:
                        await self._close_when_due(conn, tournament)
                    except Exception:
                        log.exception("Couldn't close registration for tournament %s", tournament["id"])
            finally:
                conn.close()
        except Exception:
            log.exception("Couldn't check for registrations to close")

    async def _close_when_due(self, conn: sqlite3.Connection, tournament: sqlite3.Row) -> None:
        try:
            closed, content, embed = _close_and_describe(conn, tournament, close=close_overdue_registration)
        except RegistrationClosedError:
            return  # an organizer closed it by hand since the list of due tournaments was fetched
        if not closed:
            content += " Sign-ups stay open until an organizer closes them."
        await self._refresh_signups_message(conn, tournament["id"])
        await self._announce(tournament["channel_id"], content, embed)

    async def _refresh_signups_message(self, conn: sqlite3.Connection, tournament_id: int) -> None:
        tournament = db.get_tournament(conn, tournament_id)
        await edit_signups_message(self.bot, tournament, signups_message(tournament, db.entrant_count(conn, tournament_id)))

    @close_due_registrations.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _announce(self, channel_id: int | None, content: str, embed: discord.Embed | None) -> None:
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None:
            log.warning("Can't find channel %s to post: %s", channel_id, content)
            return
        try:
            await channel.send(content, embed=embed)
        except discord.HTTPException:
            log.exception("Couldn't post to channel %s", channel_id)

    @group.command(name="open", description="Create a tournament that players sign up for with /register")
    @app_commands.describe(
        name="Tournament name",
        closes_in_hours="Close sign-ups automatically after this many hours (leave blank to close by hand)",
        seeding=SEEDING_OPTION_HELP,
    )
    @app_commands.choices(seeding=SEEDING_CHOICES)
    @requires_manage_server
    async def open_signups(self, interaction: discord.Interaction, name: str,
                   closes_in_hours: app_commands.Range[float, MIN_CLOSES_IN_HOURS, MAX_CLOSES_IN_HOURS] | None = None,
                   seeding: str = "standard") -> None:
        closes_at = None
        if closes_in_hours is not None:
            closes_at = datetime.now(timezone.utc) + timedelta(hours=closes_in_hours)
        conn = db.get_connection()
        try:
            if await refuse_taken_name(interaction, conn, name):
                return
            tournament_id = db.open_registration(
                conn, interaction.guild_id, name, interaction.user.id, interaction.channel_id,
                db.to_db_time(closes_at) if closes_at is not None else None, seeding,
            )
            await interaction.response.send_message(signups_message(db.get_tournament(conn, tournament_id), 0))
            message = await interaction.original_response()
            db.set_announcement_message(conn, tournament_id, message.id)
        finally:
            conn.close()

    @group.command(name="close", description="Close sign-ups and draw the bracket")
    @app_commands.describe(name="Tournament name",
                           seeding="Override the seeding chosen at open: standard, slide or random")
    @app_commands.autocomplete(name=registration_name_autocomplete)
    @app_commands.choices(seeding=SEEDING_CHOICES)
    @requires_manage_server
    async def close_signups(self, interaction: discord.Interaction, name: str, seeding: str | None = None) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name, needs_bracket=False)
            if tournament is None or not await _require_registration(interaction, tournament):
                return
            try:
                closed, content, embed = _close_and_describe(conn, tournament, seeding)
            except RegistrationClosedError:
                await interaction.response.send_message(f"Sign-ups for **{name}** have already closed.", ephemeral=True)
                return
            if not closed:
                await interaction.response.send_message(content, ephemeral=True)
                return
            await interaction.response.send_message(content, embed=embed)
            await interaction.followup.send(
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin.", ephemeral=True
            )
            await self._refresh_signups_message(conn, tournament["id"])
        finally:
            conn.close()

    @group.command(name="entrants", description="See who has signed up for a tournament")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def entrants(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name, needs_bracket=False)
            if tournament is None:
                return
            entrants = db.get_entrants(conn, tournament["id"])
        finally:
            conn.close()
        embed = discord.Embed(title=f"Entrants: {name}")
        if not entrants:
            embed.description = "Nobody has signed up yet."
        else:
            embed.description = join_within(entrant_lines(entrants, mention), EMBED_DESCRIPTION_LIMIT)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="add", description="Sign up a member, or an entrant with no Discord account")
    @app_commands.describe(
        tournament="Tournament name",
        user="Member to sign up",
        name="Entrant name (defaults to the member's server name; needed when no member is given)",
    )
    @app_commands.autocomplete(tournament=registration_name_autocomplete)
    @requires_manage_server
    async def add(self, interaction: discord.Interaction, tournament: str,
                  user: discord.Member | None = None, name: str | None = None) -> None:
        if user is None and name is None:
            await interaction.response.send_message(
                "Give a member to sign up, or a name for an entrant with no Discord account.", ephemeral=True
            )
            return
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            if user is not None and db.get_entrant_team_id(conn, tournament_row["id"], user.id) is not None:
                await interaction.response.send_message(
                    f"{user.display_name} is already signed up for **{tournament}**.", ephemeral=True
                )
                return
            # A member's entry is only linked to them once they've agreed to it themselves.
            linked = user is not None and db.has_agreed(conn, interaction.guild_id, user.id)
            _, entrant_name = db.register_entrant(
                conn, tournament_row["id"], name or user.display_name, user.id if linked else None
            )
            who = f"{entrant_name} ({user.mention})" if linked else entrant_name
            note = (f" They haven't agreed to link their data to their account, so they're entered by name only."
                    if user is not None and not linked else "")
            await interaction.response.send_message(
                f"✅ Signed up {who} for **{tournament}** ({db.entrant_count(conn, tournament_row['id'])} entered so far).{note}",
                ephemeral=True,
            )
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()

    @group.command(name="remove", description="Withdraw an entrant from a tournament")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant to withdraw")
    @app_commands.autocomplete(tournament=registration_name_autocomplete, entrant=team_name_autocomplete)
    @requires_manage_server
    async def remove(self, interaction: discord.Interaction, tournament: str, entrant: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            team = db.get_team_by_name(conn, tournament_row["id"], entrant)
            if team is None:
                await interaction.response.send_message(
                    f"No entrant named '{entrant}' in **{tournament}**.", ephemeral=True
                )
                return
            db.delete_team(conn, team["id"])
            await interaction.response.send_message(f"Withdrew {entrant} from **{tournament}**.", ephemeral=True)
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()

    @app_commands.command(name="register", description="Sign up for a tournament")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=registration_name_autocomplete)
    @app_commands.guild_only()
    async def register(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            if db.get_entrant_team_id(conn, tournament_row["id"], interaction.user.id) is not None:
                await interaction.response.send_message(
                    f"You're already signed up for **{tournament}**.", ephemeral=True
                )
                return
            if not db.has_agreed(conn, interaction.guild_id, interaction.user.id):
                await interaction.response.send_message(
                    CONSENT_NOTICE, view=ConsentView(self, tournament_row["id"]), ephemeral=True
                )
                return
            db.register_entrant(conn, tournament_row["id"], interaction.user.display_name, interaction.user.id)
            await interaction.response.send_message(
                f"✅ You're signed up for **{tournament}** ({db.entrant_count(conn, tournament_row['id'])} entered so far).",
                ephemeral=True,
            )
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()

    @app_commands.command(name="unregister", description="Withdraw from a tournament you signed up for")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=registration_name_autocomplete)
    @app_commands.guild_only()
    async def unregister(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            if not db.unregister_entrant(conn, tournament_row["id"], interaction.user.id):
                await interaction.response.send_message(
                    f"You aren't signed up for **{tournament}** under your account. If you opted out when "
                    f"signing up, ask an organizer to withdraw you.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(f"You withdrew from **{tournament}**.", ephemeral=True)
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationCog(bot))
