"""Changes organizers make to a bracket, shared by the cogs and the CLI: withdrawing an
entrant, calling a match off, and setting or clearing a result, each with the public lines
that announce it. Those lines name the organizer, so everyone can see who changed the event."""

from __future__ import annotations

import sqlite3
from typing import Callable

import db
from bracket import Match, Outcomes, bracket_order, resolve_all_slots
from labels import (
    advance_text,
    called_off_line,
    later_results_first,
    matchup_label,
    result_changed_line,
    result_cleared_line,
    result_line,
    walkover_lines,
    withdrawal_line,
)

# Withdrawing only makes sense while there are matches left to hand over. A withdrawal
# can complete the tournament, so reinstating has to work once it's completed too.
WITHDRAWABLE_STATUSES = ("setup", "predictions_open", "in_progress")
REINSTATABLE_STATUSES = WITHDRAWABLE_STATUSES + ("completed",)


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
    return line, dropped


def clear_result_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, match: Match, teams: tuple[int, int],
                              cleared_by: int, user_label: Callable[[int], str]) -> tuple[str, list[int]]:
    """Clear `match`'s result or skip for the organizer `cleared_by`. Returns the public
    line announcing it and the ids of later results dropped."""
    was_skipped = match.id in db.get_outcomes(conn, tournament["id"]).skipped
    dropped = db.clear_result(conn, tournament["id"], match.id)
    line = result_cleared_line(match, teams, db.get_teams(conn, tournament["id"]), user_label(cleared_by), was_skipped)
    return line, dropped


def later_results_problem(conn: sqlite3.Connection, tournament: sqlite3.Row, match_ids) -> str | None:
    """Why the results of `match_ids` can't change yet, or None if they can. A later match
    that depends on one of them and has a result, or was called off, has to be cleared
    first, so no result is left standing on a matchup that wasn't played."""
    blocking = {later for match_id in match_ids for later in db.stored_later_results(conn, tournament["id"], match_id)}
    if not blocking:
        return None
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    slots = resolve_all_slots(matches_by_id, db.get_outcomes(conn, tournament["id"]).winners.get, feeders)
    latest_first = sorted((matches_by_id[match_id] for match_id in blocking), key=bracket_order, reverse=True)
    return later_results_first([matchup_label(match, slots, teams_by_id) for match in latest_first])


def reinstate_problem(conn: sqlite3.Connection, tournament: sqlite3.Row, team_id: int) -> str | None:
    """Why `team_id` can't be reinstated yet, or None if it can: the matches it gave away
    by walkover go back to being unplayed, so the same rule applies to them."""
    matches_by_id, feeders, _ = db.load_bracket(conn, tournament["id"])
    outcomes = db.get_outcomes(conn, tournament["id"])
    slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
    given_away = [match_id for match_id in outcomes.unplayed if team_id in slots[match_id]]
    return later_results_problem(conn, tournament, given_away)
