"""Putting entrants in seed order, and turning that order into round-1 pairs.

No discord.py or sqlite3 imports here, like bracket.py, so it can be unit tested on its own.
"""

from __future__ import annotations

import random
from typing import Optional

from bracket import BracketError
from ratings import BASE_RATING, Ratings, entrant_rating


def bracket_positions(size: int) -> list[int]:
    """Seed numbers in top-to-bottom bracket order for a bracket of `size` lines (a power
    of two), so that seeds 1 and 2 can only meet in the final: 1, 8, 4, 5, 2, 7, 3, 6."""
    order = [1]
    while len(order) < size:
        lines = len(order) * 2
        order = [line_seed for seed in order for line_seed in (seed, lines + 1 - seed)]
    return order


def seeded_pairs(ordered_team_ids: list[int]) -> list[tuple[Optional[int], Optional[int]]]:
    """Round-1 pairs for teams listed best seed first. The bracket is padded up to a power
    of two, and the empty lines become byes for the top seeds."""
    if len(ordered_team_ids) < 2:
        signed_up = "nobody has signed up yet" if not ordered_team_ids else "only 1 has signed up so far"
        raise BracketError(f"A bracket needs at least 2 entrants, and {signed_up}.")
    size = 2
    while size < len(ordered_team_ids):
        size *= 2
    lines = [
        ordered_team_ids[seed - 1] if seed <= len(ordered_team_ids) else None
        for seed in bracket_positions(size)
    ]
    return list(zip(lines[::2], lines[1::2]))


def place_new_entrants(rated: dict[int, float], unrated: list[int], rng: random.Random) -> list[int]:
    """Every entrant, best seed first, given the ratings of those who have played before.
    Players with no rating yet count as an average player, so a newcomer isn't seeded
    below members who have been losing. Equal ratings are put in random order."""
    rating_of = dict(rated) | {team_id: BASE_RATING for team_id in unrated}
    tiebreak = {team_id: rng.random() for team_id in sorted(rating_of)}
    return sorted(rating_of, key=lambda team_id: (-rating_of[team_id], tiebreak[team_id]))


def rating_order(members_by_team: dict[int, list[int]], ratings: Ratings, rng: random.Random) -> list[int]:
    """Teams best seed first by Elo. `members_by_team` lists each team's Discord users,
    empty for an entrant with no account."""
    rated, unrated = {}, []
    for team_id, member_ids in sorted(members_by_team.items()):
        rating = entrant_rating(member_ids, ratings)
        if rating is None:
            unrated.append(team_id)
        else:
            rated[team_id] = rating
    return place_new_entrants(rated, unrated, rng)
