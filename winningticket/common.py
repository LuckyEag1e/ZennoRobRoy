"""Shared helpers used by multiple cogs: tournament lookup, autocomplete, match and winner pickers."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord
from discord import app_commands

import db
from bracket import Match, resolve_all_slots
from embeds import render_bracket_embed
from labels import matchup_label, team_label

log = logging.getLogger("winningticket")

# Discord's limit on options in one select menu.
MAX_SELECT_OPTIONS = 25

# Discord ignores default_permissions on subcommands, so admin subcommands inside a group
# that members also use (/tournament, /match, /help) check the permission when run instead.
requires_manage_server = app_commands.checks.has_permissions(manage_guild=True)


@dataclass
class TournamentSession:
    """A loaded tournament bracket carried between the steps of a button/select flow."""

    tournament_id: int
    tournament_name: str
    matches_by_id: dict[int, Match]
    feeders_by_target: dict
    teams_by_id: dict

    @classmethod
    def load(cls, conn: sqlite3.Connection, tournament: sqlite3.Row) -> TournamentSession:
        return cls(tournament["id"], tournament["name"], *db.load_bracket(conn, tournament["id"]))


async def resolve_tournament(
    interaction: discord.Interaction, conn: sqlite3.Connection, name: str, needs_bracket: bool = True
) -> sqlite3.Row | None:
    """Fetch a tournament by name for this guild, or send an ephemeral error and return None.
    A tournament still taking sign-ups has no bracket yet, so it's an error too unless the
    command passes needs_bracket=False because it works without one."""
    tournament = db.get_tournament_by_name(conn, interaction.guild_id, name)
    if tournament is None:
        if db.has_concluded_tournament(conn, interaction.guild_id, name):
            message = f"**{name}** has concluded. Its final results were posted when it ended."
        else:
            message = f"No tournament named '{name}' found in this server."
        await interaction.response.send_message(message, ephemeral=True)
        return None
    if needs_bracket and tournament["status"] == "registration":
        message = (
            f"**{name}** is still taking sign-ups, so it has no bracket yet. "
            f"`/registration entrants name:{name}` shows who's in"
        )
        # Only organizers can close sign-ups, so only they are pointed at the command.
        message += ", and `/registration close` draws the bracket." if interaction.permissions.manage_guild else "."
        await interaction.response.send_message(message, ephemeral=True)
        return None
    return tournament


def _tournament_choices(interaction: discord.Interaction, current: str,
                        status: str | None = None) -> list[app_commands.Choice[str]]:
    conn = db.get_connection()
    try:
        names = db.get_tournament_names(conn, interaction.guild_id, status)
    finally:
        conn.close()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:MAX_SELECT_OPTIONS]


async def tournament_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return _tournament_choices(interaction, current)


async def registration_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Only tournaments still taking sign-ups."""
    return _tournament_choices(interaction, current, "registration")


async def team_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Teams in the tournament picked in the same command's `tournament` option."""
    tournament_name = getattr(interaction.namespace, "tournament", None)
    if not tournament_name:
        return []
    conn = db.get_connection()
    try:
        tournament_row = db.get_tournament_by_name(conn, interaction.guild_id, tournament_name)
        if tournament_row is None:
            return []
        teams_by_id = db.get_teams(conn, tournament_row["id"])
    finally:
        conn.close()
    return [
        app_commands.Choice(name=team["name"], value=team["name"])
        for team in teams_by_id.values()
        if current.lower() in team["name"].lower()
    ][:MAX_SELECT_OPTIONS]


def signups_message(tournament: sqlite3.Row) -> str:
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


async def edit_signups_message(client: discord.Client, tournament: sqlite3.Row, content: str) -> None:
    """Rewrite a tournament's sign-ups announcement, if it has one the bot can still find."""
    channel = client.get_channel(tournament["channel_id"]) if tournament["channel_id"] is not None else None
    if channel is None or tournament["announcement_message_id"] is None:
        return
    try:
        await channel.get_partial_message(tournament["announcement_message_id"]).edit(content=content)
    except discord.HTTPException:
        log.warning("Couldn't update the sign-ups message for tournament %s", tournament["id"])


def load_bracket_embed(conn: sqlite3.Connection, tournament: sqlite3.Row) -> discord.Embed:
    """The tournament's real bracket as it stands, with every result recorded so far."""
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    results_by_match = db.get_results(conn, tournament["id"])
    slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
    return render_bracket_embed(
        tournament["name"], tournament["status"], matches_by_id, feeders, slots, results_by_match, teams_by_id
    )


async def turn_away_if_banned(interaction: discord.Interaction) -> bool:
    """True if an admin has banned this member, after telling them so where Discord
    allows a reply (it doesn't for autocomplete)."""
    if interaction.guild_id is None:
        return False
    conn = db.get_connection()
    try:
        banned = db.is_banned(conn, interaction.guild_id, interaction.user.id)
    finally:
        conn.close()
    if banned and interaction.type != discord.InteractionType.autocomplete:
        await interaction.response.send_message(
            "An admin has blocked you from using WinningTicket in this server.", ephemeral=True
        )
    return banned


class BotView(discord.ui.View):
    """Base for every view the bot sends, so a member banned while one is open can't keep using it."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await turn_away_if_banned(interaction)


class BotModal(discord.ui.Modal):
    """Base for every form the bot opens, so a member banned while one is open can't send it."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await turn_away_if_banned(interaction)


MatchCallback = Callable[[discord.Interaction, Match], Awaitable[None]]
WinnerCallback = Callable[[discord.Interaction, int, int], Awaitable[None]]


class MatchPicker(BotView):
    """A select menu of matches that pages through them, since Discord caps a menu at 25
    options and a large bracket has more matches than that."""

    def __init__(self, matches: list[Match], slots: dict, teams_by_id: dict, on_pick: MatchCallback,
                 results_by_match: dict[int, int] | None = None, timeout: int = 300) -> None:
        super().__init__(timeout=timeout)
        self.matches = matches
        self.slots = slots
        self.teams_by_id = teams_by_id
        self.on_pick = on_pick
        self.results_by_match = results_by_match or {}
        self.page = 0
        self.page_count = (len(matches) + MAX_SELECT_OPTIONS - 1) // MAX_SELECT_OPTIONS
        self._show_page()

    def _show_page(self) -> None:
        self.clear_items()
        start = self.page * MAX_SELECT_OPTIONS
        options = []
        for match in self.matches[start:start + MAX_SELECT_OPTIONS]:
            label = matchup_label(match, self.slots, self.teams_by_id)
            if match.id in self.results_by_match:
                label += " (has result)"
            options.append(discord.SelectOption(label=label[:100], value=str(match.id)))
        placeholder = "Select a match..."
        if self.page_count > 1:
            placeholder = f"Select a match (page {self.page + 1} of {self.page_count})..."
        select = discord.ui.Select(placeholder=placeholder, options=options)
        select.callback = self._picked
        self.add_item(select)

        if self.page_count > 1:
            for label, step in (("Previous", -1), ("Next", 1)):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                           disabled=not 0 <= self.page + step < self.page_count)
                button.callback = self._turn_page(step)
                self.add_item(button)

    def _turn_page(self, step: int):
        async def turn(interaction: discord.Interaction) -> None:
            self.page += step
            self._show_page()
            await interaction.response.edit_message(view=self)
        return turn

    async def _picked(self, interaction: discord.Interaction) -> None:
        selected_id = int(interaction.data["values"][0])
        match = next(m for m in self.matches if m.id == selected_id)
        await self.on_pick(interaction, match)


class WinnerPicker(BotView):
    """One button per team in a match; calls on_pick(interaction, winner_id, loser_id)."""

    def __init__(self, team_a: int, team_b: int, teams_by_id: dict, on_pick: WinnerCallback,
                 timeout: int = 300) -> None:
        super().__init__(timeout=timeout)
        for winner, loser in ((team_a, team_b), (team_b, team_a)):
            button = discord.ui.Button(label=team_label(teams_by_id, winner)[:80], style=discord.ButtonStyle.success)
            button.callback = self._make_callback(on_pick, winner, loser)
            self.add_item(button)

    @staticmethod
    def _make_callback(on_pick: WinnerCallback, winner: int, loser: int):
        async def pick(interaction: discord.Interaction) -> None:
            await on_pick(interaction, winner, loser)
        return pick


def resolve_emoji(client: discord.Client, name: str) -> str:
    """Look up a custom server emoji by name and return its mention string.
    Falls back to `:name:` as plain text if the bot can't see an emoji with
    that name (e.g. not in any mutual server), so it never crashes."""
    emoji = discord.utils.get(client.emojis, name=name)
    return str(emoji) if emoji is not None else f":{name}:"
