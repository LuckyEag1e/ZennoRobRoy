"""Keeping each match's one public post in Discord up to date (match_posts says what it
should say), the Ready Up button on match calls, and finding a server's match rooms."""

from __future__ import annotations

import logging
import sqlite3
from enum import Enum, auto

import discord

import coordination
import db
import match_posts
from common import BotView, send_public, turn_away_if_banned
from embeds import mention

log = logging.getLogger("winningticket")

def room_mention(room_id: int) -> str:
    return f"<#{room_id}>"


def match_rooms(client: discord.Client, guild_id: int, conn: sqlite3.Connection) -> list[int]:
    """The voice channels in the server's match-room category, in the order Discord lists them."""
    category_id = db.get_match_room_category(conn, guild_id)
    category = client.get_channel(category_id) if category_id is not None else None
    if category is None:
        return []
    return [room.id for room in category.voice_channels]


MATCH_SLOTS_HELP = "Have the bot call this many matches at once into voice rooms (leave blank to run matches yourself)"


def match_slots_problem(client: discord.Client, guild_id: int, conn: sqlite3.Connection, slots: int) -> str | None:
    """Why a tournament can't have the bot call `slots` matches at once, or None if it can."""
    rooms = match_rooms(client, guild_id, conn)
    if not rooms:
        return "Set up the match rooms first with `/admin matchrooms`, so called matches have somewhere to be played."
    if slots > len(rooms):
        if len(rooms) == 1:
            return "There's only 1 match room, so only 1 match can be called at once."
        return f"There are only {len(rooms)} match rooms, so at most {len(rooms)} matches can be called at once."
    return None


class ReadyUpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ready:(?P<match_id>\d+)"):
    """Ready Up on a match's call. The call can stay up across a deploy, so the button is
    rebuilt from its custom_id after a restart (bot.add_dynamic_items)."""

    def __init__(self, match_id: int) -> None:
        super().__init__(discord.ui.Button(label="Ready Up", style=discord.ButtonStyle.success,
                                           custom_id=f"ready:{match_id}"))
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> ReadyUpButton:
        return cls(int(match["match_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await turn_away_if_banned(interaction)

    async def callback(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            rooms = match_rooms(interaction.client, interaction.guild_id, conn)
            await interaction.response.send_message(
                coordination.ready_up(conn, self.match_id, interaction.user.id, rooms), ephemeral=True)
            await sync_match_post(interaction.client, conn, self.match_id)
        finally:
            conn.close()


def _view(state: match_posts.PostState, match_id: int) -> BotView | None:
    if not state.ready_button:
        return None
    view = BotView(timeout=None)
    view.add_item(ReadyUpButton(match_id))
    return view


class PostEdit(Enum):
    EDITED = auto()
    GONE = auto()  # deleted in Discord, so the post is marked unsent
    NOT_NOW = auto()  # Discord couldn't be reached, so the next check tries again


async def sync_match_post(client: discord.Client, conn: sqlite3.Connection, match_id: int) -> None:
    """Bring a match's post up to date: send it if it hasn't been yet, send it afresh when
    someone needs notifying (the new one takes over and the old one is deleted), or else
    edit it. It's forgotten once it shows how the match ended."""
    post = db.get_match_post(conn, match_id)
    if post is None:
        return
    state = match_posts.describe(conn, post, mention, room_mention)
    if post["message_id"] is None or (state.ping is not None and state.ping != post["pinged"]):
        if post["message_id"] is None and state.final:
            db.delete_match_post(conn, match_id)  # it never got as far as being sent
            return
        if not db.claim_post_send(conn, match_id):
            return  # something else is sending it right now
        message = await send_public(client.get_channel(post["channel_id"]), state.text, view=_view(state, match_id))
        if message is None:
            db.release_post_send(conn, match_id)  # tried again on the next check
            return
        if post["message_id"] is not None and not await _delete_message(client, post["channel_id"], post["message_id"]):
            db.keep_replaced_match_post(conn, post["channel_id"], post["message_id"])
        db.set_match_post(conn, match_id, message.id, state.text, state.ping or post["pinged"])
    elif state.text != post["shown"]:
        if await _edit(client, conn, post, state.text, _view(state, match_id)) is not PostEdit.EDITED:
            return
        db.set_match_post(conn, match_id, post["message_id"], state.text, post["pinged"])
    if state.final:
        db.delete_match_post(conn, match_id)


async def announce_match_change(client: discord.Client, channel: discord.abc.Messageable | None, match_id: int,
                                text: str) -> bool:
    """Announce how a match changed or ended. When the match has a post, that post becomes
    the announcement. Otherwise, or if the post can't be edited right now, `text` is
    posted in `channel`, and the old post is deleted once it can be. Nobody `text`
    mentions is notified. False if it couldn't be posted."""
    conn = db.get_connection()
    try:
        post = db.get_match_post(conn, match_id)
        edit = PostEdit.GONE
        if post is not None and post["message_id"] is not None:
            edit = await _edit(client, conn, post, text, None)
        if edit is PostEdit.EDITED:
            db.delete_match_post(conn, match_id)
            return True
        if await send_public(channel, text, ping=False) is None:
            return False  # a post that's still there is left for the background check to bring up to date
        if edit is PostEdit.NOT_NOW:
            db.replace_match_post(conn, match_id)
        else:
            db.delete_match_post(conn, match_id)
        return True
    finally:
        conn.close()


async def delete_replaced_post(client: discord.Client, conn: sqlite3.Connection, replaced: sqlite3.Row) -> None:
    """Delete a match post a new message took the place of, if Discord can be reached."""
    if await _delete_message(client, replaced["channel_id"], replaced["message_id"]):
        db.forget_replaced_match_post(conn, replaced["message_id"])


async def _edit(client: discord.Client, conn: sqlite3.Connection, post: sqlite3.Row, text: str,
                view: discord.ui.View | None) -> PostEdit:
    """Change what a sent post says. If someone deleted it, it's marked unsent, so the
    next update sends it again."""
    channel = client.get_channel(post["channel_id"])
    if channel is None:
        log.warning("Can't find channel %s to update a match post", post["channel_id"])
        return PostEdit.NOT_NOW
    try:
        await channel.get_partial_message(post["message_id"]).edit(content=text, view=view)
    except discord.NotFound:
        db.mark_post_unsent(conn, post["match_id"], post["message_id"])
        return PostEdit.GONE
    except discord.HTTPException:
        log.exception("Couldn't update the post for match %s", post["match_id"])
        return PostEdit.NOT_NOW
    return PostEdit.EDITED


async def _delete_message(client: discord.Client, channel_id: int, message_id: int) -> bool:
    """Delete a message, or one someone already deleted. False if Discord couldn't be reached."""
    channel = client.get_channel(channel_id)
    if channel is None:
        return False
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        log.exception("Couldn't delete message %s", message_id)
        return False
    return True
