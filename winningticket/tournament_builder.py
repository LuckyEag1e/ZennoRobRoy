"""Building a tournament's match tree, whether its teams came from a CSV or from sign-ups,
and drawing a 5v5 event's sign-ups into lobbies."""

from __future__ import annotations

import random
import sqlite3
from typing import Optional

import db
import lobbies
import lobby_db
from bracket import BracketError, generate_tree
from contribution import contributions, entrant_contributions
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
    `seeding_mode` overrides the one chosen when sign-ups opened: "standard" and "slide"
    seed entrants by their Elo from past tournaments (see seeding.seeded_pairs), and
    "random" draws them in random order with no seeds.
    Returns how many teams were drawn. Raises BracketError if too few signed up, and
    RegistrationClosedError if it was already closed. On any error nothing is saved, so
    closing can simply be tried again."""
    rng = rng or random  # the module itself, so seeding it (as the simulator does) makes draws repeatable
    tournament = db.get_tournament(conn, tournament_id)
    if tournament is None:
        raise RegistrationClosedError(f"Tournament {tournament_id} no longer exists.")
    seeding_mode = seeding_mode or tournament["seeding_mode"]
    # Replayed before the close starts writing, so the database isn't locked meanwhile.
    ratings = None if seeding_mode == "random" else compute_ratings(db.load_match_history(conn, tournament["guild_id"]))
    with conn:
        if not db.claim_registration_close(conn, tournament_id):
            raise RegistrationClosedError(f"Registration for tournament {tournament_id} has already closed.")
        members_by_team = db.get_members_by_team(conn, tournament_id)
        db.set_seeding_mode(conn, tournament_id, seeding_mode)  # the one used, for the history
        if ratings is None:
            order = rng.sample(sorted(members_by_team), len(members_by_team))
        else:
            order = rating_order(members_by_team, ratings, rng)
            db.set_team_seeds(conn, order)
        build_matches(conn, tournament_id, seeded_pairs(order, slide=seeding_mode == "slide"))
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


def lobby_split_options(conn: sqlite3.Connection, tournament_id: int) -> tuple[Optional[lobbies.Split], list[lobbies.Split]]:
    """The split a 5v5 event's close would propose for everyone signed up now, and every
    other split the organizer can choose instead."""
    signups = lobby_db.get_signups(conn, tournament_id)
    captains = sum(signup.can_captain for signup in signups)
    return lobbies.propose_split(len(signups), captains), lobbies.possible_splits(len(signups), captains)


def close_lobby_registration(conn: sqlite3.Connection, tournament_id: int, split: lobbies.Split | None = None,
                             spread: str | None = None, rng: random.Random | None = None) -> lobbies.Split:
    """Draw a 5v5 event's sign-ups into lobbies and move it to in_progress. `split` and
    `spread` default to the proposed split and the spread chosen at open. Players are
    balanced by their contribution in past events (contribution.py). Raises LobbyError if
    there aren't enough players or captains, and RegistrationClosedError if it was
    already closed; either way nothing is saved."""
    rng = rng or random
    tournament = db.get_tournament(conn, tournament_id)
    if tournament is None:
        raise RegistrationClosedError(f"Tournament {tournament_id} no longer exists.")
    proposed, options = lobby_split_options(conn, tournament_id)
    split = split or proposed
    if split is None or split not in options:
        raise lobbies.LobbyError("There aren't enough players who can captain for that many lobbies: each needs two.")
    spread = spread or tournament["lobby_spread"] or "balanced"
    signups = lobby_db.get_signups(conn, tournament_id)
    prefs = lobby_db.captain_prefs(signups)
    by_user = contributions(lobby_db.load_lobby_history(conn, tournament["guild_id"]))
    values = entrant_contributions({s.team_id: s.user_id for s in signups}, by_user)
    placed, waiting = lobbies.choose_waitlist([s.team_id for s in signups], prefs, split)
    drawn = lobbies.ensure_captains(lobbies.assign_lobbies(placed, values, split, spread, rng), prefs, values)
    formed = [lobbies.form_teams(players, prefs, lobbies.team_sizes(len(players)), values, rng) for players in drawn]
    with conn:
        if not db.claim_registration_close(conn, tournament_id):
            raise RegistrationClosedError(f"Registration for tournament {tournament_id} has already closed.")
        conn.execute("UPDATE tournaments SET status = 'in_progress', lobby_spread = ? WHERE id = ?",
                     (spread, tournament_id))
        lobby_db.form_lobbies(conn, tournament_id, formed, waiting)
        if tournament["channel_id"] is not None:
            lobby_db.add_event_post(conn, tournament_id, tournament["channel_id"])
    return split
