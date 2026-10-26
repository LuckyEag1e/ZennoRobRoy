"""Turning an ordered list of entrants into round-1 pairs.

No discord.py or sqlite3 imports here, like bracket.py, so it can be unit tested on its own.
"""

from __future__ import annotations

from typing import Optional

from bracket import BracketError


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
