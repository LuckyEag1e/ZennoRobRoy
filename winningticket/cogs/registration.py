from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from bracket import BracketError
from common import (
    edit_signups_message,
    load_bracket_embed,
    registration_name_autocomplete,
    requires_manage_server,
    resolve_tournament,
    signups_message,
    team_name_autocomplete,
    tournament_name_autocomplete,
)
from embeds import EMBED_DESCRIPTION_LIMIT, mention
from labels import entrant_lines, join_within
from tournament_builder import RegistrationClosedError, close_overdue_registration, close_registration

log = logging.getLogger("winningticket")


def _close_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row,
                        close: Callable[[sqlite3.Connection, int], int] = close_registration,
                        ) -> tuple[str, discord.Embed | None]:
    """Close registration with `close` and return the announcement and the new bracket.
    If too few signed up, the tournament stays open, and the message says why with no
    bracket. The announcement is for everyone, so it doesn't mention organizer commands."""
    name = tournament["name"]
    try:
        count = close(conn, tournament["id"])
    except BracketError as e:
        return f"Registration for **{name}** couldn't close: {e}", None
    content = f"Registration for **{name}** is closed, and {count} entrants were drawn into the bracket."
    return content, load_bracket_embed(conn, db.get_tournament(conn, tournament["id"]))


async def _require_registration(interaction: discord.Interaction, tournament: sqlite3.Row) -> bool:
    """True if the tournament is taking sign-ups; otherwise says so and returns False."""
    if tournament["status"] == "registration":
        return True
    await interaction.response.send_message(
        f"**{tournament['name']}** isn't taking sign-ups (status: {tournament['status']}).", ephemeral=True
    )
    return False


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
            content, embed = _close_and_describe(conn, tournament, close_overdue_registration)
        except RegistrationClosedError:
            return  # an organizer closed it by hand since the list of due tournaments was fetched
        if embed is None:
            content += " Sign-ups stay open until an organizer closes them."
        updated = db.get_tournament(conn, tournament["id"])
        await edit_signups_message(self.bot, updated, signups_message(updated))
        await self._announce(tournament["channel_id"], content, embed)

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
    )
    @requires_manage_server
    async def open_signups(self, interaction: discord.Interaction, name: str,
                   closes_in_hours: app_commands.Range[float, 0.01, 1440.0] | None = None) -> None:
        closes_at = None
        if closes_in_hours is not None:
            closes_at = datetime.now(timezone.utc) + timedelta(hours=closes_in_hours)
        conn = db.get_connection()
        try:
            if db.get_tournament_by_name(conn, interaction.guild_id, name) is not None:
                await interaction.response.send_message(
                    f"A tournament named '{name}' already exists in this server.", ephemeral=True
                )
                return
            tournament_id = db.open_registration(
                conn, interaction.guild_id, name, interaction.user.id, interaction.channel_id,
                db.to_db_time(closes_at) if closes_at is not None else None,
            )
            await interaction.response.send_message(signups_message(db.get_tournament(conn, tournament_id)))
            message = await interaction.original_response()
            db.set_announcement_message(conn, tournament_id, message.id)
        finally:
            conn.close()

    @group.command(name="close", description="Close sign-ups and draw the bracket")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=registration_name_autocomplete)
    @requires_manage_server
    async def close_signups(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name, needs_bracket=False)
            if tournament is None or not await _require_registration(interaction, tournament):
                return
            try:
                content, embed = _close_and_describe(conn, tournament)
            except RegistrationClosedError:
                await interaction.response.send_message(f"Sign-ups for **{name}** have already closed.", ephemeral=True)
                return
            if embed is None:
                await interaction.response.send_message(content, ephemeral=True)
                return
            await interaction.response.send_message(content, embed=embed)
            await interaction.followup.send(
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin.", ephemeral=True
            )
            updated = db.get_tournament(conn, tournament["id"])
            await edit_signups_message(self.bot, updated, signups_message(updated))
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
            _, entrant_name = db.register_entrant(
                conn, tournament_row["id"], name or user.display_name, user.id if user is not None else None
            )
            count = len(db.get_entrants(conn, tournament_row["id"]))
        finally:
            conn.close()
        who = f"{entrant_name} ({user.mention})" if user is not None else entrant_name
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} signed up {who} for **{tournament}** ({count} entered so far)."
        )

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
        finally:
            conn.close()
        await interaction.response.send_message(f"{interaction.user.mention} withdrew {entrant} from **{tournament}**.")

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
            db.register_entrant(conn, tournament_row["id"], interaction.user.display_name, interaction.user.id)
            count = len(db.get_entrants(conn, tournament_row["id"]))
        finally:
            conn.close()
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} signed up for **{tournament}** ({count} entered so far)."
        )

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
            withdrawn = db.unregister_entrant(conn, tournament_row["id"], interaction.user.id)
        finally:
            conn.close()
        if not withdrawn:
            await interaction.response.send_message(f"You aren't signed up for **{tournament}**.", ephemeral=True)
            return
        await interaction.response.send_message(f"{interaction.user.mention} withdrew from **{tournament}**.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationCog(bot))
