"""What a match's one public post should say as the match goes along: its call and ready
up, where it's played, warnings and disputes about votes, and finally how it ended.
Shared by the bot and the CLI, which do the posting."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

import db
from bracket import resolve_slot
from coordination import forfeit_deadline
from labels import (
    called_off_line,
    match_call,
    match_off,
    match_playing,
    match_waiting_for_room,
    report_concession,
    report_dispute,
    report_waiting,
    report_warning,
    report_withdrawn,
    team_label,
)
from reports import answer_deadline, ending_text, opponent, win_due_at

# The `ping` values of posts that called a match, rather than only reporting on votes.
CALL_PINGS = ("call", "reminder", "start")


@dataclass
class PostState:
    text: str
    final: bool  # it shows how the match ended, so the post needs no more changes
    # What the text notifies people about. When it changes, the post is sent afresh, since
    # editing a message doesn't notify anyone it mentions. None keeps the message as it is.
    ping: Optional[str]
    ready_button: bool
    # Players can vote on the result from the post, once the bot has started their match.
    report_button: bool = False


def describe(conn: sqlite3.Connection, post: sqlite3.Row, player_label: Callable[[int], str],
             room_label: Callable[[int], str]) -> PostState:
    """What `post` should show now. `player_label` and `room_label` name a user and a room,
    by mention in Discord."""
    tournament = db.get_tournament(conn, post["tournament_id"])
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    outcomes = db.get_outcomes(conn, tournament["id"])
    match = matches_by_id[post["match_id"]]
    teams = (post["team_a_id"], post["team_b_id"])

    def call_out(sides) -> str:
        """The sides' players, by mention, or a side's name when nobody is linked to it."""
        return " and ".join(" ".join(player_label(user_id) for user_id in db.team_member_ids(conn, team))
                            or f"**{team_label(teams_by_id, team)}**" for team in sides)

    if set(resolve_slot(match, matches_by_id, outcomes.winners.get, feeders)) != set(teams):
        # An earlier result changed who's in the match, so this post is about a matchup
        # that won't be played. A new call gets a post of its own.
        if post["pinged"] in CALL_PINGS:
            return PostState(match_off(match, teams, teams_by_id), True, None, False)
        return PostState(report_withdrawn(match, teams, teams_by_id), True, None, False)
    if match.id in outcomes.decided:
        text = ending_text(conn, tournament, match) or called_off_line(match, teams, teams_by_id)
        return PostState(text, True, None, False)

    votes = db.get_reports(conn, match.id)
    call = db.get_call(conn, match.id)
    playing = call is not None and call["room_id"] is not None
    if len(votes) == 2:
        return PostState(report_dispute(match, teams, teams_by_id), False, None, False)
    if len(votes) == 1:
        vote = votes[0]
        silent = opponent(teams, vote["team_id"])
        if post["warned_at"] is not None and vote["winner_team_id"] != vote["team_id"]:
            text = report_concession(match, vote["team_id"], vote["winner_team_id"], answer_deadline(post),
                                     teams_by_id)
            return PostState(text, False, None, False, playing)
        if post["warned_at"] is not None:
            text = report_warning(match, vote["team_id"], call_out([silent]), answer_deadline(post), teams_by_id)
            return PostState(text, False, f"warning:{silent}", False, playing)
        if call is None or call["started_at"] is not None:
            text = report_waiting(match, vote["team_id"], vote["winner_team_id"], silent, win_due_at(vote, post),
                                  teams_by_id)
            return PostState(text, False, None, False, playing)

    if call is not None:
        if call["started_at"] is None:
            ready = db.get_ready_teams(conn, match.id)
            reminder = call["reminded_at"] is not None
            asked = [team for team in teams if team not in ready] if reminder else list(teams)
            text = match_call(match, teams, ready, call_out(asked), forfeit_deadline(call), reminder, teams_by_id)
            return PostState(text, False, "reminder" if reminder else "call", True)
        if call["room_id"] is None:
            return PostState(match_waiting_for_room(match, teams, teams_by_id), False, None, False)
        needs_organizer = not all(db.team_member_ids(conn, team) for team in teams)
        text = match_playing(match, teams, room_label(call["room_id"]), call_out(teams), needs_organizer, teams_by_id)
        return PostState(text, False, "start", False, not needs_organizer)

    # Nothing about the match is waiting any more, though it isn't decided.
    if post["pinged"] in CALL_PINGS:
        return PostState(match_off(match, teams, teams_by_id), True, None, False)
    return PostState(report_withdrawn(match, teams, teams_by_id), True, None, False)
