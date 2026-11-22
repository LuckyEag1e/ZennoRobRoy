"""What a 5v5 event's public posts say at each step, and which buttons they carry. The event
post holds every lobby's teams, the waitlist, check-in, each lobby's room and its result,
and each lobby gets a results post once its captains press Finish, where they enter the
points. Shared by the bot and the CLI, which do the posting."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import db
import lobby_db
from lobby_db import SIDES, Lobby

DRAFTER_URL = "https://drafter.uma.guide"
DRAFTER_HOST_URL = f"{DRAFTER_URL}/host"
# When only one captain has entered the points, they're accepted this long after their last
# change. The same 10 minutes a bracket match's vote waits.
ANSWER_WITHIN = timedelta(minutes=10)

# Button names, each made into a real button by the bot and shown in brackets by the CLI.
START_CHECKIN = "Start Check-In"
CHECK_IN = "Check In"
END_CHECKIN = "End Check-In"
SUBMIT_ROOM_CODE = "Submit Room Code"
GET_ROOM_CODE = "Get Room Code"
FINISH = "Finish"
FINALIZE = "Finalize Scores"

# The server's custom emojis, written by name. The bot swaps each for the emoji itself
# (common.with_custom_emojis), and the CLI shows the names.
BOT_EMOJI = ":winningticket:"
ROOM_EMOJI = ":teio_welcome:"
RESULTS_EMOJI = ":biwahiyahide:"
MISMATCH_EMOJI = ":mejiro_mcqueen_shocked:"
CUSTOM_EMOJIS = (BOT_EMOJI, ROOM_EMOJI, RESULTS_EMOJI, MISMATCH_EMOJI)
# How much longer a custom emoji gets once swapped in: `<`, its id (up to 20 digits) and `>`.
CUSTOM_EMOJI_GROWTH = 22


@dataclass(frozen=True)
class PostButton:
    label: str
    action: str  # what the bot's button does (lobby_posting.HANDLERS)


EVENT_BUTTONS = {name: PostButton(name, action) for name, action in (
    (START_CHECKIN, "startcheckin"), (CHECK_IN, "checkin"), (END_CHECKIN, "endcheckin"), (SUBMIT_ROOM_CODE, "submitcode"),
    (GET_ROOM_CODE, "getcode"), (FINISH, "finish"))}


def team_points_label(team: str) -> str:
    """The results post's button for entering one team's points, and that team's form's title."""
    return f"{team} Points"


@dataclass
class PostState:
    text: str
    buttons: list[PostButton]
    # What the post notifies people about, and who. Each audience entry is (why, user id).
    # When the phase changes, the post is sent afresh notifying the whole audience, since an
    # edit notifies nobody. Within a phase it's sent afresh only for entries that are new,
    # and then notifies only those users. None means the post is only ever edited.
    phase: Optional[str] = None
    audience: list[tuple[str, int]] = field(default_factory=list)


# Discord's limit on a message's length.
MESSAGE_LIMIT = 2000


def register_command(name: str) -> str:
    """How to sign up for a 5v5 event, or change your captain choice, with a command."""
    return f"`/register tournament:{name} captain:<your choice>`"


def entry_due_at(entry: lobby_db.Entry) -> datetime:
    """When one captain's points are accepted if the other captain enters none."""
    return db.from_db_time(entry.changed_at) + ANSWER_WITHIN


def _when(moment: datetime, style: str = "R") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>"


class Names:
    """How a post names the event's entrants: a mention for anyone with an account, and
    the entrant's name otherwise."""

    def __init__(self, conn: sqlite3.Connection, tournament_id: int, player_label: Callable[[int], str]) -> None:
        self.signups = {s.team_id: s for s in lobby_db.get_signups(conn, tournament_id)}
        self.player_label = player_label

    def __call__(self, team_id: int) -> str:
        signup = self.signups.get(team_id)
        if signup is None:
            return "someone who left"
        return self.player_label(signup.user_id) if signup.user_id is not None else f"**{signup.name}**"

    def plain(self, team_id: int) -> str:
        signup = self.signups.get(team_id)
        return signup.name if signup is not None else "someone who left"

    def user_ids(self, team_ids: list[int]) -> list[int]:
        return [self.signups[t].user_id for t in team_ids if t in self.signups and self.signups[t].user_id is not None]

    def checked_in(self, team_id: int) -> bool:
        return team_id in self.signups and self.signups[team_id].checked_in

    def checkin_mark(self, team_id: int) -> str:
        """✅ or ⏳ after a player's name during check-in. Players with no account are checked
        in by an organizer, since they have to be there to play too."""
        signup = self.signups.get(team_id)
        if signup is None:
            return ""
        return " ✅" if signup.checked_in else " ⏳"


def team_name(lobby: Lobby, side: str, names: Names) -> str:
    captain = lobby.teams[side].captain
    return f"Team {names.plain(captain)}" if captain is not None else f"Team {SIDES.index(side) + 1}"


def team_lines(lobby: Lobby, names: Names, show_checkin: bool, points: Optional[dict[int, int]] = None) -> list[str]:
    """One line per team: its name and players, marking the captain and, as asked,
    check-in and points."""
    lines = []
    for side in SIDES:
        team = lobby.teams[side]
        total = f" ({sum(points.get(p, 0) for p in team.players)})" if points is not None else ""
        players = []
        for player in team.players:
            crown = " (captain)" if player == team.captain else ""
            score = f" {points.get(player, 0)}" if points is not None else ""
            mark = names.checkin_mark(player) if show_checkin else ""
            players.append(f"{names(player)}{crown}{score}{mark}")
        lines.append(f"{team_name(lobby, side, names)}{total}: {', '.join(players) or 'nobody'}")
    return lines


def missing_captain_lines(lobby: Lobby, names: Names, signups_open: bool) -> list[str]:
    """A line for each team with no captain, which happens when its captain leaves and
    nobody left on it can captain. Its empty spot is held for the next one who signs up."""
    lines = []
    for side in SIDES:
        if lobby.teams[side].captain is not None:
            continue
        if signups_open:
            lines.append(f"⚠️ {team_name(lobby, side, names)} needs a captain: its empty spot is held for the next "
                         f"player who signs up and can captain.")
        else:
            lines.append(f"⚠️ {team_name(lobby, side, names)} needs a captain: an organizer will sort it out.")
    return lines


def _lobby_status(conn: sqlite3.Connection, lobby: Lobby, names: Names, player_label: Callable[[int], str]
                  ) -> list[str]:
    """A lobby's team lines and where it's up to once check-in is over."""
    result = lobby_db.get_result(conn, lobby.id)
    if result is not None:
        winner = result.winning_side
        return team_lines(lobby, names, False, result.points) + [f"🏆 {team_name(lobby, winner, names)} wins"]
    lines = team_lines(lobby, names, False)
    finished = lobby_db.get_finished_sides(conn, lobby.id)
    missing = missing_captain_lines(lobby, names, signups_open=False)
    if lobby_db.races_over(lobby, finished):
        return lines + ["🏁 Races over: the captains enter the points on the lobby's results post below"]
    if missing:
        return lines + missing
    if lobby.room_code is None:
        return lines + ["Waiting for a captain to create the room"]
    lines.append(f"{ROOM_EMOJI} Room ready, hosted by {player_label(lobby.host_user_id)}: press **{GET_ROOM_CODE}** to join")
    waiting = [lobby.teams[side].captain for side in SIDES if side not in finished]
    if finished:
        lines.append(f"🏁 {' and '.join(names(c) for c in lobby.captains if lobby.side_of(c) in finished)} pressed "
                     f"**{FINISH}**. Waiting for {' and '.join(names(c) for c in waiting)}.")
    return lines


# After a name in the event post's dropout list, by db.WITHDRAWAL_REASONS. Leaving before
# the draw deletes the sign-up, so everyone listed left after it.
_HOW_THEY_LEFT = {"dropped": "", "withdrawn": " (withdrawn by an organizer)", "disqualified": " (disqualified)"}


def describe_event(conn: sqlite3.Connection, tournament: sqlite3.Row, player_label: Callable[[int], str]) -> PostState:
    names = Names(conn, tournament["id"], player_label)
    name = tournament["name"]
    drawn = lobby_db.get_lobbies(conn, tournament["id"])
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    running = lobby_db.checkin_running(tournament)
    finished = bool(tournament["checkin_finished"])
    starts = _start_line(tournament) if not finished else ""
    lines = [f"{BOT_EMOJI} **{name}**{starts}"]
    for lobby in drawn:
        lines += ["", f"**Lobby {lobby.number}**"]
        if finished:
            lines += _lobby_status(conn, lobby, names, player_label)
        else:
            lines += team_lines(lobby, names, running) + missing_captain_lines(lobby, names, signups_open=True)
    waiting = [names(t) + (names.checkin_mark(t) if running else "") for t in waitlist]
    done = tournament["status"] in ("completed", "concluded")
    if waiting and not done:
        label = "Waitlist, in case a spot opens" if finished else "Waitlist, in order"
        lines += ["", f"**{label}:** {', '.join(waiting)}"]
    elif not finished:
        lines += ["", "**Waitlist:** nobody yet"]
    left = [signup for signup in names.signups.values() if signup.withdrawn]
    if left:
        lines += ["", "**Dropped out:** " + ", ".join(names(s.team_id) + _HOW_THEY_LEFT[s.withdrawn] for s in left)]

    everyone = [("in", user_id) for user_id in names.user_ids([p for lobby in drawn for p in lobby.players])]
    waiters = [("in", user_id) for user_id in names.user_ids(waitlist)]
    if done:
        return PostState(_fit(lines + ["", "🏁 Every lobby has finished. Thanks for playing!"]), [])
    if finished:
        playing = [lobby for lobby in drawn if lobby_db.get_result(conn, lobby.id) is None
                   and not lobby_db.races_over(lobby, lobby_db.get_finished_sides(conn, lobby.id))]
        if any(lobby.room_code is None for lobby in playing):
            lines += ["", f"**Captains:** create the room at {DRAFTER_HOST_URL}, then press **{SUBMIT_ROOM_CODE}**."]
        if playing:
            lines += ["", f"**Everyone:** press **{GET_ROOM_CODE}** once your room is ready.",
                      f"**Captains:** when the races are over (play the tiebreaker first if the teams are level on "
                      f"points), both press **{FINISH}**. You'll each enter the points on your lobby's results post."]
        buttons = [EVENT_BUTTONS[SUBMIT_ROOM_CODE], EVENT_BUTTONS[GET_ROOM_CODE]] + (
            [EVENT_BUTTONS[FINISH]] if playing else [])
        # Once one captain presses Finish, the other is pinged to press it too.
        finishing = [(f"finish:{lobby.id}", user_id) for lobby in playing
                     for user_id in _waiting_to_finish(conn, lobby, names)]
        return PostState(_fit(lines), buttons, "play", everyone + _room_ready(playing, names) + finishing)
    if running:
        ends = db.from_db_time(tournament["checkin_ends_at"])
        if ends <= datetime.now(timezone.utc) and lobby_db.teams_without_captain(conn, tournament["id"]):
            when = "is open until every team has a captain"
        else:
            when = f"is open until {_when(ends, 't')} ({_when(ends)})"
        lines += ["", f"✅ **Check-in {when}**: press **{CHECK_IN}**. Players without a linked account: tell an "
                      f"organizer you're here, and they'll check you in. Anyone in a lobby who hasn't checked in by "
                      f"then is replaced by the next checked-in player on the waitlist. Check-in ends early once "
                      f"everyone has checked in."]
        return PostState(_fit(lines), [EVENT_BUTTONS[CHECK_IN], EVENT_BUTTONS[END_CHECKIN]], "checkin",
                         everyone + waiters)
    lines += ["", f"Check-in opens before the start, and you'll be pinged here. Can't make it? "
                  f"`/match dropout tournament:{name}`"]
    return PostState(_fit(lines), [EVENT_BUTTONS[START_CHECKIN]], "formed", everyone + waiters)


def _room_ready(playing: list[Lobby], names: Names) -> list[tuple[str, int]]:
    """Each lobby's players are pinged when its room code is in, and again if a new one
    replaces it, so nobody has to keep checking. Not the host, who entered it."""
    return [(f"room:{lobby.id}:{hashlib.sha1(lobby.room_code.encode()).hexdigest()[:8]}", user_id)
            for lobby in playing if lobby.room_code is not None
            for user_id in names.user_ids(lobby.players) if user_id != lobby.host_user_id]


def _waiting_to_finish(conn: sqlite3.Connection, lobby: Lobby, names: Names) -> list[int]:
    """The captains still to press Finish once the other one has."""
    finished = lobby_db.get_finished_sides(conn, lobby.id)
    if not finished:
        return []
    return names.user_ids([lobby.teams[side].captain for side in SIDES
                           if side not in finished and lobby.teams[side].captain is not None])


def describe_results(conn: sqlite3.Connection, lobby: Lobby, player_label: Callable[[int], str]) -> PostState:
    """A lobby's results post: each captain enters everyone's points, and matching points
    are final. Points that don't match are shown so the captains can correct theirs, or an
    organizer can finalize them."""
    tournament = db.get_tournament(conn, lobby.tournament_id)
    names = Names(conn, lobby.tournament_id, player_label)
    title = f"{RESULTS_EMOJI} **{tournament['name']} · Lobby {lobby.number} results**"
    result = lobby_db.get_result(conn, lobby.id)
    if result is not None:
        winner = team_name(lobby, result.winning_side, names)
        return PostState(_fit([title, ""] + team_lines(lobby, names, False, result.points)
                              + ["", f"🏆 **{winner} wins!** Congratulations, and thanks for playing."]), [])
    captains = {side: lobby.teams[side].captain for side in SIDES if lobby.teams[side].captain is not None}
    entries = lobby_db.get_entries(conn, lobby.id)
    teams = {side: team_name(lobby, side, names) for side in SIDES}
    buttons = [PostButton(team_points_label(teams[side]), f"points{side.lower()}") for side in SIDES]
    lines = [title, "", f"{' and '.join(names(c) for c in captains.values())}: press both buttons below and enter "
                        f"every player's points, including the tiebreaker if one was played."
             + (" Points you both enter the same are final." if len(captains) == 2 else "")]
    audience = [("points", u) for u in names.user_ids(list(captains.values()))]
    mismatched = lobby_db.mismatched_players(lobby, entries)
    if mismatched:
        a, b = (names.plain(lobby.teams[side].captain) for side in SIDES)
        lines += ["", f"{MISMATCH_EMOJI} **The points don't match** for: " + "; ".join(
            f"{names(p)} ({a}: {entries['A'].points[p]}, {b}: {entries['B'].points[p]})" for p in mismatched),
            "**Captains:** check with each other, and the one who's wrong presses that team's button to correct "
            "theirs. Once they match, they're final.",
            f"**Organizers:** press **{FINALIZE}** to enter the right points."]
        audience += [(f"mismatch:{entries['A'].changed_at}:{entries['B'].changed_at}", user_id)
                     for user_id in names.user_ids(list(captains.values()))]
        return PostState(_fit(lines), buttons + [PostButton(FINALIZE, "finalize")], "points", audience)
    lines.append("")
    for side, captain in captains.items():
        lines.append(_entry_progress(lobby, entries.get(side), names(captain), teams))
    lone = lobby_db.lone_entry(lobby, entries)
    if lone is not None and len(captains) == 2:
        other = next(c for side, c in captains.items() if side != lone.side)
        lines.append(f"If {names(other)} doesn't finish entering theirs, {names.plain(captains[lone.side])}'s are "
                     f"accepted {_when(entry_due_at(lone))}.")
    return PostState(_fit(lines), buttons, "points", audience)


def _entry_progress(lobby: Lobby, entry: Optional[lobby_db.Entry], captain: str, teams: dict[str, str]) -> str:
    """How far one captain has got with entering the points."""
    if entry is None:
        return f"⏳ Waiting for {captain}"
    if lobby_db.is_complete(lobby, entry):
        return f"✅ {captain} has entered points"
    done = [side for side in SIDES if all(p in entry.points for p in lobby.teams[side].players)]
    to_go = [teams[side] for side in SIDES if side not in done]
    return (f"⏳ {captain} has entered {' and '.join(teams[side] for side in done)}'s points, "
            f"still to enter {' and '.join(to_go)}'s")


def _fit(lines: list[str]) -> str:
    """The post's text, shortening the longest lines if it's over Discord's limit, leaving
    room for its custom emojis to be swapped in."""
    text = "\n".join(lines)
    limit = MESSAGE_LIMIT - CUSTOM_EMOJI_GROWTH * sum(text.count(emoji) for emoji in CUSTOM_EMOJIS)
    while len(text) > limit:
        longest = max(range(len(lines)), key=lambda i: len(lines[i]))
        lines[longest] = lines[longest][:len(lines[longest]) - (len(text) - limit) - 1] + "…"
        text = "\n".join(lines)
    return text


def _start_line(tournament: sqlite3.Row) -> str:
    if tournament["starts_at"] is None:
        return ""
    starts = db.from_db_time(tournament["starts_at"])
    return f" · starts {_when(starts, 'F')} ({_when(starts)})"


def describe(conn: sqlite3.Connection, post: sqlite3.Row, player_label: Callable[[int], str]) -> Optional[PostState]:
    """What `post` should show now, or None for a post that's no longer used."""
    key = post["post_key"]
    if key == lobby_db.EVENT_POST:
        return describe_event(conn, db.get_tournament(conn, post["tournament_id"]), player_label)
    prefix = lobby_db.results_post_key(0)[:-1]
    if key.startswith(prefix) and key[len(prefix):].isdigit():
        lobby = lobby_db.get_lobby(conn, int(key[len(prefix):]))
        if lobby is not None:
            return describe_results(conn, lobby, player_label)
    return None


def shown(state: PostState) -> str:
    """What's recorded as a post's content, so a change to the buttons alone still edits it."""
    return state.text + "\n" + "|".join(button.label for button in state.buttons)


def parse_pinged(pinged: Optional[str]) -> tuple[Optional[str], set[str]]:
    """The phase a post last notified people about, and the audience entries it notified."""
    if not pinged:
        return None, set()
    phase, _, entries = pinged.partition("|")
    return phase, {entry for entry in entries.split(",") if entry}


def _entry(why: str, user_id: int) -> str:
    return f"{why}@{user_id}"


def notification(state: PostState, pinged: Optional[str]) -> Optional[tuple[str, list[int]]]:
    """(the new `pinged` to store, who to notify) if the post must be sent afresh to notify
    someone, or None if an edit will do."""
    if state.phase is None:
        return None
    phase, notified = parse_pinged(pinged)
    audience = {_entry(why, user_id): user_id for why, user_id in state.audience}
    if phase == state.phase:
        new = [entry for entry in audience if entry not in notified]
        if not new:
            return None
        notify = list(dict.fromkeys(audience[entry] for entry in new))
        everyone = notified | set(audience)
    else:
        notify = list(dict.fromkeys(audience.values()))
        everyone = set(audience)
    return f"{state.phase}|{','.join(sorted(everyone))}", notify
