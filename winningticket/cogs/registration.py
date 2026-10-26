from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from bracket import BracketError
from common import (
    load_bracket_embed,
    registration_name_autocomplete,
    requires_manage_server,
    resolve_tournament,
    team_name_autocomplete,
    tournament_name_autocomplete,
)
from embeds import EMBED_DESCRIPTION_LIMIT
from labels import join_within
from tournament_builder import close_registration

log = logging.getLogger("winningticket")


def _close_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row) -> tuple[str, discord.Embed | None]:
    """Close registration and return the announcement and the new bracket. If too few
    signed up, the tournament stays open, and the message says why with no bracket."""
    name = tournament["name"]
    try:
        count = close_registration(conn, tournament["id"])
    except BracketError as e:
        return f"Registration for **{name}** couldn't close: {e}", None
    content = (
        f"Registration for **{name}** is closed, and {count} entrants were drawn into the bracket. "
        f"Run `/tournament open name:{name}` when you're ready for predictions to begin."
    )
    return content, load_bracket_embed(conn, db.get_tournament(conn, tournament["id"]))


def _signups_message(tournament: sqlite3.Row) -> str:
    """The sign-ups announcement as the tournament now stands. It's posted when sign-ups
    open and rewritten when they close, so its countdown doesn't keep running."""
    name = tournament["name"]
    if tournament["status"] != "registration":
        return f"📝 Sign-ups for **{name}** are closed."
    closes_at = tournament["registration_closes_at"]
    closing = (
        f"Sign-ups close <t:{int(db.from_db_time(closes_at).timestamp())}:R>."
        if closes_at is not None else "An organizer will close sign-ups."
    )
    return f"📝 Sign-ups are open for **{name}**! Run `/register tournament:{name}` to enter. {closing}"


def _entrant_label(entrant: sqlite3.Row) -> str:
    if entrant["user_id"] is None:
        return f"{entrant['name']} (no Discord account)"
    return f"{entrant['name']} (<@{entrant['user_id']}>)"


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
        conn = db.get_connection()
        try:
            for tournament in db.get_tournaments_due_to_close(conn):
                content, embed = _close_and_describe(conn, tournament)
                if embed is None:
                    # Don't retry on every check; an organizer can close it by hand once more sign up.
                    db.clear_registration_deadline(conn, tournament["id"])
                    content += " Close it by hand with `/registration close` once more players have signed up."
                await self._update_signups_message(db.get_tournament(conn, tournament["id"]))
                await self._announce(tournament["channel_id"], content, embed)
        finally:
            conn.close()

    @close_due_registrations.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_signups_message(self, tournament: sqlite3.Row) -> None:
        """Rewrite the sign-ups announcement to match the tournament, if it can still be found."""
        channel = self.bot.get_channel(tournament["channel_id"]) if tournament["channel_id"] is not None else None
        if channel is None or tournament["announcement_message_id"] is None:
            return
        try:
            await channel.get_partial_message(tournament["announcement_message_id"]).edit(
                content=_signups_message(tournament)
            )
        except discord.HTTPException:
            log.warning("Couldn't update the sign-ups message for tournament %s", tournament["id"])

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
            await interaction.response.send_message(_signups_message(db.get_tournament(conn, tournament_id)))
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
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None or not await _require_registration(interaction, tournament):
                return
            content, embed = _close_and_describe(conn, tournament)
            if embed is None:
                await interaction.response.send_message(content, ephemeral=True)
                return
            await interaction.response.send_message(content, embed=embed)
            await self._update_signups_message(db.get_tournament(conn, tournament["id"]))
        finally:
            conn.close()

    @group.command(name="entrants", description="See who has signed up for a tournament")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def entrants(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return
            entrants = db.get_entrants(conn, tournament["id"])
        finally:
            conn.close()
        embed = discord.Embed(title=f"Entrants: {name}")
        if not entrants:
            embed.description = "Nobody has signed up yet."
        else:
            lines = [f"{i}. {_entrant_label(entrant)}" for i, entrant in enumerate(entrants, start=1)]
            embed.description = join_within(lines, EMBED_DESCRIPTION_LIMIT)
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
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            if user is not None and db.get_entrant_team_id(conn, tournament_row["id"], user.id) is not None:
                await interaction.response.send_message(
                    f"{user.display_name} is already signed up for **{tournament}**.", ephemeral=True
                )
                return
            team_id = db.register_entrant(
                conn, tournament_row["id"], name or user.display_name, user.id if user is not None else None
            )
            entrant_name = db.get_teams(conn, tournament_row["id"])[team_id]["name"]
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
            tournament_row = await resolve_tournament(interaction, conn, tournament)
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
            tournament_row = await resolve_tournament(interaction, conn, tournament)
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
            tournament_row = await resolve_tournament(interaction, conn, tournament)
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
