"""Changes to who plays on in a bracket, shared by the cogs and the CLI: withdrawing an
entrant and calling a match off, each with the public lines that announce it."""

from __future__ import annotations

import sqlite3
from typing import Callable

import db
from bracket import Outcomes
from labels import called_off_line, walkover_lines, withdrawal_line

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
                      skipped_by: int) -> tuple[list[str], list[int]]:
    """Call a match off. Returns the public lines announcing it, with the walkovers it
    gave, and the ids of results dropped."""
    before = db.get_outcomes(conn, tournament["id"])
    dropped = db.skip_match(conn, tournament["id"], match_id, teams, skipped_by)
    headline = called_off_line(db.get_matches(conn, tournament["id"])[match_id], teams, db.get_teams(conn, tournament["id"]))
    return [headline] + new_walkovers(conn, tournament, before), dropped


def new_walkovers(conn: sqlite3.Connection, tournament: sqlite3.Row, before: Outcomes) -> list[str]:
    """Lines announcing the walkovers given since `before`."""
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    return walkover_lines(before, db.get_outcomes(conn, tournament["id"]), matches_by_id, feeders, teams_by_id,
                          tournament["name"])
