"""Shared helpers used by multiple cogs: tournament lookup, autocomplete, match pickers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import discord
from discord import app_commands

import db
from bracket import Match
from labels import matchup_label

# Discord's limit on options in one select menu.
MAX_SELECT_OPTIONS = 25


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
    interaction: discord.Interaction, conn: sqlite3.Connection, name: str
) -> sqlite3.Row | None:
    """Fetch a tournament by name for this guild, or send an ephemeral error and return None."""
    tournament = db.get_tournament_by_name(conn, interaction.guild_id, name)
    if tournament is None:
        await interaction.response.send_message(
            f"No tournament named '{name}' found in this server.", ephemeral=True
        )
        return None
    return tournament


async def tournament_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    conn = db.get_connection()
    try:
        names = db.get_tournament_names(conn, interaction.guild_id)
    finally:
        conn.close()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:MAX_SELECT_OPTIONS]


def match_select_options(
    matches: list[Match], slots: dict, teams_by_id: dict, results_by_match: dict[int, int] | None = None
) -> list[discord.SelectOption]:
    """Select-menu options for picking a match. Pass results_by_match to tag matches already decided."""
    options = []
    for match in matches[:MAX_SELECT_OPTIONS]:
        label = matchup_label(match, slots, teams_by_id)
        if results_by_match is not None and match.id in results_by_match:
            label += " (has result)"
        options.append(discord.SelectOption(label=label[:100], value=str(match.id)))
    return options


def resolve_emoji(client: discord.Client, name: str) -> str:
    """Look up a custom server emoji by name and return its mention string.
    Falls back to `:name:` as plain text if the bot can't see an emoji with
    that name (e.g. not in any mutual server), so it never crashes."""
    emoji = discord.utils.get(client.emojis, name=name)
    return str(emoji) if emoji is not None else f":{name}:"
