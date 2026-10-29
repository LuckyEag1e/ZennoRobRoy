"""What the simulation checks after every step. Each check returns a list of problems as
plain strings; the harness records them against the step that caused them."""

from __future__ import annotations

import re
import sqlite3

import discord

import db
from bracket import final_match, stale_results
from sim.discord_fakes import FakeMessage

# Discord's limits, past which it rejects a message outright.
CONTENT_LIMIT = 2000
EMBED_TOTAL_LIMIT = 6000
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELDS_LIMIT = 25
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
BUTTON_LABEL_LIMIT = 80
SELECT_OPTIONS_LIMIT = 25
SELECT_LABEL_LIMIT = 100


def message_problems(message: FakeMessage) -> list[str]:
    problems = []
    if message.deleted:
        return problems
    if not message.content and not message.embeds and message.view is None:
        problems.append("empty message (Discord rejects these)")
    if message.content and len(message.content) > CONTENT_LIMIT:
        problems.append(f"content is {len(message.content)} characters (limit {CONTENT_LIMIT})")
    for embed in message.embeds:
        problems += _embed_problems(embed)
    if message.view is not None:
        problems += _view_problems(message.view)
    return problems


def _embed_problems(embed: discord.Embed) -> list[str]:
    problems = []
    if len(embed) > EMBED_TOTAL_LIMIT:
        problems.append(f"embed totals {len(embed)} characters (limit {EMBED_TOTAL_LIMIT})")
    if embed.title and len(embed.title) > EMBED_TITLE_LIMIT:
        problems.append(f"embed title is {len(embed.title)} characters")
    if embed.description and len(embed.description) > EMBED_DESCRIPTION_LIMIT:
        problems.append(f"embed description is {len(embed.description)} characters")
    if len(embed.fields) > EMBED_FIELDS_LIMIT:
        problems.append(f"embed has {len(embed.fields)} fields")
    for embed_field in embed.fields:
        if not embed_field.name or not str(embed_field.name).strip():
            problems.append("embed field with an empty name")
        elif len(embed_field.name) > FIELD_NAME_LIMIT:
            problems.append(f"embed field name is {len(embed_field.name)} characters")
        if not embed_field.value or not str(embed_field.value).strip():
            problems.append(f"embed field '{embed_field.name}' has an empty value")
        elif len(embed_field.value) > FIELD_VALUE_LIMIT:
            problems.append(f"embed field '{embed_field.name}' value is {len(embed_field.value)} characters")
    return problems


def _view_problems(view: discord.ui.View) -> list[str]:
    problems = []
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label and len(item.label) > BUTTON_LABEL_LIMIT:
            problems.append(f"button label is {len(item.label)} characters")
        if isinstance(item, discord.ui.Select):
            if not item.options:
                problems.append("select menu with no options")
            if len(item.options) > SELECT_OPTIONS_LIMIT:
                problems.append(f"select menu has {len(item.options)} options")
            for option in item.options:
                if len(option.label) > SELECT_LABEL_LIMIT:
                    problems.append(f"select option label is {len(option.label)} characters")
    return problems


def organizer_commands_named(message: FakeMessage, organizer_commands: set[str]) -> list[str]:
    """Organizer commands a public message mentions. Public posts shouldn't point
    players at commands they can't use."""
    if not message.public or message.deleted:
        return []
    text = message.text()
    return [
        f"public message names organizer command /{name}"
        for name in sorted(organizer_commands)
        if re.search(rf"/{re.escape(name)}(?![\w-])", text)
    ]


def database_problems(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    problems = []
    for tournament in conn.execute("SELECT * FROM tournaments WHERE guild_id = ?", (guild_id,)):
        name, status = tournament["name"], tournament["status"]
        matches_by_id, feeders, _ = db.load_bracket(conn, tournament["id"])
        if status == "registration":
            if matches_by_id:
                problems.append(f"'{name}' is still in registration but has {len(matches_by_id)} matches")
            continue
        if not matches_by_id:
            problems.append(f"'{name}' is {status} but has no bracket")
            continue
        finals = [m for m in matches_by_id.values() if m.feeds_into_match_id is None]
        if len(finals) != 1:
            problems.append(f"'{name}' has {len(finals)} matches with nothing after them (expected one final)")
            continue
        results = db.get_results(conn, tournament["id"])
        stale = stale_results(matches_by_id, feeders, results)
        if stale:
            problems.append(f"'{name}' has results whose winner isn't in that match: {stale}")
        final_decided = final_match(matches_by_id).id in results
        if status in ("setup", "predictions_open") and results:
            problems.append(f"'{name}' is {status} but already has {len(results)} results")
        if status in ("completed", "concluded") and not final_decided:
            problems.append(f"'{name}' is {status} but its final has no result")
        if status == "in_progress" and final_decided:
            problems.append(f"'{name}' has a decided final but is still in_progress")
    for row in conn.execute("SELECT id FROM feedback WHERE trim(message) = ''"):
        problems.append(f"blank feedback #{row['id']} was stored")
    return problems
