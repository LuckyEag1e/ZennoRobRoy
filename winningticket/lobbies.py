"""Splitting a 5v5 event's sign-ups into lobbies, choosing captains, balancing teams, and
replacing players who drop out. Plain data only, like bracket.py, so it can be tested
without a database or Discord. Players are entrant ids."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from itertools import permutations
from typing import Optional

# What a player said when signing up: they want to captain, can if needed, or won't.
CAPTAIN_PREFS = ("want", "can", "no")
# A lobby is two teams: 5v5 at most, 4v4 at least.
MIN_LOBBY = 8
MAX_LOBBY = 10
# How players are shared out between lobbies: the strongest together, a similar mix in
# every lobby, or at random.
SPREADS = ("tier", "balanced", "random")


class LobbyError(Exception):
    """The players can't be formed into lobbies as asked."""


def is_candidate(pref: str) -> bool:
    return pref in ("want", "can")


def best_candidate(players: list[int], prefs: dict[int, str]) -> Optional[int]:
    """The first player in `players` who wants to captain, else the first who can, else None."""
    for wanted in ("want", "can"):
        for player in players:
            if prefs.get(player) == wanted:
                return player
    return None


def team_sizes(lobby_size: int) -> tuple[int, int]:
    """The two team sizes for a lobby: 10 is 5v5, 9 is 5v4, 8 is 4v4."""
    return math.ceil(lobby_size / 2), lobby_size // 2


@dataclass(frozen=True)
class Split:
    lobby_sizes: tuple[int, ...]
    waitlisted: int

    @property
    def placed(self) -> int:
        return sum(self.lobby_sizes)


def _even_split(players: int, lobbies: int) -> tuple[int, ...]:
    base, extra = divmod(players, lobbies)
    return tuple(base + 1 if i < extra else base for i in range(lobbies))


def possible_splits(player_count: int, captain_count: int) -> list[Split]:
    """Every way of splitting the players into lobbies, fewest lobbies first. Each lobby
    takes 8 to 10 players and needs two captains, and whoever doesn't fit waits."""
    most = min(captain_count // 2, player_count // MIN_LOBBY)
    splits = []
    for lobbies in range(1, most + 1):
        placed = min(player_count, lobbies * MAX_LOBBY)
        splits.append(Split(_even_split(placed, lobbies), player_count - placed))
    return splits


def propose_split(player_count: int, captain_count: int) -> Optional[Split]:
    """The split that gets the most players in, using the fewest (so fullest) lobbies to do
    it. None if there aren't enough players or captains for even one lobby."""
    splits = possible_splits(player_count, captain_count)
    if not splits:
        return None
    return max(splits, key=lambda split: (split.placed, -len(split.lobby_sizes)))


def choose_waitlist(signup_order: list[int], prefs: dict[int, str], split: Split) -> tuple[list[int], list[int]]:
    """(placed, waitlist), both in sign-up order. The latest sign-ups wait, except that
    enough captain candidates are placed for every lobby to have two: a waiting candidate
    (wanting before willing) takes the place of the latest placed player who can't captain."""
    position = {player: index for index, player in enumerate(signup_order)}
    placed = signup_order[:split.placed]
    waiting = signup_order[split.placed:]
    needed = 2 * len(split.lobby_sizes)
    while sum(is_candidate(prefs[player]) for player in placed) < needed:
        incoming = best_candidate(waiting, prefs)
        outgoing = next((player for player in reversed(placed) if not is_candidate(prefs[player])), None)
        if incoming is None or outgoing is None:
            raise LobbyError("There aren't enough captains for that many lobbies.")
        placed = sorted([player for player in placed if player != outgoing] + [incoming], key=position.get)
        waiting = sorted([player for player in waiting if player != incoming] + [outgoing], key=position.get)
    return placed, waiting


def _strongest_first(players: list[int], contributions: dict[int, float], rng: random.Random) -> list[int]:
    """Players by contribution, highest first, with equal contributions in random order."""
    shuffled = rng.sample(players, len(players))
    return sorted(shuffled, key=lambda player: -contributions[player])


def assign_lobbies(placed: list[int], contributions: dict[int, float], split: Split, spread: str,
                   rng: random.Random) -> list[list[int]]:
    """Share the placed players out between the split's lobbies."""
    sizes = split.lobby_sizes
    if spread == "balanced":
        lobbies: list[list[int]] = [[] for _ in sizes]
        # A snake draft: 1, 2, 3, 3, 2, 1, ... skipping lobbies that are already full.
        snake = list(range(len(sizes))) + list(reversed(range(len(sizes))))
        turn = 0
        for player in _strongest_first(placed, contributions, rng):
            while len(lobbies[snake[turn % len(snake)]]) >= sizes[snake[turn % len(snake)]]:
                turn += 1
            lobbies[snake[turn % len(snake)]].append(player)
            turn += 1
        return lobbies
    if spread == "tier":
        order = _strongest_first(placed, contributions, rng)
    elif spread == "random":
        order = rng.sample(placed, len(placed))
    else:
        raise ValueError(f"Unknown spread: {spread}")
    lobbies = []
    start = 0
    for size in sizes:
        lobbies.append(order[start:start + size])
        start += size
    return lobbies


def ensure_captains(lobbies: list[list[int]], prefs: dict[int, str],
                    contributions: dict[int, float]) -> list[list[int]]:
    """Give every lobby at least two captain candidates by swapping a spare candidate from
    another lobby for one of its other players. Each swap picks the two players whose
    contributions are closest, so the spread between lobbies changes as little as it can."""
    lobbies = [list(lobby) for lobby in lobbies]

    def candidates(lobby: list[int]) -> list[int]:
        return [player for player in lobby if is_candidate(prefs[player])]

    while True:
        short = next((lobby for lobby in lobbies if len(candidates(lobby)) < 2), None)
        if short is None:
            return lobbies
        swaps = [
            (abs(contributions[spare] - contributions[other]), spare, other, donor)
            for donor in lobbies if len(candidates(donor)) > 2
            for spare in candidates(donor)
            for other in short if not is_candidate(prefs[other])
        ]
        if not swaps:
            raise LobbyError("There aren't enough captains to go round the lobbies.")
        _, spare, other, donor = min(swaps, key=lambda swap: swap[0])
        donor[donor.index(spare)] = other
        short[short.index(other)] = spare


@dataclass(frozen=True)
class Teams:
    team_a: list[int]
    team_b: list[int]
    captain_a: int
    captain_b: int


def _balance_around(captain_a: int, captain_b: int, others: list[int], sizes: tuple[int, int],
                    contributions: dict[int, float]) -> tuple[list[int], list[int]]:
    """Each captain's team, filled highest contribution first, every player going to the
    team with the lower total that still has room."""
    teams = ([captain_a], [captain_b])
    totals = [contributions[captain_a], contributions[captain_b]]
    for player in sorted(others, key=lambda player: -contributions[player]):
        open_sides = [side for side in (0, 1) if len(teams[side]) < sizes[side]]
        side = min(open_sides, key=lambda side: (totals[side], len(teams[side]) - sizes[side]))
        teams[side].append(player)
        totals[side] += contributions[player]
    return teams


def form_teams(players: list[int], prefs: dict[int, str], sizes: tuple[int, int],
               contributions: dict[int, float], rng: random.Random) -> Teams:
    """Pick a lobby's two captains and teams together, with the closest team totals two
    opposing captains allow. When at least two players want to captain, only they're
    considered; otherwise anyone who can. Equally close options are chosen between at random."""
    wanting = [player for player in players if prefs[player] == "want"]
    candidates = wanting if len(wanting) >= 2 else [player for player in players if is_candidate(prefs[player])]
    if len(candidates) < 2:
        raise LobbyError("A lobby needs at least two players who can captain.")
    pairs = list(permutations(candidates, 2))  # ordered, since 5v4 teams differ in size
    rng.shuffle(pairs)
    best = None
    for captain_a, captain_b in pairs:
        others = [player for player in players if player not in (captain_a, captain_b)]
        team_a, team_b = _balance_around(captain_a, captain_b, others, sizes, contributions)
        gap = abs(sum(contributions[p] for p in team_a) - sum(contributions[p] for p in team_b))
        if best is None or gap < best[0]:
            best = (gap, Teams(team_a, team_b, captain_a, captain_b))
    return best[1]


@dataclass(frozen=True)
class Replacement:
    newcomer: Optional[int]  # who takes the spot, or None if nobody is waiting
    captain: Optional[int]  # the team's captain afterwards, or None if nobody can captain


def replace_in_place(team: list[int], captain: Optional[int], leaving: int, prefs: dict[int, str],
                     waitlist: list[int]) -> Replacement:
    """Who takes the place of a player leaving a team that's already been announced, and
    who captains it afterwards. Nobody else moves. The first waiting player takes the spot,
    unless the team is losing its captain (or has none) and no teammate can take over:
    then the first waiting captain candidate does, and captains."""
    first_waiting = waitlist[0] if waitlist else None
    if captain is not None and leaving != captain:
        return Replacement(first_waiting, captain)
    teammate = best_candidate([player for player in team if player != leaving], prefs)
    if teammate is not None:
        return Replacement(first_waiting, teammate)
    newcomer = best_candidate(waitlist, prefs)
    if newcomer is not None:
        return Replacement(newcomer, newcomer)
    return Replacement(first_waiting, None)
