"""Calling matches in tournaments that coordinate them: calling the next matches as places
free up, readying up, reminders and forfeits for sides that don't ready, and handing out
rooms. Shared by the bot's background loop and the CLI. Neither posts anything here: they
bring each match's post up to date afterwards (match_posts)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import db
from bracket import bracket_order, resolvable_matches, resolve_all_slots

# A side that hasn't readied up is reminded after this long, and forfeits once the
# reminder has been up this long.
READY_REMINDER_AFTER = timedelta(minutes=5)
READY_FORFEIT_AFTER = timedelta(minutes=5)


def needs_ready_up(conn: sqlite3.Connection, teams: tuple[int, int]) -> bool:
    """Only a match with Discord users on both sides waits for them to ready up."""
    return all(db.team_member_ids(conn, team) for team in teams)


def forfeit_deadline(call: sqlite3.Row) -> datetime:
    reminded = (db.from_db_time(call["reminded_at"]) if call["reminded_at"] is not None
                else db.from_db_time(call["called_at"]) + READY_REMINDER_AFTER)
    return reminded + READY_FORFEIT_AFTER


def tend(conn: sqlite3.Connection, tournament: sqlite3.Row, now: datetime, rooms: list[int]) -> None:
    """Move a coordinated tournament's calls along as of `now`: remind and then forfeit
    sides that haven't readied, give started matches a free one of `rooms`, and call the
    next matches while places are free."""
    for call in db.get_calls(conn, tournament["id"]):
        # A call's post can end early, when it was about an earlier matchup, so each call
        # gets a new one if it has none.
        if tournament["channel_id"] is not None:
            db.claim_match_post(conn, call["match_id"], tournament["channel_id"], (call["team_a_id"], call["team_b_id"]))
        if call["started_at"] is None:
            if call["reminded_at"] is None:
                if now >= db.from_db_time(call["called_at"]) + READY_REMINDER_AFTER:
                    db.mark_reminded(conn, call["match_id"])
            elif now >= forfeit_deadline(call):
                db.forfeit_match(conn, tournament["id"], call)
        elif call["room_id"] is None:
            db.give_room(conn, tournament["guild_id"], call["match_id"], rooms)

    # Every called match needs a room once it starts, and the server's tournaments share the
    # rooms, so a match is only called while both the tournament and the server have room.
    places = min(tournament["match_slots"] - len(db.get_calls(conn, tournament["id"])),
                 len(rooms) - db.count_server_calls(conn, tournament["guild_id"]))
    if places <= 0 or tournament["channel_id"] is None:
        return
    matches_by_id, feeders, _ = db.load_bracket(conn, tournament["id"])
    outcomes = db.get_outcomes(conn, tournament["id"])
    slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
    called = {call["match_id"] for call in db.get_calls(conn, tournament["id"])}
    ready = resolvable_matches(matches_by_id, feeders, outcomes.winners.get, outcomes.decided)
    for match in sorted((m for m in ready if m.id not in called), key=bracket_order)[:places]:
        _call(conn, tournament, match.id, slots[match.id], rooms)


def _call(conn: sqlite3.Connection, tournament: sqlite3.Row, match_id: int, teams: tuple[int, int],
          rooms: list[int]) -> None:
    if not db.call_match(conn, match_id, teams):
        return
    db.claim_match_post(conn, match_id, tournament["channel_id"], teams)
    if not needs_ready_up(conn, teams):
        _start(conn, tournament, match_id, rooms)


def _start(conn: sqlite3.Connection, tournament: sqlite3.Row, match_id: int, rooms: list[int]) -> None:
    if db.start_match(conn, match_id):
        db.give_room(conn, tournament["guild_id"], match_id, rooms)


def ready_up_for(conn: sqlite3.Connection, tournament: sqlite3.Row, user_id: int, rooms: list[int]) -> list[int]:
    """Ready up `user_id`'s side in each of the tournament's called matches whose call has
    been posted, as if they'd pressed Ready Up. Returns the matches readied."""
    readied = []
    for call in db.get_calls(conn, tournament["id"]):
        post = db.get_match_post(conn, call["match_id"])
        if call["started_at"] is not None or post is None or post["message_id"] is None:
            continue
        sides = (call["team_a_id"], call["team_b_id"])
        team = next((side for side in sides if user_id in db.team_member_ids(conn, side)), None)
        if team is not None and team not in db.get_ready_teams(conn, call["match_id"]):
            ready_up(conn, call["match_id"], user_id, rooms)
            readied.append(call["match_id"])
    return readied


def ready_up(conn: sqlite3.Connection, match_id: int, user_id: int, rooms: list[int]) -> str:
    """Ready up the side `user_id` plays on, starting the match once both sides are ready.
    Returns what to tell them privately."""
    call = db.get_call(conn, match_id)
    if call is None:
        return "This match isn't waiting for anyone to ready up any more."
    teams = (call["team_a_id"], call["team_b_id"])
    team_id = next((team for team in teams if user_id in db.team_member_ids(conn, team)), None)
    if team_id is None:
        return "This isn't your match."
    if call["started_at"] is not None:
        return "This match has already started."
    tournament = db.get_tournament(conn, db.get_match_tournament_id(conn, match_id))
    if tournament["status"] != "in_progress":
        return f"**{tournament['name']}** isn't being played right now."
    if not db.ready_up(conn, match_id, team_id, user_id):
        return "Your side is already ready."
    if db.get_ready_teams(conn, match_id) == set(teams):
        _start(conn, tournament, match_id, rooms)
        return "You're ready, and so is your opponent. The match is starting."
    return "You're ready. Waiting for your opponent."
