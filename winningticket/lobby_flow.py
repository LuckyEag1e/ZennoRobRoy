"""What players, captains and organizers can do in a 5v5 event, and the private reply each
gets: signing up, dropping out, check-in, room codes, and reporting points. The checks
and wording live here so the bot and the CLI behave and read the same. Public posts are
lobby_posts.py's job; every change here shows up on them."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
import lobbies
import lobby_db
from labels import OPTED_OUT_NOTE, name_held_message
from lobby_db import SIDES, Lobby
from lobby_posts import (
    DRAFTER_HOST_URL,
    REPORT_RESULT,
    register_command,
    report_due_at,
    team_lines,
    team_name,
    Names,
)

log = logging.getLogger("winningticket")

CAPTAIN_CHOICES = {
    "want": "Want to captain",
    "can": "Can captain if needed",
    "no": "Not captain",
}
CAPTAIN_QUESTION = ("Will you captain? Each team needs one: captains host the drafter room or join it, give the bot "
                    "the room code, and report everyone's points at the end.")
# Asked of an organizer adding someone without a captain choice, after "Will <name> captain?".
ADD_CAPTAIN_QUESTION = "Captains host the drafter room and report the points, and need a linked account."
DEFAULT_CHECKIN_MINUTES = 15
MAX_CHECKIN_MINUTES = 120
ROOM_CODE_MAX_LENGTH = 32
MAX_POINTS = 999


def split_label(split: lobbies.Split) -> str:
    """A split as the close names it, such as "5v5 + 5v4, 3 waitlisted"."""
    lobby_names = " + ".join("v".join(str(size) for size in lobbies.team_sizes(size)) for size in split.lobby_sizes)
    return f"{lobby_names}, {split.waitlisted} waitlisted" if split.waitlisted else lobby_names


def takes_late_signups(tournament: sqlite3.Row) -> bool:
    """After the draw, sign-ups still go in (to a team with room, or the waitlist) until
    check-in is over."""
    return tournament["status"] == "in_progress" and not tournament["checkin_finished"]


def move_player(conn: sqlite3.Connection, tournament: sqlite3.Row, team_id: int) -> str:
    """An organizer moving a player to the other team of their lobby, usually to give a
    team with no captain one. Returns the organizer's reply."""
    lobby = lobby_db.find_lobby_of(conn, tournament["id"], team_id)
    if lobby is None:
        return "That player isn't in a lobby."
    if lobby_db.get_result(conn, lobby.id) is not None:
        return "That lobby's result is already in."
    names = Names(conn, tournament["id"], str)
    other = "B" if lobby.side_of(team_id) == "A" else "A"
    change = lobby_db.move_to_other_team(conn, tournament["id"], team_id)
    if change is None:
        return f"{team_name(lobby, other, names)} is full, so there's no spot to move them into."
    lobby = lobby_db.get_lobby(conn, lobby.id)
    names = Names(conn, tournament["id"], str)
    reply = f"✅ {names.plain(team_id)} moved to {team_name(lobby, other, names)}."
    if change.newcomer is not None:
        reply += f" {names.plain(change.newcomer)} takes their old spot from the waitlist."
    return "\n".join([reply] + captain_fix_lines(conn, db.get_tournament(conn, tournament["id"])))


def captain_fix_lines(conn: sqlite3.Connection, tournament: sqlite3.Row) -> list[str]:
    """For an organizer: each team with no captain, and how to give it one. Check-in can't
    start, or finish, until every team has one."""
    name = tournament["name"]
    names = Names(conn, tournament["id"], str)
    prefs = lobby_db.captain_prefs(list(names.signups.values()))
    lines = []
    for lobby, side in lobby_db.teams_without_captain(conn, tournament["id"]):
        other = "B" if side == "A" else "A"
        spare = [p for p in lobby.teams[other].players if lobbies.is_candidate(prefs.get(p, "no"))]
        line = f"⚠️ Lobby {lobby.number}'s {team_name(lobby, side, names)} has no captain."
        if len(spare) >= 2:
            mover = next(p for p in spare if p != lobby.teams[other].captain)
            line += (f" {team_name(lobby, other, names)} has {len(spare)} who can captain: move one over with "
                     f"`/admin lobbymove tournament:{name} entrant:{names.plain(mover)}`.")
        elif takes_late_signups(tournament):
            line += " Its empty spot is held for the next player who signs up and can captain."
        line += " Or make a player on it with a linked account captain with `/admin setcaptain`."
        lines.append(line)
    if lines and takes_late_signups(tournament):
        stage = "finish" if lobby_db.checkin_running(tournament) else "start"
        lines.append(f"Check-in can't {stage} until every team has a captain.")
    return lines


def signups_over(name: str, by_organizer: bool) -> str:
    """Why the sign-up commands no longer change a 5v5 event once check-in is over, and
    what to use instead: a dropout, filled from the checked-in waitlist."""
    if by_organizer:
        return (f"Check-in for **{name}** is over, so sign-ups are closed. To take a player out, use `/admin "
                f"withdraw`: their spot goes to the next checked-in player on the waitlist.")
    return (f"Check-in for **{name}** is over, so sign-ups are closed. To give up your spot, run `/match dropout "
            f"tournament:{name}`.")


def signup_reply(conn: sqlite3.Connection, tournament: sqlite3.Row, team_id: int, pref: str,
                 changed: bool = False) -> str:
    """What a player is told once they're signed up, or have changed their captain choice.
    Only what's theirs alone: the public post already says how the event runs."""
    name = tournament["name"]
    choice = CAPTAIN_CHOICES[pref]
    if changed:
        return f"✅ Your captain choice for **{name}** is now: {choice}."
    if tournament["status"] == "registration":
        where = f"✅ You're signed up for **{name}**. Captain choice: {choice}."
    else:
        checked = " You're checked in already." if lobby_db.checkin_running(tournament) else ""
        where = (f"✅ You're signed up for **{name}**. {late_signup_line(conn, tournament['id'], team_id, True)} "
                 f"Captain choice: {choice}.{checked}")
    return where + f"\nTo change your captain choice, run {register_command(name)} again."


def late_signup_line(conn: sqlite3.Connection, tournament_id: int, team_id: int, you: bool) -> str:
    """Where a sign-up from after the draw was placed, told to them (`you`) or to the
    organizer who added them."""
    who = "You're" if you else "They're"
    lobby = lobby_db.find_lobby_of(conn, tournament_id, team_id)
    names = Names(conn, tournament_id, str)
    if lobby is None:
        place = f"{who} **#{lobby_db.get_waitlist(conn, tournament_id).index(team_id) + 1} on the waitlist**"
        # Every team with a captain is full, or they'd have been placed. Any room left is a
        # spot held for a captain, which is worth saying since the lobby isn't full.
        held = [f"{team_name(held_lobby, side, names)} in lobby {held_lobby.number}"
                for held_lobby, side in lobby_db.teams_without_captain(conn, tournament_id)
                if len(held_lobby.teams[side].players) < lobbies.MAX_LOBBY // 2]
        if not held:
            return f"{place}."
        return (f"{place}: {' and '.join(held)} {'has' if len(held) == 1 else 'have'} room, but only for a player "
                f"who can captain, since {'it has' if len(held) == 1 else 'they have'} no captain.")
    return f"{who} in lobby {lobby.number}, on **{team_name(lobby, lobby.side_of(team_id), names)}**."


def returning_entrant(conn: sqlite3.Connection, tournament: sqlite3.Row, name: str,
                      user_id: Optional[int]) -> Optional[int]:
    """The entrant an organizer means by adding `name` (or the member `user_id`) again to a
    drawn 5v5 event they left, or None if that isn't who they mean."""
    if tournament["status"] == "registration":
        return None
    if user_id is not None:
        team_id = db.get_entrant_team_id(conn, tournament["id"], user_id)
    else:
        team = db.get_team_by_name(conn, tournament["id"], name)
        team_id = team["id"] if team is not None and not db.team_member_ids(conn, team["id"]) else None
    if team_id is None or not db.get_teams(conn, tournament["id"])[team_id]["withdrawn"]:
        return None
    return team_id


def captain_problem(pref: str, linked: bool, by_organizer: bool) -> Optional[str]:
    """Why someone can't be signed up with this captain choice, or None if they can. Only
    entrants with a linked account can captain, and both ways of signing up (a player
    themselves, or an organizer adding them) refuse rather than change the choice."""
    if linked or pref == "no":
        return None
    fix = ("Add them as Not captain instead." if by_organizer else
           "Pick Not captain, or press Register again and choose Agree.")
    return (f"Only players with a linked account can captain, since captains give the bot the room code and report "
            f"the points. {fix}")


def sign_up(conn: sqlite3.Connection, tournament: sqlite3.Row, name: str, user_id: int, linked: bool,
            pref: str) -> str:
    """Sign a member up for a 5v5 event with their captain choice, or change the choice
    if they're already in. `linked` says whether they agreed to link the entry to their
    account; if not, they're entered by `name` only. Returns their private reply."""
    tournament_name = tournament["name"]
    existing = db.get_entrant_team_id(conn, tournament["id"], user_id)
    if tournament["status"] != "registration" and not takes_late_signups(tournament):
        return f"Sign-ups for **{tournament_name}** are closed."
    if existing is not None:
        withdrawn = db.get_teams(conn, tournament["id"])[existing]["withdrawn"]
        if withdrawn == "disqualified":
            return f"An organizer disqualified you from **{tournament_name}**, so you can't sign up again."
        if withdrawn:
            lobby_db.rejoin(conn, tournament["id"], existing, pref)
            return signup_reply(conn, db.get_tournament(conn, tournament["id"]), existing, pref)
        lobby_db.set_captain_pref(conn, tournament["id"], existing, pref)
        return signup_reply(conn, tournament, existing, pref, changed=True)
    problem = captain_problem(pref, linked, by_organizer=False)
    if problem is not None:
        return problem
    entrant = db.register_entrant(conn, tournament["id"], name, user_id if linked else None)
    if entrant is None:
        if linked:
            return name_held_message(tournament_name, name)
        return (f"Someone is already signed up for **{tournament_name}** as {name}. If that's you, you're in. If "
                f"not, ask an organizer to add you under another name.")
    team_id, _ = entrant
    lobby_db.set_captain_pref(conn, tournament["id"], team_id, pref)
    if tournament["status"] != "registration":
        lobby_db.place_late_signup(conn, tournament["id"], team_id)
    reply = signup_reply(conn, db.get_tournament(conn, tournament["id"]), team_id, pref)
    return reply if linked else f"{reply}\n{OPTED_OUT_NOTE}"


def dropout(conn: sqlite3.Connection, tournament: sqlite3.Row, team_id: int, reason: str, by: int) -> str:
    """Take a player out of a drawn event and say what happened to their spot. `reason` is
    one of db.WITHDRAWAL_REASONS."""
    name = tournament["name"]
    lobby = lobby_db.find_lobby_of(conn, tournament["id"], team_id)
    if lobby is not None and lobby_db.get_result(conn, lobby.id) is not None:
        return f"That lobby's result is already in, so there's nothing left to drop out of in **{name}**."
    change = lobby_db.leave(conn, tournament["id"], team_id, reason, by)
    if change is None:
        return f"That player isn't in a lobby or on the waitlist for **{name}**."
    if change.lobby_id is None:
        return f"Done: off the waitlist for **{name}**."
    names = Names(conn, tournament["id"], str)
    if change.newcomer is None:
        reply = f"Done. Nobody was waiting to take the spot in lobby {lobby.number}, so that team plays a player short."
    else:
        reply = f"Done. {names.plain(change.newcomer)} takes the spot in lobby {lobby.number} from the waitlist."
    if change.captain_lost and reason != "dropped":
        reply = "\n".join([reply] + captain_fix_lines(conn, db.get_tournament(conn, tournament["id"])))
    return reply


def checkin_start_problem(conn: sqlite3.Connection, tournament: sqlite3.Row) -> Optional[str]:
    """Why an organizer can't start check-in now, or None if they can."""
    name = tournament["name"]
    if tournament["status"] != "in_progress" or tournament["format"] != "lobbies":
        return f"**{name}** has no lobbies waiting for check-in."
    if tournament["checkin_ends_at"] is not None:
        return f"Check-in for **{name}** has already started."
    missing = captain_fix_lines(conn, tournament)
    return "\n".join(missing) if missing else None


def start_checkin(conn: sqlite3.Connection, tournament: sqlite3.Row, minutes: int, now: datetime) -> str:
    problem = checkin_start_problem(conn, tournament)
    if problem is not None:
        return problem
    ends = now + timedelta(minutes=minutes)
    if not lobby_db.start_checkin(conn, tournament["id"], db.to_db_time(ends)):
        return f"Check-in for **{tournament['name']}** has already started."
    return f"✅ Check-in for **{tournament['name']}** is open for {minutes} minutes."


def cancel_checkin(conn: sqlite3.Connection, tournament: sqlite3.Row) -> str:
    name = tournament["name"]
    if not lobby_db.cancel_checkin(conn, tournament["id"]):
        if tournament["checkin_finished"]:
            return f"Check-in for **{name}** is already over."
        return f"Check-in for **{name}** isn't open."
    return f"✅ Check-in for **{name}** is called off, and everyone's check-in is cleared. Start it again when you're ready."


def check_in(conn: sqlite3.Connection, tournament: sqlite3.Row, user_id: int) -> str:
    name = tournament["name"]
    team_id = db.get_entrant_team_id(conn, tournament["id"], user_id)
    if team_id is None:
        return f"You aren't signed up for **{name}**. {register_command(name)} signs you up."
    if not lobby_db.checkin_running(tournament):
        if tournament["checkin_ends_at"] is None:
            return f"Check-in for **{name}** hasn't opened yet. You'll be pinged when it does."
        return f"Check-in for **{name}** is over."
    in_lobby = lobby_db.find_lobby_of(conn, tournament["id"], team_id)
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    if in_lobby is None and team_id not in waitlist:
        return f"You're no longer playing in **{name}**."
    newly = lobby_db.check_in(conn, team_id)
    done = "✅ Checked in" if newly else "You're already checked in"
    ends = db.from_db_time(tournament["checkin_ends_at"])
    if in_lobby is not None:
        return f"{done} for lobby {in_lobby.number} of **{name}**."
    place = waitlist.index(team_id) + 1
    return (f"{done} for **{name}**, **#{place} on the waitlist**. Stay around until check-in ends "
            f"<t:{int(ends.timestamp())}:R> in case you're brought in.")


def check_in_for(conn: sqlite3.Connection, tournament: sqlite3.Row, team_id: int) -> str:
    """An organizer checking a player in on their behalf. Returns the organizer's reply."""
    name = tournament["name"]
    who = Names(conn, tournament["id"], str).plain(team_id)
    if not lobby_db.checkin_running(tournament):
        return f"Check-in for **{name}** isn't open."
    in_lobby = lobby_db.find_lobby_of(conn, tournament["id"], team_id)
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    if in_lobby is None and team_id not in waitlist:
        return f"{who} is no longer playing in **{name}**."
    if not lobby_db.check_in(conn, team_id):
        return f"{who} is already checked in."
    where = f"lobby {in_lobby.number}" if in_lobby is not None else f"#{waitlist.index(team_id) + 1} on the waitlist"
    return f"✅ Checked in {who} ({where})."


def finish_due_checkins(conn: sqlite3.Connection) -> list[int]:
    """End every check-in that has run out, returning the events it ended for. One that
    fails is logged and tried again next time, without holding up the others."""
    finished = []
    for tournament in lobby_db.get_checkins_due(conn):
        try:
            if lobby_db.finish_checkin(conn, tournament["id"]) is not None:
                finished.append(tournament["id"])
        except Exception:
            log.exception("Couldn't end check-in for tournament %s", tournament["id"])
    return finished


@dataclass(frozen=True)
class Role:
    """Who someone is in a lobby: a player in it, a captain of it, an organizer, or none."""

    team_id: Optional[int]
    side: Optional[str]
    captain: bool
    organizer: bool


def role_in(conn: sqlite3.Connection, lobby: Lobby, user_id: int, organizer: bool) -> Role:
    team_id = db.get_entrant_team_id(conn, lobby.tournament_id, user_id)
    side = lobby.side_of(team_id) if team_id is not None else None
    return Role(team_id if side is not None else None, side, side is not None and team_id in lobby.captains, organizer)


def _not_ready_for_code(tournament: sqlite3.Row) -> Optional[str]:
    if not tournament["checkin_finished"]:
        return "The room is set up once check-in is over, so every player in the lobby is final."
    if tournament["status"] != "in_progress":
        return f"**{tournament['name']}** has finished."
    return None


def name_only_players(conn: sqlite3.Connection, lobby: Lobby) -> list[str]:
    names = Names(conn, lobby.tournament_id, str)
    return [names.plain(player) for player in lobby.players if names.signups[player].user_id is None]


def _name_only_note(conn: sqlite3.Connection, lobby: Lobby) -> str:
    left_out = name_only_players(conn, lobby)
    if not left_out:
        return ""
    return (f"\n**Give the code to these players yourself**, since they don't have a linked account and can't get it "
            f"from the bot: {', '.join(left_out)}.")


def room_code_problem(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby, role: Role) -> Optional[str]:
    """Why someone can't give the bot a room code for `lobby`, or None if they can."""
    if not role.captain and not role.organizer:
        return "Only this lobby's captains or an organizer can give the bot its room code."
    return _not_ready_for_code(tournament)


def submit_room_code(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby, role: Role, user_id: int,
                     code: str) -> str:
    problem = room_code_problem(conn, tournament, lobby, role)
    if problem is not None:
        return problem
    code = code.strip()
    if not code or len(code) > ROOM_CODE_MAX_LENGTH:
        return f"Enter the room code shown on {DRAFTER_HOST_URL} after you create the room."
    lobby_db.set_room_code(conn, lobby.id, code, user_id)
    return f"✅ Saved room code **{code}** for lobby {lobby.number}.{_name_only_note(conn, lobby)}"


def get_room_code(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby, role: Role) -> str:
    if role.team_id is None and not role.organizer:
        return "The room code is only for players in this lobby."
    problem = _not_ready_for_code(tournament)
    if problem is not None:
        return problem
    if lobby.room_code is None:
        return "The room code isn't in yet. A captain enters it once they've created the room; check back in a moment."
    text = (f"🎮 Room code for lobby {lobby.number}: **{lobby.room_code}**\nJoin at https://drafter.uma.guide with that "
            f"code.")
    if role.captain or role.organizer:
        text += _name_only_note(conn, lobby)
    return text


def report_problem(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby, role: Role) -> Optional[str]:
    """Why someone can't report `lobby`'s points, or None if they can."""
    if not role.captain and not role.organizer:
        return "Only this lobby's captains or an organizer can report its points."
    if not tournament["checkin_finished"]:
        return "Points are reported after the races, once check-in is over."
    if not all(lobby.teams[side].players for side in SIDES):
        return "One of this lobby's teams has nobody left on it, so an organizer has to sort the lobby out first."
    if lobby_db.get_result(conn, lobby.id) is not None and not role.organizer:
        return "This lobby's result is already in. Ask an organizer if it needs correcting."
    return None


def parse_points(lobby: Lobby, entered: dict[int, str]) -> dict[int, int] | str:
    """Everyone's points from what was typed, or why they can't be used."""
    points = {}
    for player in lobby.players:
        text = entered.get(player, "").strip()
        if not text.isdigit() or int(text) > MAX_POINTS:
            return "Enter each player's points as a whole number (0 if they scored none)."
        points[player] = int(text)
    if lobby_db.winning_side(lobby, points) is None:
        return ("The teams are level on points. Play the tiebreaker track first, then report everyone's points with "
                "the tiebreaker's included.")
    return points


def submit_report(conn: sqlite3.Connection, tournament: sqlite3.Row, lobby: Lobby, role: Role, user_id: int,
                  points: dict[int, int]) -> str:
    """Save a captain's report for the other captain to confirm, or record an organizer's
    at once."""
    problem = report_problem(conn, tournament, lobby, role)
    if problem is not None:
        return problem
    if role.organizer and not role.captain:
        lobby_db.record_result(conn, lobby.id, points, user_id)
        return f"✅ Recorded the points for lobby {lobby.number}."
    other = "B" if role.side == "A" else "A"
    if lobby.teams[other].captain is None:
        lobby_db.record_result(conn, lobby.id, points, user_id)
        return (f"✅ Recorded the points for lobby {lobby.number}. The other team has no captain to confirm them, "
                f"so an organizer can correct them if needed.")
    lobby_db.submit_report(conn, lobby.id, user_id, role.side, points)
    report = lobby_db.get_report(conn, lobby.id)
    return (f"✅ Sent to the other captain. To correct the points before they're accepted "
            f"<t:{int(report_due_at(report).timestamp())}:R>, press **{REPORT_RESULT}** again.")


def _answerable_report(conn: sqlite3.Connection, lobby: Lobby, role: Role) -> lobby_db.Report | str:
    report = lobby_db.get_report(conn, lobby.id)
    if report is None:
        return "There's no report waiting for an answer in this lobby."
    if not role.organizer and (not role.captain or role.side == report.reporting_side):
        return "Only the other team's captain can answer this report."
    return report


def confirm_report(conn: sqlite3.Connection, lobby: Lobby, role: Role, user_id: int) -> str:
    report = _answerable_report(conn, lobby, role)
    if isinstance(report, str):
        return report
    if report.disputed and not role.organizer:
        return "You disputed these points, so an organizer settles them now."
    if not lobby_db.accept_report(conn, report, user_id):
        return "The report changed while you were looking at it. Check the event's post and answer again."
    return f"✅ Confirmed. The result for lobby {lobby.number} is in."


def dispute_report(conn: sqlite3.Connection, lobby: Lobby, role: Role) -> str:
    report = _answerable_report(conn, lobby, role)
    if isinstance(report, str):
        return report
    if report.disputed:
        return "These points are already disputed. An organizer will settle them."
    if not lobby_db.dispute_report(conn, report):
        return "The report changed while you were looking at it. Check the event's post and answer again."
    return f"✅ Disputed the points for lobby {lobby.number}."


def accept_due_reports(conn: sqlite3.Connection, now: datetime) -> None:
    """Accept every report the other captain hasn't answered in time."""
    for report in lobby_db.get_open_reports(conn):
        if report_due_at(report) <= now:
            try:
                lobby_db.accept_report(conn, report, None)
            except Exception:
                log.exception("Couldn't accept the report for lobby %s", report.lobby_id)


def tend(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[int]:
    """The timed steps of every 5v5 event: ending check-in and accepting unanswered
    reports. Returns the events whose check-in it ended."""
    finished = finish_due_checkins(conn)
    accept_due_reports(conn, now or datetime.now(timezone.utc))
    return finished


def lobby_summary_lines(conn: sqlite3.Connection, tournament: sqlite3.Row) -> list[str]:
    """One line per lobby for the conclude post and the history: who won, and its top scorer."""
    names = Names(conn, tournament["id"], str)
    lines = []
    for lobby in lobby_db.get_lobbies(conn, tournament["id"]):
        result = lobby_db.get_result(conn, lobby.id)
        if result is None:
            lines.append(f"Lobby {lobby.number}: no result")
            continue
        totals = {side: sum(result.points.get(p, 0) for p in lobby.teams[side].players) for side in SIDES}
        loser = "B" if result.winning_side == "A" else "A"
        best = max(result.points.values())
        top = ", ".join(names.plain(p) for p, value in result.points.items() if value == best)
        lines.append(f"Lobby {lobby.number}: 🏆 {team_name(lobby, result.winning_side, names)} "
                     f"{totals[result.winning_side]}-{totals[loser]} · top scorer {top} ({best})")
    return lines


def lobby_signups_message(tournament: sqlite3.Row, entrants: int) -> str:
    """A 5v5 event's sign-ups announcement, which carries Register and Unregister buttons
    until the waitlist stops taking late sign-ups."""
    name = tournament["name"]
    count = f"**{entrants}** signed up so far."
    if tournament["status"] == "registration":
        starts = db.from_db_time(tournament["starts_at"]) if tournament["starts_at"] else None
        when = f" Play starts <t:{int(starts.timestamp())}:F> (<t:{int(starts.timestamp())}:R>)." if starts else ""
        return (f"📝 Sign-ups are open for **{name}**, a 5v5 event!{when} Press **Register** below (or run "
                f"{register_command(name)}) and say whether you'll captain. Lobbies and teams are drawn ahead of "
                f"time, and everyone is pinged here with their lobby or their place on the waitlist. {count}")
    if takes_late_signups(tournament):
        return (f"📝 The lobbies for **{name}** are drawn: see the post below. Pressing **Register** now puts you on a "
                f"team that has room, or on the waitlist if none does. {count}")
    return f"📝 Sign-ups for **{name}** are closed."


def entrant_lines(conn: sqlite3.Connection, tournament: sqlite3.Row, player_label) -> list[str]:
    """A 5v5 event's sign-ups, with each one's captain choice and where they stand."""
    lobby_of = {player: lobby.number for lobby in lobby_db.get_lobbies(conn, tournament["id"]) for player in lobby.players}
    waitlist = lobby_db.get_waitlist(conn, tournament["id"])
    lines = []
    for number, signup in enumerate(lobby_db.get_signups(conn, tournament["id"]), start=1):
        account = "no Discord account" if signup.user_id is None else player_label(signup.user_id)
        parts = [f"{number}. {signup.name} ({account})", CAPTAIN_CHOICES[signup.captain_pref].lower()]
        if signup.team_id in lobby_of:
            parts.append(f"lobby {lobby_of[signup.team_id]}")
        elif signup.team_id in waitlist:
            parts.append(f"waitlist #{waitlist.index(signup.team_id) + 1}")
        elif signup.withdrawn:
            parts.append("left")
        elif tournament["status"] != "registration":
            parts.append("missed check-in")
        if lobby_db.checkin_running(tournament) and signup.checked_in:
            parts.append("checked in")
        lines.append(" · ".join(parts))
    return lines


def history_lines(conn: sqlite3.Connection, tournament: sqlite3.Row, player_label) -> list[str]:
    """A concluded 5v5 event lobby by lobby: who won, and everyone's points."""
    names = Names(conn, tournament["id"], player_label)
    lines = []
    for summary, lobby in zip(lobby_summary_lines(conn, tournament), lobby_db.get_lobbies(conn, tournament["id"])):
        result = lobby_db.get_result(conn, lobby.id)
        lines += [f"**{summary}**"] + team_lines(lobby, names, False, result.points if result else None) + [""]
    return lines
