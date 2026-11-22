"""The Discord side of 5v5 events: the buttons on their public posts, the private menus and
forms those buttons open, and keeping the posts up to date (lobby_posts says what each
should show). lobby_flow holds the rules and replies, shared with the CLI."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord

import db
import lobby_db
import lobby_flow
import lobby_posts
from common import (BotModal, BotView, edit_signups_message, send_public, signups_message, turn_away_if_banned,
                    with_custom_emojis)
from embeds import mention
from labels import CONSENT_NOTICE
from posting import delete_message

log = logging.getLogger("winningticket")

# Discord's limit on a form field's label.
FIELD_LABEL_LIMIT = 45

STYLES = {
    "register": discord.ButtonStyle.success, "unregister": discord.ButtonStyle.secondary,
    "startcheckin": discord.ButtonStyle.primary, "checkin": discord.ButtonStyle.success,
    "endcheckin": discord.ButtonStyle.secondary,
    "submitcode": discord.ButtonStyle.secondary, "getcode": discord.ButtonStyle.primary,
    "finish": discord.ButtonStyle.success, "pointsa": discord.ButtonStyle.primary,
    "pointsb": discord.ButtonStyle.primary, "finalize": discord.ButtonStyle.danger,
}
# The id in a button's custom_id is the tournament's, except on a results post, where it's
# the lobby's.
EVENT_ACTIONS = {"register", "unregister", "startcheckin", "checkin", "endcheckin"}
# Buttons that act on one lobby: the presser's own, or for an organizer, one they pick.
LOBBY_ACTIONS = {"submitcode", "getcode", "finish"}
RESULTS_ACTIONS = {"pointsa", "pointsb", "finalize"}
SIGNUP_BUTTONS = [lobby_posts.PostButton("Register", "register"), lobby_posts.PostButton("Unregister", "unregister")]


def is_organizer(interaction: discord.Interaction) -> bool:
    return interaction.permissions.manage_guild


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lobby:(?P<action>[a-z]+):(?P<id>\d+)"):
    """Every button on a 5v5 event's sign-ups post, event post and results posts. They're
    rebuilt from their custom_id after a restart (bot.add_dynamic_items), so posts keep
    working across deploys; the label only matters when the button is first sent."""

    def __init__(self, action: str, target_id: int, label: str = "") -> None:
        super().__init__(discord.ui.Button(label=label or action, style=STYLES.get(action, discord.ButtonStyle.secondary),
                                           custom_id=f"lobby:{action}:{target_id}"))
        self.action = action
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> LobbyButton:
        return cls(match["action"], int(match["id"]), item.label or "")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await turn_away_if_banned(interaction)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action not in HANDLERS:
            # A button from an older version, on a post that hasn't been brought up to date yet.
            await interaction.response.send_message("That button is out of date. Use the event's latest post.",
                                                    ephemeral=True)
            return
        conn = db.get_connection()
        try:
            if self.action in RESULTS_ACTIONS:
                lobby = lobby_db.get_lobby(conn, self.target_id)
                if lobby is None:
                    await interaction.response.send_message("That lobby no longer exists.", ephemeral=True)
                    return
                await HANDLERS[self.action](interaction, conn, db.get_tournament(conn, lobby.tournament_id), lobby)
                return
            tournament = db.get_tournament(conn, self.target_id)
            if tournament is None or tournament["format"] != "lobbies":
                await interaction.response.send_message("That event no longer exists.", ephemeral=True)
                return
            if self.action in EVENT_ACTIONS:
                await HANDLERS[self.action](interaction, conn, tournament, None)
                return
            await _for_a_lobby(interaction, conn, tournament, self.action)
        finally:
            conn.close()


async def _for_a_lobby(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row,
                       action: str) -> None:
    """Run a lobby button for the presser's own lobby. An organizer who isn't playing picks
    the lobby, unless only one lobby is left to act on."""
    team_id = db.get_entrant_team_id(conn, tournament["id"], interaction.user.id)
    own = lobby_db.find_lobby_of(conn, tournament["id"], team_id) if team_id is not None else None
    if own is not None:
        await HANDLERS[action](interaction, conn, tournament, own)
        return
    if not is_organizer(interaction):
        await interaction.response.send_message(f"You aren't playing in a lobby of **{tournament['name']}**.",
                                                ephemeral=True)
        return
    choices = _lobbies_still_racing(conn, tournament)
    if len(choices) == 1:
        await HANDLERS[action](interaction, conn, tournament, choices[0])
        return
    if not choices:
        await interaction.response.send_message("No lobby needs that right now.", ephemeral=True)
        return
    await interaction.response.send_message("Which lobby?", view=LobbyPicker(tournament["id"], action, choices),
                                            ephemeral=True)


def _lobbies_still_racing(conn: sqlite3.Connection, tournament: sqlite3.Row) -> list[lobby_db.Lobby]:
    """The lobbies an organizer might mean by pressing one of the event post's lobby buttons:
    those whose races aren't over yet."""
    return [lobby for lobby in lobby_db.get_lobbies(conn, tournament["id"])
            if lobby_db.get_result(conn, lobby.id) is None
            and not lobby_db.races_over(lobby, lobby_db.get_finished_sides(conn, lobby.id))]


class LobbyPicker(BotView):
    """Lets an organizer pick which lobby a button on the event post is for."""

    def __init__(self, tournament_id: int, action: str, choices: list[lobby_db.Lobby]) -> None:
        super().__init__(timeout=600)
        self.tournament_id = tournament_id
        self.action = action
        select = discord.ui.Select(placeholder="Lobby", options=[
            discord.SelectOption(label=f"Lobby {lobby.number}", value=str(lobby.id)) for lobby in choices[:25]])
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
            lobby = lobby_db.get_lobby(conn, int(interaction.data["values"][0]))
            if tournament is None or lobby is None:
                await interaction.response.send_message("That lobby no longer exists.", ephemeral=True)
                return
            await HANDLERS[self.action](interaction, conn, tournament, lobby)
        finally:
            conn.close()


def event_view(target_id: int, buttons: list[lobby_posts.PostButton]) -> BotView:
    view = BotView(timeout=None)
    for button in buttons:
        view.add_item(LobbyButton(button.action, target_id, button.label))
    return view


def _post_view(state: lobby_posts.PostState, post: sqlite3.Row) -> Optional[BotView]:
    if not state.buttons:
        return None
    key = post["post_key"]
    target_id = post["tournament_id"] if key == lobby_db.EVENT_POST else int(key.rsplit(":", 1)[1])
    return event_view(target_id, state.buttons)


# -- signing up --------------------------------------------------------------------------

class CaptainChoice(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(placeholder="Will you captain?", options=[
            discord.SelectOption(label=label, value=value) for value, label in lobby_flow.CAPTAIN_CHOICES.items()])

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.choose(interaction, interaction.data["values"][0])


class LobbySignupView(BotView):
    """A private sign-up for a 5v5 event: the consent notice when it's needed, then the
    captain question, unless the command already gave a choice. Nothing is saved until a
    captain choice that's allowed is picked."""

    def __init__(self, tournament_id: int, needs_consent: bool, pref: Optional[str] = None) -> None:
        super().__init__(timeout=600)
        self.tournament_id = tournament_id
        self.pref = pref
        self.linked = True
        if needs_consent:
            for label, linked, style in (("Agree", True, discord.ButtonStyle.success),
                                         ("Opt Out", False, discord.ButtonStyle.secondary)):
                button = discord.ui.Button(label=label, style=style)
                button.callback = self._consent_callback(linked)
                self.add_item(button)
        else:
            self.add_item(CaptainChoice())

    def _consent_callback(self, linked: bool):
        async def answer(interaction: discord.Interaction) -> None:
            self.linked = linked
            if self.pref is not None:
                await self.choose(interaction, self.pref)
                return
            self.clear_items()
            self.add_item(CaptainChoice())
            await interaction.response.edit_message(content=lobby_flow.CAPTAIN_QUESTION, view=self)
        return answer

    async def choose(self, interaction: discord.Interaction, pref: str) -> None:
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament is None:
                await interaction.response.edit_message(content="That event no longer exists.", view=None)
                return
            problem = lobby_flow.captain_problem(pref, self.linked, by_organizer=False)
            if problem is not None:
                # Ask again, keeping the menu, rather than saving a choice that can't stand.
                self.clear_items()
                self.add_item(CaptainChoice())
                await interaction.response.edit_message(content=problem, view=self)
                return
            user = interaction.user
            if self.linked:
                db.record_agreement(conn, interaction.guild_id, user.id)
            elif db.get_entrant_team_id(conn, tournament["id"], user.id) is None:
                db.opt_out(conn, interaction.guild_id, user.id)  # unlinks entries from before consent was asked
            reply = lobby_flow.sign_up(conn, tournament, user.display_name, user.id, self.linked, pref)
            await interaction.response.edit_message(content=reply, view=None)
            await refresh_signups(interaction.client, conn, tournament["id"])
        finally:
            conn.close()


async def start_signup(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row,
                       pref: Optional[str]) -> None:
    """Sign a member up for a 5v5 event from /register or the Register button: straight
    away when they've agreed before and gave a captain choice, otherwise asking first."""
    if tournament["status"] != "registration" and not lobby_flow.takes_late_signups(tournament):
        await interaction.response.send_message(f"Sign-ups for **{tournament['name']}** are closed.", ephemeral=True)
        return
    signed_up = db.get_entrant_team_id(conn, tournament["id"], interaction.user.id) is not None
    agreed = signed_up or db.has_agreed(conn, interaction.guild_id, interaction.user.id)
    if agreed and pref is not None:
        reply = lobby_flow.sign_up(conn, tournament, interaction.user.display_name, interaction.user.id, True, pref)
        await interaction.response.send_message(reply, ephemeral=True)
        await refresh_signups(interaction.client, conn, tournament["id"])
        return
    content = CONSENT_NOTICE if not agreed else lobby_flow.CAPTAIN_QUESTION
    if signed_up:
        content = f"You're already signed up for **{tournament['name']}**. {lobby_flow.CAPTAIN_QUESTION}"
    await interaction.response.send_message(content, view=LobbySignupView(tournament["id"], not agreed, pref),
                                            ephemeral=True)


async def leave_event(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row) -> None:
    """Unregister from a 5v5 event: before the draw the entry is removed, and after it the
    spot goes to the waitlist."""
    name = tournament["name"]
    team_id = db.get_entrant_team_id(conn, tournament["id"], interaction.user.id)
    if tournament["status"] != "registration" and not lobby_flow.takes_late_signups(tournament):
        reply = lobby_flow.signups_over(name, by_organizer=False)
    elif team_id is None:
        reply = (f"You aren't signed up for **{name}** under your account. If you opted out when signing up, ask an "
                 f"organizer to withdraw you.")
    elif tournament["status"] == "registration":
        db.unregister_entrant(conn, tournament["id"], interaction.user.id)
        reply = f"You withdrew from **{name}**."
    else:
        reply = lobby_flow.dropout(conn, tournament, team_id, "dropped", interaction.user.id)
    await interaction.response.send_message(reply, ephemeral=True)
    await refresh_signups(interaction.client, conn, tournament["id"])
    await sync_posts(interaction.client, conn, tournament["id"])


async def refresh_signups(client: discord.Client, conn: sqlite3.Connection, tournament_id: int) -> None:
    tournament = db.get_tournament(conn, tournament_id)
    await edit_signups_message(client, tournament, signups_message(tournament, db.entrant_count(conn, tournament_id)),
                               remove_buttons=not (tournament["status"] == "registration"
                                                   or lobby_flow.takes_late_signups(tournament)))


# -- button handlers ------------------------------------------------------------------

async def _register(interaction, conn, tournament, lobby) -> None:
    await start_signup(interaction, conn, tournament, None)


async def _unregister(interaction, conn, tournament, lobby) -> None:
    await leave_event(interaction, conn, tournament)


class CheckInLengthForm(BotModal, title="Start Check-In"):
    minutes = discord.ui.TextInput(label="Minutes Check-In Stays Open", default=str(lobby_flow.DEFAULT_CHECKIN_MINUTES),
                                   max_length=3)

    def __init__(self, tournament_id: int) -> None:
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.minutes.value.strip()
        if not text.isdigit() or not 1 <= int(text) <= lobby_flow.MAX_CHECKIN_MINUTES:
            await interaction.response.send_message(
                f"Enter a whole number of minutes from 1 to {lobby_flow.MAX_CHECKIN_MINUTES}.", ephemeral=True)
            return
        await start_checkin(interaction, self.tournament_id, int(text))


async def answer_after_posts(interaction: discord.Interaction, conn: sqlite3.Connection, tournament_id: int,
                             reply: Optional[str]) -> None:
    """Bring the event's posts up to date, then answer privately. A post sent afresh to
    notify someone lands below anything already sent, so answering first would leave the
    reply above the post it's about. No `reply` just acknowledges the press."""
    await interaction.response.defer(ephemeral=True)
    await sync_posts(interaction.client, conn, tournament_id)
    if reply is not None:
        await interaction.followup.send(reply, ephemeral=True)


async def start_checkin(interaction: discord.Interaction, tournament_id: int, minutes: int) -> None:
    conn = db.get_connection()
    try:
        reply = lobby_flow.start_checkin(conn, db.get_tournament(conn, tournament_id), minutes,
                                         datetime.now(timezone.utc))
        await answer_after_posts(interaction, conn, tournament_id, reply)
    finally:
        conn.close()


async def _start_checkin(interaction, conn, tournament, lobby) -> None:
    if not is_organizer(interaction):
        await interaction.response.send_message("Only organizers can start check-in.", ephemeral=True)
        return
    problem = lobby_flow.checkin_start_problem(conn, tournament)
    if problem is not None:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.send_modal(CheckInLengthForm(tournament["id"]))


async def end_checkin(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row) -> None:
    reply = lobby_flow.end_checkin(conn, tournament)
    await refresh_signups(interaction.client, conn, tournament["id"])  # late sign-ups end with check-in
    await answer_after_posts(interaction, conn, tournament["id"], reply)


async def _end_checkin(interaction, conn, tournament, lobby) -> None:
    if not is_organizer(interaction):
        await interaction.response.send_message("Only organizers can end check-in.", ephemeral=True)
        return
    await end_checkin(interaction, conn, tournament)


async def _check_in(interaction, conn, tournament, lobby) -> None:
    await interaction.response.send_message(lobby_flow.check_in(conn, tournament, interaction.user.id), ephemeral=True)
    await sync_posts(interaction.client, conn, tournament["id"])


class RoomCodeForm(BotModal, title="Submit Room Code"):
    code = discord.ui.TextInput(label="Room Code", max_length=lobby_flow.ROOM_CODE_MAX_LENGTH)

    def __init__(self, lobby_id: int) -> None:
        super().__init__()
        self.lobby_id = lobby_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            # Re-read, since the lobby may have changed while the form was open.
            lobby = lobby_db.get_lobby(conn, self.lobby_id)
            tournament = db.get_tournament(conn, lobby.tournament_id)
            role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
            reply = lobby_flow.submit_room_code(conn, tournament, lobby, role, interaction.user.id, self.code.value)
            await answer_after_posts(interaction, conn, tournament["id"], reply)
        finally:
            conn.close()


async def _submit_code(interaction, conn, tournament, lobby) -> None:
    role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
    problem = lobby_flow.room_code_problem(conn, tournament, lobby, role)
    if problem is not None:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.send_modal(RoomCodeForm(lobby.id))


async def _get_code(interaction, conn, tournament, lobby) -> None:
    role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
    reply = lobby_flow.get_room_code(conn, tournament, lobby, role)
    await interaction.response.send_message(with_custom_emojis(interaction.client, reply), ephemeral=True)


class TeamPointsForm(BotModal):
    """One team's points: a field per player, labelled with their name."""

    def __init__(self, flow: PointsEntry, side: str) -> None:
        super().__init__(title=lobby_posts.team_points_label(flow.team_names[side])[:FIELD_LABEL_LIMIT])
        self.flow = flow
        self.side = side
        self.fields: dict[int, discord.ui.TextInput] = {}
        for player in flow.lobby.teams[side].players:
            default = flow.defaults.get(player)
            field = discord.ui.TextInput(label=flow.player_names[player][:FIELD_LABEL_LIMIT], max_length=3,
                                         placeholder="Points, including the tiebreaker if played",
                                         default=str(default) if default is not None else None)
            self.fields[player] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.flow.entered.update({player: field.value for player, field in self.fields.items()})
        following = self.flow.team_after(self.side)
        if following is not None:
            await interaction.response.send_message(
                f"Got {self.flow.team_names[self.side]}'s points. Now enter {self.flow.team_names[following]}'s.",
                view=self.flow.next_step_view(following), ephemeral=True)
            return
        await self.flow.submit(interaction)


class PointsEntry:
    """Entering points, a form per team in `teams`, since a form holds at most five fields.
    The results post has a button per team, so a captain fills in one at a time there. An
    organizer's final points, or a captain's by command, take both forms, and since Discord
    won't open a form straight from another, a button sits between them. `side` is the
    captain's whose entry this is, or None for an organizer's final points."""

    def __init__(self, conn: sqlite3.Connection, lobby: lobby_db.Lobby, side: Optional[str],
                 teams: tuple[str, ...]) -> None:
        self.lobby = lobby
        self.side = side
        self.teams = teams
        names = lobby_posts.Names(conn, lobby.tournament_id, str)
        self.team_names = {s: lobby_posts.team_name(lobby, s, names) for s in lobby_db.SIDES}
        self.player_names = {player: names.plain(player) for player in lobby.players}
        self.defaults = lobby_flow.points_defaults(conn, lobby, side)
        self.entered: dict[int, str] = {}

    def team_after(self, side: str) -> Optional[str]:
        following = self.teams[self.teams.index(side) + 1:]
        return following[0] if following else None

    def next_step_view(self, side: str) -> BotView:
        view = BotView(timeout=600)
        button = discord.ui.Button(label=f"Enter {self.team_names[side]} Points"[:80], style=discord.ButtonStyle.primary)

        async def open_next(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(TeamPointsForm(self, side))

        button.callback = open_next
        view.add_item(button)
        return view

    async def submit(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            lobby = lobby_db.get_lobby(conn, self.lobby.id)
            if lobby is None or lobby.players != self.lobby.players:
                await interaction.response.send_message(
                    "The lobby's players changed while you were entering points. Press the button again.",
                    ephemeral=True)
                return
            tournament = db.get_tournament(conn, lobby.tournament_id)
            points = lobby_flow.parse_points(list(self.entered), self.entered)
            if isinstance(points, str):
                await interaction.response.send_message(points, ephemeral=True)
                return
            role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
            if self.side is None:
                reply = lobby_flow.finalize(conn, tournament, lobby, role, interaction.user.id, points)
            else:
                reply = lobby_flow.enter_points(conn, tournament, lobby, role, interaction.user.id, points)
            # No reply when the results post shows all there is to say: what they've entered.
            await answer_after_posts(interaction, conn, tournament["id"], reply)
        finally:
            conn.close()


async def open_points(interaction: discord.Interaction, conn: sqlite3.Connection, tournament: sqlite3.Row,
                      lobby: lobby_db.Lobby, final: bool, teams: tuple[str, ...] = lobby_db.SIDES) -> None:
    """Open the forms for `teams`' points: an organizer's final points, or else the
    presser's entry as one of the lobby's captains."""
    role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
    if final:
        problem = lobby_flow.finalize_problem(conn, tournament, lobby, role)
    else:
        problem = lobby_flow.points_problem(conn, tournament, lobby, role)
    if problem is not None:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    flow = PointsEntry(conn, lobby, None if final else role.side, teams)
    await interaction.response.send_modal(TeamPointsForm(flow, teams[0]))


async def _finish(interaction, conn, tournament, lobby) -> None:
    role = lobby_flow.role_in(conn, lobby, interaction.user.id, is_organizer(interaction))
    reply = lobby_flow.finish(conn, tournament, lobby, role, interaction.user.id)
    await answer_after_posts(interaction, conn, tournament["id"], reply)


async def _points_a(interaction, conn, tournament, lobby) -> None:
    await open_points(interaction, conn, tournament, lobby, final=False, teams=("A",))


async def _points_b(interaction, conn, tournament, lobby) -> None:
    await open_points(interaction, conn, tournament, lobby, final=False, teams=("B",))


async def _finalize(interaction, conn, tournament, lobby) -> None:
    await open_points(interaction, conn, tournament, lobby, final=True)


HANDLERS = {
    "register": _register,
    "unregister": _unregister,
    "startcheckin": _start_checkin,
    "checkin": _check_in,
    "endcheckin": _end_checkin,
    "submitcode": _submit_code,
    "getcode": _get_code,
    "finish": _finish,
    "pointsa": _points_a,
    "pointsb": _points_b,
    "finalize": _finalize,
}


# -- keeping posts up to date ------------------------------------------------------------

async def sync_posts(client: discord.Client, conn: sqlite3.Connection, tournament_id: int) -> None:
    """Bring the event's posts up to date."""
    for post in lobby_db.get_posts(conn, tournament_id):
        try:
            await _sync_post(client, conn, post)
        except Exception:
            log.exception("Couldn't update post %s of tournament %s", post["post_key"], tournament_id)


async def _sync_post(client: discord.Client, conn: sqlite3.Connection, post: sqlite3.Row) -> None:
    """Send a post that hasn't been sent, send it afresh when someone needs notifying (the
    old message is then deleted), or else edit it to match what it should show."""
    state = lobby_posts.describe(conn, post, mention)
    if state is None:
        return
    tournament_id, key = post["tournament_id"], post["post_key"]
    note = lobby_posts.notification(state, post["pinged"])
    if post["message_id"] is None or note is not None:
        claim = lobby_db.claim_post_send(conn, tournament_id, key)
        if claim is None:
            return  # something else is sending it right now
        pinged, notify = note if note is not None else (post["pinged"], [])
        message = await send_public(client.get_channel(post["channel_id"]), with_custom_emojis(client, state.text),
                                    view=_post_view(state, post), notify=notify)
        if message is None:
            lobby_db.release_post_send(conn, tournament_id, key)
            return
        if not lobby_db.take_over_post(conn, tournament_id, key, claim, message.id, lobby_posts.shown(state), pinged):
            old = message.id  # the claim was given up as stuck, so this message isn't wanted
        else:
            old = post["message_id"]
        if old is not None and not await delete_message(client, post["channel_id"], old):
            db.keep_replaced_match_post(conn, post["channel_id"], old)
        return
    if lobby_posts.shown(state) == post["shown"]:
        return
    channel = client.get_channel(post["channel_id"])
    if channel is None:
        return
    try:
        await channel.get_partial_message(post["message_id"]).edit(
            content=with_custom_emojis(client, state.text), view=_post_view(state, post))
    except discord.NotFound:
        lobby_db.mark_post_unsent(conn, tournament_id, key, post["message_id"])
        return
    except discord.HTTPException:
        log.exception("Couldn't update post %s of tournament %s", key, tournament_id)
        return
    lobby_db.set_post_shown(conn, tournament_id, key, post["message_id"], lobby_posts.shown(state))
