"""Changes organizers make to a bracket, shared by the cogs and the CLI: withdrawing an
entrant, calling a match off, and setting or clearing a result, each with the public lines
that announce it. Those lines name the organizer, so everyone can see who changed the event."""

from __future__ import annotations

import sqlite3
from typing import Callable

import db
from bracket import Match, Outcomes
from labels import (
    LATER_RESULTS_CLEARED,
    advance_text,
    called_off_line,
    result_changed_line,
    result_cleared_line,
    result_line,
    walkover_lines,
    withdrawal_line,
)

# Withdrawing only makes sense while there are matches left to hand over.
WITHDRAWABLE_STATUSES = ("setup", "predictions_open", "in_progress")


def withdraw_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, team: sqlite3.Row, reason: str,
                          withdrawn_by: int, user_label: Callable[[int], str]) -> tuple[list[str], list[int]]:
    """Withdraw an entrant on behalf of the user `withdrawn_by`, named in the announcement
    with `user_label`. Returns the public lines announcing it, with the walkovers it gave,
    and the ids of results dropped."""
    before = db.get_outcomes(conn, tournament["id"])
    dropped = db.withdraw_entrant(conn, tournament["id"], team["id"], reason, withdrawn_by)
    line = withdrawal_line(team["name"], reason, tournament["name"], user_label(withdrawn_by))
    return [line] + new_walkovers(conn, tournament, before), dropped


def skip_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, match_id: int, teams: tuple[int, int],
                      skipped_by: int, user_label: Callable[[int], str]) -> tuple[list[str], list[int]]:
    """Call a match off on behalf of the organizer `skipped_by`, named in the announcement
    with `user_label`. Returns the public lines announcing it, with the walkovers it gave,
    and the ids of results dropped."""
    before = db.get_outcomes(conn, tournament["id"])
    dropped = db.skip_match(conn, tournament["id"], match_id, teams, skipped_by)
    headline = called_off_line(db.get_matches(conn, tournament["id"])[match_id], teams, db.get_teams(conn, tournament["id"]),
                               user_label(skipped_by))
    return [headline] + new_walkovers(conn, tournament, before), dropped


def new_walkovers(conn: sqlite3.Connection, tournament: sqlite3.Row, before: Outcomes) -> list[str]:
    """Lines announcing the walkovers given since `before`."""
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    return walkover_lines(before, db.get_outcomes(conn, tournament["id"]), matches_by_id, feeders, teams_by_id,
                          tournament["name"])


def set_result_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, match: Match, winner: int, loser: int,
                            recorded_by: int, user_label: Callable[[int], str]) -> tuple[str, list[int]]:
    """Record the organizer `recorded_by`'s result for `match`, replacing any it had.
    Returns the public line announcing it and the ids of later results dropped."""
    replacing = match.id in db.get_outcomes(conn, tournament["id"]).decided
    dropped = db.record_result(conn, tournament["id"], match.id, winner, recorded_by)
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    advance = advance_text(match, winner, matches_by_id, feeders, db.get_outcomes(conn, tournament["id"]),
                           teams_by_id, tournament["name"])
    describe = result_changed_line if replacing else result_line
    line = describe(match, winner, loser, teams_by_id, advance, user_label(recorded_by))
    return line + (f" {LATER_RESULTS_CLEARED}" if dropped else ""), dropped


def clear_result_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, match: Match, teams: tuple[int, int],
                              cleared_by: int, user_label: Callable[[int], str]) -> tuple[str, list[int]]:
    """Clear `match`'s result or skip for the organizer `cleared_by`. Returns the public
    line announcing it and the ids of later results dropped."""
    was_skipped = match.id in db.get_outcomes(conn, tournament["id"]).skipped
    dropped = db.clear_result(conn, tournament["id"], match.id)
    line = result_cleared_line(match, teams, db.get_teams(conn, tournament["id"]), user_label(cleared_by), was_skipped)
    return line + (f" {LATER_RESULTS_CLEARED}" if dropped else ""), dropped
