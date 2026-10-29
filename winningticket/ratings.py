"""Elo ratings worked out from past tournaments' results, used to seed new brackets.

No discord.py or sqlite3 imports here, like bracket.py, so it can be unit tested on its own.
Ratings are never stored: they're replayed from the full history whenever they're needed,
so linking an old entrant to a member changes them straight away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Optional

BASE_RATING = 1500.0
K_FACTOR = 32


@dataclass(frozen=True)
class PlayedMatch:
    """A decided game from a past tournament, as the Discord users on each side."""

    winners: tuple[int, ...]
    losers: tuple[int, ...]


@dataclass
class Ratings:
    """Each user's singles and team Elo. Users missing from a table haven't played that
    kind of game yet, so their rating there is provisional."""

    singles: dict[int, float] = field(default_factory=dict)
    team: dict[int, float] = field(default_factory=dict)

    def singles_rating(self, user_id: int) -> float:
        return self.singles.get(user_id, BASE_RATING)

    def team_rating(self, user_id: int) -> float:
        """A player new to team games starts from their singles rating."""
        return self.team.get(user_id, self.singles_rating(user_id))

    def has_played(self, user_id: int) -> bool:
        return user_id in self.singles or user_id in self.team


def expected_score(rating: float, opponent: float) -> float:
    """The chance Elo gives `rating` of beating `opponent`."""
    return 1 / (1 + 10 ** ((opponent - rating) / 400))


def compute_ratings(history: list[PlayedMatch]) -> Ratings:
    """Replay games oldest first. A game is a team game if either side has more than one
    player, and it moves every member's team rating by the same amount. Games against an
    entrant with no Discord account can't be rated, so they're skipped."""
    ratings = Ratings()
    for game in history:
        if not game.winners or not game.losers:
            continue
        is_singles = len(game.winners) == 1 and len(game.losers) == 1
        rating_of = ratings.singles_rating if is_singles else ratings.team_rating
        table = ratings.singles if is_singles else ratings.team
        before = {user_id: rating_of(user_id) for user_id in game.winners + game.losers}
        change = K_FACTOR * (1 - expected_score(mean(before[u] for u in game.winners),
                                                mean(before[u] for u in game.losers)))
        for user_id in game.winners:
            table[user_id] = before[user_id] + change
        for user_id in game.losers:
            table[user_id] = before[user_id] - change
    return ratings


def entrant_rating(member_ids: list[int], ratings: Ratings) -> Optional[float]:
    """An entrant's rating for seeding: a solo player's singles rating, or a team's
    average team rating. None while that rating is still provisional (including for an
    entrant with no Discord account), so seeding can decide where new players go."""
    if len(member_ids) == 1:
        return ratings.singles.get(member_ids[0])
    if not any(ratings.has_played(user_id) for user_id in member_ids):
        return None
    return mean(ratings.team_rating(user_id) for user_id in member_ids)
