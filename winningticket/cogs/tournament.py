from __future__ import annotations

import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from common import (
    load_bracket_embed,
    requires_manage_server,
    resolve_emoji,
    resolve_tournament,
    tournament_name_autocomplete,
)
from csv_import import CsvValidationError, import_tournament, parse_csv
from embeds import render_final_results_embed, render_leaderboard_embed


# A live post further up the channel than this many messages has scrolled out of view,
# so asking for it posts a fresh one instead of pointing at it.
LIVE_POST_IN_VIEW = 5

log = logging.getLogger("winningticket")


def render_live_post(conn: sqlite3.Connection, tournament: sqlite3.Row, kind: str) -> discord.Embed:
    """A tournament's bracket or leaderboard as it stands now."""
    if kind == "bracket":
        return load_bracket_embed(conn, tournament)
    return render_leaderboard_embed(tournament["name"], db.load_scores(conn, tournament["id"]))


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # What each live post's message last showed, so the refresh loop only edits ones that
        # changed. It's by message: a new post can replace the live one mid-refresh, and an
        # edit to the old message mustn't mark the new one as current.
        self.last_shown: dict[int, dict] = {}

    group = app_commands.Group(
        name="tournament", description="Manage bracket prediction tournaments", guild_only=True
    )

    async def cog_load(self) -> None:
        self.refresh_live_posts.start()

    async def cog_unload(self) -> None:
        self.refresh_live_posts.cancel()

    # Every change to a tournament shows up in its live posts within a few seconds, whatever
    # caused it, so nothing that changes results, names or status needs to remember to refresh them.
    @tasks.loop(seconds=5)
    async def refresh_live_posts(self) -> None:
        try:
            conn = db.get_connection()
            try:
                for live in db.get_live_posts(conn):
                    try:
                        await self._refresh(conn, live)
                    except Exception:
                        log.exception("Couldn't refresh the %s post for tournament %s", live["kind"], live["tournament_id"])
            finally:
                conn.close()
        except Exception:
            log.exception("Couldn't refresh live posts")

    @refresh_live_posts.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh(self, conn: sqlite3.Connection, live: sqlite3.Row) -> None:
        tournament = db.get_tournament(conn, live["tournament_id"])
        message_id = live["message_id"]
        shown = render_live_post(conn, tournament, live["kind"])
        channel = self.bot.get_channel(live["channel_id"])
        if self.last_shown.get(message_id) != shown.to_dict() and channel is not None:
            try:
                await channel.get_partial_message(message_id).edit(embed=shown)
                self.last_shown[message_id] = shown.to_dict()
            except discord.NotFound:
                # Deleted by someone; the next request posts a new one.
                self._forget(conn, live)
                return
        if tournament["status"] == "concluded":
            # Its final state is shown; nothing will change it again.
            self._forget(conn, live)

    def _forget(self, conn: sqlite3.Connection, live: sqlite3.Row) -> None:
        # Only if it's still the live post: a newer one may have replaced it meanwhile.
        current = db.get_live_post(conn, live["tournament_id"], live["kind"])
        if current is not None and current["message_id"] == live["message_id"]:
            db.delete_live_post(conn, live["tournament_id"], live["kind"])
        self.last_shown.pop(live["message_id"], None)

    async def _show_live_post(self, interaction: discord.Interaction, conn: sqlite3.Connection,
                              tournament: sqlite3.Row, kind: str) -> None:
        """Point the member at the tournament's live bracket or leaderboard if it's still in
        view in this channel, privately, so the channel doesn't fill up with copies.
        Otherwise post a fresh one, which becomes the live post."""
        live = db.get_live_post(conn, tournament["id"], kind)
        if live is not None and await self._in_view(interaction.channel, live):
            url = _jump_url(interaction.guild_id, live["channel_id"], live["message_id"])
            await interaction.response.send_message(
                f"The {kind} is just above, and it's kept up to date: {url}", ephemeral=True
            )
            return
        embed = render_live_post(conn, tournament, kind)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        db.set_live_post(conn, tournament["id"], kind, interaction.channel_id, message.id)
        self.last_shown[message.id] = embed.to_dict()

    @staticmethod
    async def _in_view(channel, live: sqlite3.Row) -> bool:
        if channel is None or channel.id != live["channel_id"]:
            return False
        try:
            async for message in channel.history(limit=LIVE_POST_IN_VIEW):
                if message.id == live["message_id"]:
                    return True
        except discord.HTTPException:
            pass  # e.g. no permission to read history; a fresh post is the safe answer
        return False

    @staticmethod
    async def _resolve_for_next_status(interaction: discord.Interaction, conn: sqlite3.Connection,
                                       name: str, new_status: str) -> sqlite3.Row | None:
        """Fetch a tournament that's at the step just before `new_status`, or reply with an
        error and return None."""
        tournament = await resolve_tournament(interaction, conn, name)
        if tournament is None:
            return None
        if not db.is_next_status(tournament["status"], new_status):
            await interaction.response.send_message(
                f"**{name}** is `{tournament['status']}`, so it can't move to `{new_status}` from here. "
                f"Use `/admin setstatus` to force a status.",
                ephemeral=True,
            )
            return None
        return tournament

    async def _advance_status(self, interaction: discord.Interaction, name: str, new_status: str) -> bool:
        """Move a tournament one step along its lifecycle. Replies with an error and
        returns False if it isn't at the step just before `new_status`."""
        conn = db.get_connection()
        try:
            tournament = await self._resolve_for_next_status(interaction, conn, name, new_status)
            if tournament is None:
                return False
            db.set_tournament_status(conn, tournament["id"], new_status)
            return True
        finally:
            conn.close()

    @group.command(name="create", description="Create a tournament from a bracket CSV")
    @app_commands.describe(name="Tournament name", csv_file="CSV with team_a,team_b[,seed_a,seed_b] columns")
    @requires_manage_server
    async def create(self, interaction: discord.Interaction, name: str, csv_file: discord.Attachment) -> None:
        conn = db.get_connection()
        try:
            if db.get_tournament_by_name(conn, interaction.guild_id, name) is not None:
                await interaction.response.send_message(
                    f"A tournament named '{name}' already exists in this server.", ephemeral=True
                )
                return

            raw = await csv_file.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                await interaction.response.send_message("CSV file must be UTF-8 encoded text.", ephemeral=True)
                return

            try:
                rows = parse_csv(content)
                import_tournament(conn, interaction.guild_id, name, interaction.user.id, rows)
            except CsvValidationError as e:
                await interaction.response.send_message(f"CSV error: {e}", ephemeral=True)
                return

            # Announcements are public, so the organizer's next step goes to them privately.
            await interaction.response.send_message(f"Created tournament **{name}** with {len(rows)} round-1 matchups.")
            await interaction.followup.send(
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin.", ephemeral=True
            )
        finally:
            conn.close()

    @group.command(name="open", description="Open a tournament for predictions")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def open_predictions(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._advance_status(interaction, name, "predictions_open"):
            return
        emoji = resolve_emoji(self.bot, "winningticket")
        await interaction.response.send_message(
            f"{emoji} Predictions are now open for **{name}**! Run `/predict tournament:{name}` to fill out your bracket."
        )

    @group.command(name="lock", description="Lock predictions and start recording real results")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def lock(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._advance_status(interaction, name, "in_progress"):
            return
        await interaction.response.send_message(f"🔒 Predictions are locked for **{name}**.")
        await interaction.followup.send("Use `/match result` to record real outcomes.", ephemeral=True)

    @group.command(name="conclude", description="Archive a finished tournament, keeping its results")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    @requires_manage_server
    async def conclude(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await self._resolve_for_next_status(interaction, conn, name, "concluded")
            if tournament is None:
                return
            result, teams_by_id, scores = db.load_final_results(conn, tournament["id"])
            if result.champion is None:
                await interaction.response.send_message(
                    f"**{name}**'s final has no result yet, so it can't be concluded. "
                    f"Record it with `/match result` first.",
                    ephemeral=True,
                )
                return
            # Post before archiving: once concluded, nothing can find the tournament to post again.
            await interaction.response.send_message(embed=render_final_results_embed(name, result, teams_by_id, scores))
            db.set_tournament_status(conn, tournament["id"], "concluded")
        finally:
            conn.close()
        await interaction.followup.send(
            f"**{name}** is concluded. Its results are saved, and the name is free for a new tournament.",
            ephemeral=True,
        )

    @group.command(name="bracket", description="Show the current bracket")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def bracket(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return
            await self._show_live_post(interaction, conn, tournament, "bracket")
        finally:
            conn.close()

    @group.command(name="leaderboard", description="Show the current prediction leaderboard")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def leaderboard(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name)
            if tournament is None:
                return
            await self._show_live_post(interaction, conn, tournament, "leaderboard")
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
