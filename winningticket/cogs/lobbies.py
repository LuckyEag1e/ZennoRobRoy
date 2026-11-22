from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import lobby_db
import lobby_flow
from common import requires_manage_server, resolve_tournament, tournament_name_autocomplete, with_custom_emojis
from lobby_posting import (LobbyButton, end_checkin, is_organizer, open_points, refresh_signups, start_checkin,
                           sync_posts)

log = logging.getLogger("winningticket")

LOBBY_OPTION_HELP = "Lobby number (needed if you aren't playing in one, such as an organizer)"


async def _find_lobby(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row,
                      number: Optional[int]) -> Optional[lobby_db.Lobby]:
    """The lobby numbered `number`, or else the one the member plays in, or None after
    saying why there isn't one."""
    drawn = lobby_db.get_lobbies(conn, tournament["id"])
    if number is not None:
        lobby = next((lobby for lobby in drawn if lobby.number == number), None)
        if lobby is None:
            await interaction.response.send_message(f"**{tournament['name']}** has no lobby {number}.", ephemeral=True)
        return lobby
    team_id = db.get_entrant_team_id(conn, tournament["id"], interaction.user.id)
    lobby = next((lobby for lobby in drawn if team_id is not None and team_id in lobby.players), None)
    if lobby is None:
        await interaction.response.send_message(
            f"You aren't playing in a lobby in **{tournament['name']}**. Give the lobby's number.", ephemeral=True)
    return lobby


async def _resolve_event(interaction: discord.Interaction, conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return await resolve_tournament(interaction, conn, name, needs_bracket=False, formats=("lobbies",))


class LobbiesCog(commands.Cog):
    """Slash commands for 5v5 events. Most players never need them, since the buttons on
    the event's posts do the same things."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="lobby", description="Play in a 5v5 event's lobbies", guild_only=True)

    async def cog_load(self) -> None:
        self.tend_lobbies.start()

    async def cog_unload(self) -> None:
        self.tend_lobbies.cancel()

    # Often enough that check-in ends within seconds of the time players were shown.
    @tasks.loop(seconds=5)
    async def tend_lobbies(self) -> None:
        # An exception escaping a tasks.loop stops it until the bot restarts, so every
        # error is logged here, and one event failing doesn't hold up the others.
        try:
            conn = db.get_connection()
            try:
                for tournament_id in lobby_flow.tend(conn):
                    await refresh_signups(self.bot, conn, tournament_id)  # late sign-ups have ended
                for tournament in lobby_db.get_tournaments_with_posts(conn):
                    await sync_posts(self.bot, conn, tournament["id"])
            finally:
                conn.close()
        except Exception:
            log.exception("Couldn't tend the 5v5 events")

    @tend_lobbies.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()
        # A sign-ups post is only rewritten when someone joins or leaves, so one posted by
        # an older version of the bot would keep its old wording and count until then.
        conn = db.get_connection()
        try:
            for tournament in lobby_db.get_tournaments_taking_signups(conn):
                await refresh_signups(self.bot, conn, tournament["id"])
        except Exception:
            log.exception("Couldn't refresh the 5v5 sign-ups posts")
        finally:
            conn.close()

    @app_commands.command(name="checkin", description="Check in for a 5v5 event once check-in is open")
    @app_commands.describe(tournament="Event name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @app_commands.guild_only()
    async def checkin(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            if event is None:
                return
            await interaction.response.send_message(lobby_flow.check_in(conn, event, interaction.user.id),
                                                    ephemeral=True)
            await sync_posts(self.bot, conn, event["id"])
        finally:
            conn.close()

    @group.command(name="startcheckin", description="Open check-in for a 5v5 event's lobbies and waitlist")
    @app_commands.describe(tournament="Event name", minutes="How long check-in stays open")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @requires_manage_server
    async def startcheckin(self, interaction: discord.Interaction, tournament: str,
                           minutes: app_commands.Range[int, 1, lobby_flow.MAX_CHECKIN_MINUTES] =
                           lobby_flow.DEFAULT_CHECKIN_MINUTES) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
        finally:
            conn.close()
        if event is not None:
            await start_checkin(interaction, event["id"], minutes)

    @group.command(name="cancelcheckin", description="Call off a 5v5 event's check-in, clearing everyone's check-in")
    @app_commands.describe(tournament="Event name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @requires_manage_server
    async def cancelcheckin(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            if event is None:
                return
            await interaction.response.send_message(lobby_flow.cancel_checkin(conn, event), ephemeral=True)
            await sync_posts(self.bot, conn, event["id"])
        finally:
            conn.close()

    @group.command(name="endcheckin", description="End a 5v5 event's check-in now, replacing anyone who didn't check in")
    @app_commands.describe(tournament="Event name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    @requires_manage_server
    async def endcheckin(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            if event is not None:
                await end_checkin(interaction, conn, event)
        finally:
            conn.close()

    @group.command(name="roomcode", description="Give the bot your lobby's drafter room code")
    @app_commands.describe(tournament="Event name", code="The room code from the drafter site", lobby=LOBBY_OPTION_HELP)
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def roomcode(self, interaction: discord.Interaction, tournament: str, code: str,
                       lobby: Optional[int] = None) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            found = event and await _find_lobby(interaction, conn, event, lobby)
            if not found:
                return
            role = lobby_flow.role_in(conn, found, interaction.user.id, is_organizer(interaction))
            reply = lobby_flow.submit_room_code(conn, event, found, role, interaction.user.id, code)
            await interaction.response.send_message(reply, ephemeral=True)
            await sync_posts(self.bot, conn, event["id"])
        finally:
            conn.close()

    @group.command(name="code", description="Get your lobby's room code (and, for its host, who to pass it to)")
    @app_commands.describe(tournament="Event name", lobby=LOBBY_OPTION_HELP)
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def code(self, interaction: discord.Interaction, tournament: str, lobby: Optional[int] = None) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            found = event and await _find_lobby(interaction, conn, event, lobby)
            if not found:
                return
            role = lobby_flow.role_in(conn, found, interaction.user.id, is_organizer(interaction))
            reply = lobby_flow.get_room_code(conn, event, found, role)
            await interaction.response.send_message(with_custom_emojis(self.bot, reply), ephemeral=True)
        finally:
            conn.close()

    @group.command(name="finish", description="Say your lobby's races are over (captains and organizers)")
    @app_commands.describe(tournament="Event name", lobby=LOBBY_OPTION_HELP)
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def finish(self, interaction: discord.Interaction, tournament: str, lobby: Optional[int] = None) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            found = event and await _find_lobby(interaction, conn, event, lobby)
            if not found:
                return
            role = lobby_flow.role_in(conn, found, interaction.user.id, is_organizer(interaction))
            await interaction.response.send_message(lobby_flow.finish(conn, event, found, role, interaction.user.id),
                                                    ephemeral=True)
            await sync_posts(self.bot, conn, event["id"])
        finally:
            conn.close()

    @group.command(name="report",
                   description="Enter everyone's points in your lobby (captains), or the final points (organizers)")
    @app_commands.describe(tournament="Event name", lobby=LOBBY_OPTION_HELP)
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def report(self, interaction: discord.Interaction, tournament: str, lobby: Optional[int] = None) -> None:
        conn = db.get_connection()
        try:
            event = await _resolve_event(interaction, conn, tournament)
            found = event and await _find_lobby(interaction, conn, event, lobby)
            if not found:
                return
            role = lobby_flow.role_in(conn, found, interaction.user.id, is_organizer(interaction))
            await open_points(interaction, conn, event, found, final=not role.captain)
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(LobbyButton)
    await bot.add_cog(LobbiesCog(bot))
