"""What a 5v5 event's public posts say at each step, and which buttons they carry: the event
post (the waitlist and check-in) and one post per lobby (its teams, check-in, the room
code, the captains' report and the result). Shared by the bot and the CLI, which do the
posting. Every post tells whoever reads it what to do next."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import db
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
    # What the post notifies people about, and who. When the phase changes, the post is
    # sent afresh notifying `audience`, since an edit notifies nobody. Within a phase it's
    # sent afresh only when someone new joins the audience, and then notifies only them.
    phase: Optional[str] = None
    audience: list[int] = field(default_factory=list)


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
    """Each team's name and players, marking captains and, as asked, check-in and points."""
    lines = []
    for side in SIDES:
        team = lobby.teams[side]
        total = f": {sum(points.get(p, 0) for p in team.players)} points" if points is not None else ""
        lines.append(f"**{team_name(lobby, side, names)}**{total}")
        for player in team.players:
            mark = (" ✅" if names.checked_in(player) else " ⏳") if show_checkin else ""
            crown = " (captain)" if player == team.captain else ""
            score = f": {points.get(player, 0)}" if points is not None else ""
            lines.append(f"• {names(player)}{crown}{score}{mark}")
        if team.captain is None:
            lines.append("⚠️ This team has no captain yet. An organizer will pick one.")
    return lines


def describe_event(conn: sqlite3.Connection, tournament: sqlite3.Row, player_label: Callable[[int], str]) -> PostState:
    names = Names(conn, tournament["id"], player_label)
    name = tournament["name"]
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    lobby_count = len(lobby_db.get_lobbies(conn, tournament["id"]))
    running = lobby_db.checkin_running(tournament)
    waiting_lines = [f"{place}. {names(team_id)}" + ((" ✅" if names.checked_in(team_id) else " ⏳") if running else "")
                     for place, team_id in enumerate(waitlist, start=1)]
    waiting = "\n".join(waiting_lines) if waiting_lines else "Nobody yet."
    join_late = f"Signing up now with `/register tournament:{name}` adds you to the end of the waitlist."

    if tournament["status"] in ("completed", "concluded"):
        return PostState(f"🏁 Every lobby in **{name}** has finished. Thanks for playing!", [])
    if tournament["checkin_finished"]:
        text = (f"🏁 Check-in for **{name}** is over, and the lobbies are final.\n**Still on the waitlist:**\n{waiting}\n"
                f"If someone in a lobby drops out before their lobby's result is in, the next player here who "
                f"checked in takes their spot and is pinged on that lobby's post.")
        return PostState(text, [])
    if running:
        ends = db.from_db_time(tournament["checkin_ends_at"])
        text = (f"✅ **Check-in is open** for **{name}** until {_when(ends, 't')} ({_when(ends)}). Everyone in a "
                f"lobby and everyone on the waitlist: press **{CHECK_IN}**, here or on your lobby's post.\n"
                f"**Waitlist**, in order:\n{waiting}\n"
                f"When check-in ends, anyone in a lobby who hasn't checked in is replaced by the next checked-in "
                f"player on the waitlist. {join_late}")
        return PostState(text, [CHECK_IN], "checkin", names.user_ids(waitlist))
    starts = _start_line(tournament)
    lobby_word = "lobby is" if lobby_count == 1 else "lobbies are"
    text = (f"🏁 **{name}**: the {lobby_word} set! {starts} Each lobby has its own post below.\n"
            f"**Waitlist**, in order:\n{waiting}\n"
            f"On the waitlist? Stay ready: check-in opens before the start and you'll be pinged here. If someone "
            f"in a lobby can't make it or doesn't check in, the next checked-in player on the waitlist takes "
            f"their spot. {join_late}")
    return PostState(text, [START_CHECKIN], "formed", names.user_ids(waitlist))


def _start_line(tournament: sqlite3.Row) -> str:
    if tournament["starts_at"] is None:
        return ""
    starts = db.from_db_time(tournament["starts_at"])
    return f"Play starts {_when(starts, 'F')} ({_when(starts)})."


def describe_lobby(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby,
                   player_label: Callable[[int], str]) -> PostState:
    names = Names(conn, tournament["id"], player_label)
    name = tournament["name"]
    heading = f"🎌 **{name}**, lobby {lobby.number}"
    result = lobby_db.get_result(conn, lobby.id)
    if result is not None:
        winner = result.winning_side
        loser = "B" if winner == "A" else "A"
        totals = {side: sum(result.points.get(p, 0) for p in lobby.teams[side].players) for side in SIDES}
        text = "\n".join([f"🏆 **{team_name(lobby, winner, names)}** wins lobby {lobby.number} of **{name}**, "
                          f"{totals[winner]} points to {totals[loser]}."]
                         + team_lines(lobby, names, False, result.points))
        return PostState(text, [])

    everyone = names.user_ids(lobby.players)
    if not tournament["checkin_ends_at"]:
        text = "\n".join([heading] + team_lines(lobby, names, False) + [
            f"You're in! {_start_line(tournament)} Check-in opens before then, and you'll be pinged here: press "
            f"**{CHECK_IN}** when it does. Can't make it? Run `/match dropout tournament:{name}` and the next "
            f"player on the waitlist takes your spot."])
        return PostState(text, [], "formed", everyone)
    if lobby_db.checkin_running(tournament):
        ends = db.from_db_time(tournament["checkin_ends_at"])
        text = "\n".join([heading] + team_lines(lobby, names, True) + [
            f"✅ **Check in now** by pressing **{CHECK_IN}**. Check-in ends {_when(ends)}, and anyone who hasn't "
            f"checked in by then is replaced from the waitlist."])
        return PostState(text, [CHECK_IN], "checkin", everyone)

    report = lobby_db.get_report(conn, lobby.id)
    captains = [names(captain) for captain in lobby.captains]
    if report is not None:
        reporter = player_label(report.reported_by)
        other_side = "B" if report.reporting_side == "A" else "A"
        other_captain = lobby.teams[other_side].captain
        lines = [heading, f"📋 {reporter} reported the points:"] + team_lines(lobby, names, False, report.points)
        if report.disputed:
            lines.append(f"⚠️ {names(other_captain) if other_captain else 'The other team'} disputed these points. An "
                         f"organizer will settle it. A captain can press **{REPORT_RESULT}** to enter corrected points.")
            return PostState("\n".join(lines), [REPORT_RESULT])
        answer = f"{names(other_captain)}: " if other_captain is not None else "Other team's captain: "
        lines.append(f"{answer}press **{CONFIRM}** if that's right, or **{DISPUTE}** if it isn't. The points are "
                     f"accepted {_when(report_due_at(report))} otherwise.")
        return PostState("\n".join(lines), [CONFIRM, DISPUTE, REPORT_RESULT], f"report:{report.changed_at}",
                         names.user_ids([other_captain]) if other_captain is not None else [])

    lines = [heading, "Check-in is over, and these teams are final."] + team_lines(lobby, names, False)
    if lobby.room_code is None:
        lines.append(f"**Captains** ({' and '.join(captains) or 'to be picked'}): one of you open "
                     f"{DRAFTER_HOST_URL}, create the room, then press **{SUBMIT_ROOM_CODE}** here and enter its code.")
        lines.append(f"**Everyone else:** wait here for the room code, then press **{GET_ROOM_CODE}**. Only you see it.")
        return PostState("\n".join(lines), [SUBMIT_ROOM_CODE, GET_ROOM_CODE], "ready", everyone)
    lines.append(f"🎮 Hosted by {player_label(lobby.host_user_id)}. Press **{GET_ROOM_CODE}** for the code and the "
                 f"link to join. Then draft with your team, train your character, and race.")
    lines.append(f"**Captains:** when the races are done (play the tiebreaker track first if the teams are level on "
                 f"points), press **{REPORT_RESULT}** and enter everyone's points. The other captain then confirms them.")
    return PostState("\n".join(lines), [GET_ROOM_CODE, SUBMIT_ROOM_CODE, REPORT_RESULT], "ready", everyone)


def describe(conn: sqlite3.Connection, post: sqlite3.Row, player_label: Callable[[int], str]) -> Optional[PostState]:
    """What `post` should show now, or None if its lobby no longer exists."""
    tournament = db.get_tournament(conn, post["tournament_id"])
    if post["post_key"] == lobby_db.EVENT_POST:
        return describe_event(conn, tournament, player_label)
    lobby = lobby_db.get_lobby(conn, int(post["post_key"].split(":")[1]))
    return describe_lobby(conn, tournament, lobby, player_label) if lobby is not None else None


def parse_pinged(pinged: Optional[str]) -> tuple[Optional[str], set[int]]:
    """The phase a post last notified people about, and who it notified."""
    if not pinged:
        return None, set()
    phase, _, ids = pinged.partition("|")
    return phase, {int(user_id) for user_id in ids.split(",") if user_id}


def notification(state: PostState, pinged: Optional[str]) -> Optional[tuple[str, list[int]]]:
    """(the new `pinged` to store, who to notify) if the post must be sent afresh to notify
    someone, or None if an edit will do."""
    if state.phase is None:
        return None
    phase, notified = parse_pinged(pinged)
    if phase == state.phase:
        new = [user_id for user_id in state.audience if user_id not in notified]
        if not new:
            return None
        notify = new
        everyone = notified | set(state.audience)
    else:
        notify = list(state.audience)
        everyone = set(state.audience)
    return f"{state.phase}|{','.join(str(user_id) for user_id in sorted(everyone))}", notify
