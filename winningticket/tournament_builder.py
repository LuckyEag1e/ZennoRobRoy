"""Building a tournament's match tree, whether its teams came from a CSV or from sign-ups."""

from __future__ import annotations

import random
import sqlite3
from typing import Optional

import db
from bracket import BracketError, generate_tree
from ratings import compute_ratings
from seeding import rating_order, seeded_pairs


# How far ahead a sign-up deadline can be set, in hours.
MIN_CLOSES_IN_HOURS = 0.01
MAX_CLOSES_IN_HOURS = 1440.0


class RegistrationClosedError(Exception):
    """Raised when closing a tournament that has already left registration."""


def build_matches(conn: sqlite3.Connection, tournament_id: int,
                  team_pairs: list[tuple[Optional[int], Optional[int]]]) -> None:
    """Add the full single-elimination tree for round-1 pairs of existing team ids. Raises
    BracketError, before adding anything, if the pairs can't form a bracket. It doesn't
    commit, so the caller can save the tree together with whatever goes with it."""
    specs = generate_tree(team_pairs)

    match_ids_by_round_slot: dict[tuple[int, int], int] = {}
    for spec in specs:
        if spec.round == 1:
            team_a_id, team_b_id = team_pairs[spec.slot_index]
        else:
            team_a_id, team_b_id = None, None
        match_id = db.insert_match_spec(conn, tournament_id, spec, team_a_id, team_b_id)
        match_ids_by_round_slot[(spec.round, spec.slot_index)] = match_id
    db.link_feeders(conn, tournament_id, specs, match_ids_by_round_slot)


def close_registration(conn: sqlite3.Connection, tournament_id: int, rng: random.Random | None = None,
                       seeding_mode: str | None = None) -> int:
    """Draw the signed-up teams into a bracket and move the tournament on to setup.
    `seeding_mode` overrides the one chosen when sign-ups opened: "auto" seeds teams by
    their Elo from past tournaments, and "random" draws them in random order with no seeds.
    Returns how many teams were drawn. Raises BracketError if too few signed up, and
    RegistrationClosedError if it was already closed. On any error nothing is saved, so
    closing can simply be tried again."""
    rng = rng or random  # the module itself, so seeding it (as the simulator does) makes draws repeatable
    with conn:
        if not db.claim_registration_close(conn, tournament_id):
            raise RegistrationClosedError(f"Registration for tournament {tournament_id} has already closed.")
        tournament = db.get_tournament(conn, tournament_id)
        members_by_team = db.get_members_by_team(conn, tournament_id)
        if (seeding_mode or tournament["seeding_mode"]) == "auto":
            ratings = compute_ratings(db.load_match_history(conn, tournament["guild_id"]))
            order = rating_order(members_by_team, ratings, rng)
            db.set_team_seeds(conn, order)
        else:
            order = rng.sample(sorted(members_by_team), len(members_by_team))
        build_matches(conn, tournament_id, seeded_pairs(order))
    return len(order)


def close_overdue_registration(conn: sqlite3.Connection, tournament_id: int, seeding_mode: str | None = None) -> int:
    """close_registration for a tournament whose deadline has passed. If too few signed
    up, the deadline is dropped so it isn't retried on every check, and the BracketError
    is raised for the caller to report. Sign-ups then stay open until closed by hand."""
    try:
        return close_registration(conn, tournament_id, seeding_mode=seeding_mode)
    except BracketError:
        db.clear_registration_deadline(conn, tournament_id)
        raise
