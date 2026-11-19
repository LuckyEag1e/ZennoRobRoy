"""What a 5v5 event's one public post says at each step, and which buttons it carries: every
lobby's teams, the waitlist, check-in, each lobby's room and points, and the results.
Shared by the bot and the CLI, which do the posting."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import db
import lobbies
import lobby_db
from lobby_db import SIDES, Lobby

DRAFTER_HOST_URL = "https://drafter.uma.guide/host"
# A captain's report is accepted this long after it last changed, unless the other captain
# answers first. The same 10 minutes a bracket match's vote waits.
ANSWER_WITHIN = timedelta(minutes=10)

# Button names, each made into a real button by the bot and shown in brackets by the CLI.
START_CHECKIN = "Start Check-In"
CHECK_IN = "Check In"
SUBMIT_ROOM_CODE = "Submit Room Code"
GET_ROOM_CODE = "Get Room Code"
REPORT_RESULT = "Report Result"
CONFIRM = "Confirm"
DISPUTE = "Dispute"


@dataclass
class PostState:
    text: str
    buttons: list[str]
    # What the post notifies people about, and who. Each audience entry is (why, user id).
    # When the phase changes, the post is sent afresh notifying the whole audience, since an
    # edit notifies nobody. Within a phase it's sent afresh only for entries that are new,
    # and then notifies only those users. None means the post is only ever edited.
    phase: Optional[str] = None
    audience: list[tuple[str, int]] = field(default_factory=list)


MAX_TEAM = lobbies.MAX_LOBBY // 2
# Discord's limit on a message's length.
MESSAGE_LIMIT = 2000


def register_command(name: str) -> str:
    """How to sign up for a 5v5 event, or change your captain choice, with a command."""
    return f"`/register tournament:{name} captain:<your choice>`"


def report_due_at(report: lobby_db.Report) -> datetime:
    return db.from_db_time(report.changed_at) + ANSWER_WITHIN


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
            mark = (" ✅" if names.checked_in(player) else " ⏳") if show_checkin else ""
            players.append(f"{names(player)}{crown}{score}{mark}")
        lines.append(f"{team_name(lobby, side, names)}{total}: {', '.join(players) or 'nobody'}")
    return lines


def missing_captain_lines(lobby: Lobby, names: Names, signups_open: bool) -> list[str]:
    """A warning for each team with no captain, which happens when its captain leaves and
    nobody left on it can captain, saying how it gets one."""
    lines = []
    for side in SIDES:
        team = lobby.teams[side]
        if team.captain is not None:
            continue
        if signups_open and len(team.players) < MAX_TEAM:
            lines.append(f"⚠️ {team_name(lobby, side, names)} has no captain. The next player who can captain and "
                         f"signs up before check-in ends joins it; otherwise an organizer will pick one.")
        else:
            lines.append(f"⚠️ {team_name(lobby, side, names)} has no captain: an organizer will pick one.")
    return lines


def _lobby_status(conn: sqlite3.Connection, lobby: Lobby, names: Names, player_label: Callable[[int], str]
                  ) -> tuple[list[str], Optional[lobby_db.Report]]:
    """A lobby's team lines and where it's up to once check-in is over, and its report
    if one is waiting for an answer."""
    result = lobby_db.get_result(conn, lobby.id)
    if result is not None:
        winner = result.winning_side
        return team_lines(lobby, names, False, result.points) + [f"🏆 {team_name(lobby, winner, names)} wins"], None
    report = lobby_db.get_report(conn, lobby.id)
    if report is not None:
        other = lobby.teams["B" if report.reporting_side == "A" else "A"].captain
        lines = team_lines(lobby, names, False, report.points)
        if report.disputed:
            return lines + ["⚠️ Points disputed: an organizer will settle them"], report
        answer = names(other) if other is not None else "The other captain"
        return lines + [f"📋 {answer}: press **{CONFIRM}** or **{DISPUTE}** on these points. They're accepted "
                        f"{_when(report_due_at(report))} otherwise."], report
    lines = team_lines(lobby, names, False)
    missing = missing_captain_lines(lobby, names, signups_open=False)
    if missing:
        lines += missing
    elif lobby.room_code is None:
        lines.append("Waiting for a captain to create the room")
    else:
        lines.append(f"🎮 Room ready, hosted by {player_label(lobby.host_user_id)}")
    return lines, None


def describe_event(conn: sqlite3.Connection, tournament: sqlite3.Row, player_label: Callable[[int], str]) -> PostState:
    names = Names(conn, tournament["id"], player_label)
    name = tournament["name"]
    drawn = lobby_db.get_lobbies(conn, tournament["id"])
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    running = lobby_db.checkin_running(tournament)
    finished = bool(tournament["checkin_finished"])
    starts = _start_line(tournament) if not finished else ""
    lines = [f"🎌 **{name}**{starts}"]
    reports = []
    for lobby in drawn:
        lines += ["", f"**Lobby {lobby.number}**"]
        if finished:
            status, report = _lobby_status(conn, lobby, names, player_label)
            lines += status
            if report is not None:
                reports.append((lobby, report))
        else:
            lines += team_lines(lobby, names, running) + missing_captain_lines(lobby, names, signups_open=True)
    waiting = [names(t) + ((" ✅" if names.checked_in(t) else " ⏳") if running else "") for t in waitlist]
    done = tournament["status"] in ("completed", "concluded")
    if waiting and not done:
        label = "Waitlist, in case a spot opens" if finished else "Waitlist, in order"
        lines += ["", f"**{label}:** {', '.join(waiting)}"]
    elif not finished:
        lines += ["", "**Waitlist:** nobody yet"]

    everyone = [("in", user_id) for user_id in names.user_ids([p for lobby in drawn for p in lobby.players])]
    waiters = [("in", user_id) for user_id in names.user_ids(waitlist)]
    if done:
        return PostState(_fit(lines + ["", "🏁 Every lobby has finished. Thanks for playing!"]), [])
    if finished:
        playing = [lobby for lobby in drawn if lobby_db.get_result(conn, lobby.id) is None]
        if any(lobby.room_code is None for lobby in playing):
            lines += ["", f"**Captains:** create the room at {DRAFTER_HOST_URL}, then press **{SUBMIT_ROOM_CODE}**."]
        lines += ["", f"**Everyone:** press **{GET_ROOM_CODE}** once your room is ready. After the races, a captain "
                      f"presses **{REPORT_RESULT}** (if the teams are level on points, play the tiebreaker first)."]
        buttons = [SUBMIT_ROOM_CODE, GET_ROOM_CODE, REPORT_RESULT] + ([CONFIRM, DISPUTE] if reports else [])
        answering = [(f"report:{lobby.id}:{report.changed_at}", user_id) for lobby, report in reports
                     if not report.disputed
                     for user_id in names.user_ids([c for c in lobby.captains if lobby.side_of(c) != report.reporting_side])]
        return PostState(_fit(lines), buttons, "play", everyone + answering)
    if running:
        ends = db.from_db_time(tournament["checkin_ends_at"])
        lines += ["", f"✅ **Check-in is open** until {_when(ends, 't')} ({_when(ends)}): press **{CHECK_IN}**. "
                      f"Anyone in a lobby who hasn't checked in by then is replaced by the next checked-in player "
                      f"on the waitlist."]
        return PostState(_fit(lines), [CHECK_IN], "checkin", everyone + waiters)
    lines += ["", f"Check-in opens before the start, and you'll be pinged here. Can't make it? "
                  f"`/match dropout tournament:{name}`"]
    return PostState(_fit(lines), [START_CHECKIN], "formed", everyone + waiters)


def _fit(lines: list[str]) -> str:
    """The post's text, shortening the longest lines if it's over Discord's limit."""
    text = "\n".join(lines)
    while len(text) > MESSAGE_LIMIT:
        longest = max(range(len(lines)), key=lambda i: len(lines[i]))
        lines[longest] = lines[longest][:len(lines[longest]) - (len(text) - MESSAGE_LIMIT) - 1] + "…"
        text = "\n".join(lines)
    return text


def _start_line(tournament: sqlite3.Row) -> str:
    if tournament["starts_at"] is None:
        return ""
    starts = db.from_db_time(tournament["starts_at"])
    return f" · starts {_when(starts, 'F')} ({_when(starts)})"


def describe(conn: sqlite3.Connection, post: sqlite3.Row, player_label: Callable[[int], str]) -> Optional[PostState]:
    """What `post` should show now, or None for a post that's no longer used."""
    if post["post_key"] != lobby_db.EVENT_POST:
        return None
    return describe_event(conn, db.get_tournament(conn, post["tournament_id"]), player_label)


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
