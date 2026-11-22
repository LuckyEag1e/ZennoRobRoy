"""How much each player contributed in past 5v5 lobbies, used to balance new teams. It's
kept apart from prediction scores and Elo, and like ratings it's never stored: it's
worked out again from concluded events whenever it's needed."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Hashable, Optional, TypeVar

# The lobby's top scorer gets 10, the next 9, and so on, whatever the lobby's size.
TOP_SCORE = 10
WIN_BONUS = 3
WINNING_CAPTAIN_BONUS = 1
# Events aren't frequent, so only the last few lobbies count.
RECENT_LOBBIES = 3
# What everyone counts as when nobody being drawn has played before. Any value works,
# since it only matters relative to other players.
FIRST_EVENT_CONTRIBUTION = 5.5

Player = TypeVar("Player", bound=Hashable)


def lobby_scores(points: dict[Player, int], winners: set[Player],
                 winning_captain: Optional[Player]) -> dict[Player, int]:
    """Each player's contribution in one lobby. They're ranked by points across the whole
    lobby, both teams together, and players level on points share the better place."""
    scores = {}
    for player, own in points.items():
        place = 1 + sum(1 for other in points.values() if other > own)
        scores[player] = TOP_SCORE + 1 - place
        if player in winners:
            scores[player] += WIN_BONUS
        if player == winning_captain:
            scores[player] += WINNING_CAPTAIN_BONUS
    return scores


@dataclass(frozen=True)
class PlayedLobby:
    """One lobby's result, by entrant. `user_ids` holds the account behind each entrant
    that has one; only they build up a contribution."""

    points: dict[int, int]
    winners: frozenset[int]
    winning_captain: Optional[int]
    user_ids: dict[int, int]


def contributions(history: list[PlayedLobby], recent: int = RECENT_LOBBIES) -> dict[int, float]:
    """Each user's average contribution over their last `recent` lobbies, from history in
    the order it was played."""
    scores_by_user: dict[int, list[int]] = {}
    for lobby in history:
        for entrant, score in lobby_scores(lobby.points, set(lobby.winners), lobby.winning_captain).items():
            user_id = lobby.user_ids.get(entrant)
            if user_id is not None:
                scores_by_user.setdefault(user_id, []).append(score)
    return {user_id: mean(scores[-recent:]) for user_id, scores in scores_by_user.items()}


def entrant_contributions(user_of: dict[int, Optional[int]], by_user: dict[int, float]) -> dict[int, float]:
    """The contribution to balance each entrant by. Anyone without history (new players,
    and entrants with no account) counts as the average of those being drawn who have it."""
    known = {entrant: by_user[user] for entrant, user in user_of.items() if user in by_user}
    default = mean(known.values()) if known else FIRST_EVENT_CONTRIBUTION
    return {entrant: known.get(entrant, default) for entrant in user_of}
