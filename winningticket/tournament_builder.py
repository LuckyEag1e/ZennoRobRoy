"""Building a tournament's match tree, whether its teams came from a CSV or from sign-ups."""

from __future__ import annotations

import random
import sqlite3
from typing import Optional

import db
from bracket import generate_tree
from seeding import seeded_pairs


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


def close_registration(conn: sqlite3.Connection, tournament_id: int, rng: random.Random | None = None) -> int:
    """Draw the signed-up teams into a bracket in random order and move the tournament on
    to setup. Returns how many teams were drawn. Raises BracketError if too few signed
    up. On any error nothing is saved, so closing can simply be tried again."""
    team_ids = sorted({entrant["team_id"] for entrant in db.get_entrants(conn, tournament_id)})
    pairs = seeded_pairs((rng or random).sample(team_ids, len(team_ids)))
    with conn:
        build_matches(conn, tournament_id, pairs)
        db.set_tournament_status(conn, tournament_id, "setup")
    return len(team_ids)
