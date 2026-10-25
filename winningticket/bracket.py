"""Pure bracket logic: tree generation, slot derivation, scoring, pick-distribution stats.

No discord.py or sqlite3 imports here on purpose -- this module operates on plain
dataclasses/dicts so it can be unit tested without a database or a running bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


class BracketError(ValueError):
    pass


@dataclass
class MatchSpec:
    """A match produced by generate_tree(), before DB ids exist."""

    round: int
    slot_index: int
    team_a: Optional[int] = None  # placeholder team id, round 1 only
    team_b: Optional[int] = None  # placeholder team id, round 1 only
    feeds_into_round: Optional[int] = None
    feeds_into_slot_index: Optional[int] = None
    feeds_into_side: Optional[str] = None  # 'A' or 'B'


@dataclass
class Match:
    """A match as persisted in the DB (or an equivalent in-memory fixture)."""

    id: int
    round: int
    slot_index: int
    team_a_id: Optional[int] = None
    team_b_id: Optional[int] = None
    feeds_into_match_id: Optional[int] = None
    feeds_into_side: Optional[str] = None  # 'A' or 'B'
    # The round players see, which also sets the points. It differs from `round` (the tree
    # column) when byes meet: their match sits in round 2 of the tree but is both teams'
    # first game. Left unset until assign_played_rounds() numbers the whole tree.
    played_round: Optional[int] = None


@dataclass
class Podium:
    champion: Optional[int]
    runner_up: Optional[int]
    third_places: list[int]  # both semi-final losers: there's no third-place game


@dataclass
class UserScore:
    user_id: int
    points: int
    correct_picks: int
    possible_picks: int


WinnerLookup = Callable[[int], Optional[int]]


def is_bye(match: Match) -> bool:
    """A round-1 match with only one real team -- it auto-advances with no game played."""
    if match.round != 1:
        return False
    return (match.team_a_id is None) != (match.team_b_id is None)


def bye_team_id(match: Match) -> Optional[int]:
    """The sole team in a bye match, or None if `match` isn't a bye."""
    if not is_bye(match):
        return None
    return match.team_a_id if match.team_a_id is not None else match.team_b_id


def effective_winner(match: Match, winner_of: WinnerLookup) -> Optional[int]:
    """A match's winner for tree-advancement purposes: the automatic bye winner,
    or whatever `winner_of` reports (real results or a user's own predictions)."""
    bye_winner = bye_team_id(match)
    if bye_winner is not None:
        return bye_winner
    return winner_of(match.id)


def check_matchup_count(count: int) -> None:
    """A single-elimination bracket only works with a power-of-2 number of round-1 matchups."""
    if count < 1 or (count & (count - 1)) != 0:
        raise BracketError(
            f"{count} round-1 matchups given; the number of matchups must be a "
            f"power of 2 (1, 2, 4, 8, 16, 32, ...)"
        )


def assign_played_rounds(matches_by_id: dict[int, Match], feeders_by_target: dict[int, dict[str, Match]]) -> None:
    """Number each match by the round it's actually played in: one round after the later
    of its two feeder games. A bye has no game, so it gets 0."""
    # Feeders are always in an earlier tree column, so they're numbered first.
    for match in sorted(matches_by_id.values(), key=lambda m: m.round):
        if match.round == 1:
            match.played_round = 0 if is_bye(match) else 1
            continue
        feeders = feeders_by_target.get(match.id, {})
        match.played_round = 1 + max(
            (feeder.played_round for feeder in feeders.values() if feeder is not None), default=0
        )


def byes_in_round(matches_by_id: dict[int, Match], round_num: int) -> list[Match]:
    """Matches whose winner (or, for a round-1 bye, whose only team) sits out `round_num`
    waiting for their next game, in bracket order."""
    waiting = []
    for match in matches_by_id.values():
        next_match = matches_by_id.get(match.feeds_into_match_id)
        if next_match is not None and match.played_round < round_num < next_match.played_round:
            waiting.append(match)
    return sorted(waiting, key=bracket_order)


def rounds(matches_by_id: dict[int, Match]) -> list[int]:
    return sorted({m.played_round for m in matches_by_id.values() if m.played_round >= 1})


def bracket_order(match: Match) -> tuple[int, int]:
    """Sort key: earlier rounds first, then top to bottom. The second part is the match's
    first round-1 row, which compares matches from different tree columns."""
    return (match.played_round, match.slot_index << (match.round - 1))


def final_match(matches_by_id: dict[int, Match]) -> Match:
    return next(m for m in matches_by_id.values() if m.feeds_into_match_id is None)


def next_match_to_pick(matches_by_id: dict[int, Match], picks: dict[int, int]) -> Optional[Match]:
    """The first match in bracket order that still needs a pick, or None once the bracket is
    complete. Deriving it from the picks (rather than counting steps) means a repeated or
    stale click can't skip a match."""
    for match in sorted(matches_by_id.values(), key=bracket_order):
        if not is_bye(match) and match.id not in picks:
            return match
    return None


def matches_in_round(matches_by_id: dict[int, Match], round_num: int) -> list[Match]:
    return sorted((m for m in matches_by_id.values() if m.played_round == round_num), key=bracket_order)


def generate_tree(team_pairs: list[tuple[Optional[int], Optional[int]]]) -> list[MatchSpec]:
    """Build the full single-elimination match tree from round-1 pairs.

    `team_pairs` is a list of (team_a_id, team_b_id) in top-to-bottom bracket
    order. Its length must be a power of two. Round r matches 2*i and 2*i+1
    feed into round r+1 match i (sides A and B respectively). Either side of a
    pair may be None to represent a bye -- the other team auto-advances.
    """
    check_matchup_count(len(team_pairs))

    all_specs: list[MatchSpec] = []
    current_round = [
        MatchSpec(round=1, slot_index=i, team_a=a, team_b=b)
        for i, (a, b) in enumerate(team_pairs)
    ]
    all_specs.extend(current_round)

    round_num = 1
    while len(current_round) > 1:
        next_round: list[MatchSpec] = []
        for i in range(0, len(current_round), 2):
            left = current_round[i]
            right = current_round[i + 1]
            next_slot_index = i // 2
            left.feeds_into_round = round_num + 1
            left.feeds_into_slot_index = next_slot_index
            left.feeds_into_side = "A"
            right.feeds_into_round = round_num + 1
            right.feeds_into_slot_index = next_slot_index
            right.feeds_into_side = "B"
            next_round.append(MatchSpec(round=round_num + 1, slot_index=next_slot_index))
        all_specs.extend(next_round)
        current_round = next_round
        round_num += 1

    return all_specs


def build_feeder_index(matches: list[Match]) -> dict[int, dict[str, Match]]:
    """Return {match_id: {'A': feeder_match_or_None, 'B': feeder_match_or_None}}."""
    index: dict[int, dict[str, Match]] = {}
    for m in matches:
        if m.feeds_into_match_id is None:
            continue
        side_map = index.setdefault(m.feeds_into_match_id, {"A": None, "B": None})
        side_map[m.feeds_into_side] = m
    return index


def resolve_slot(
    match: Match,
    matches_by_id: dict[int, Match],
    winner_of: WinnerLookup,
    feeders_by_target: dict[int, dict[str, Match]],
    memo: Optional[dict[int, tuple[Optional[int], Optional[int]]]] = None,
) -> tuple[Optional[int], Optional[int]]:
    """Return (team_a_id, team_b_id) occupying this match, per `winner_of`.

    Round 1 teams come directly from the match row. Round 2+ teams are the
    winners of the two feeder matches, as reported by `winner_of` -- which the
    caller points at either real match_results or one user's own predictions.
    """
    if memo is None:
        memo = {}
    if match.id in memo:
        return memo[match.id]

    if match.round == 1:
        result = (match.team_a_id, match.team_b_id)
        memo[match.id] = result
        return result

    feeders = feeders_by_target.get(match.id, {"A": None, "B": None})
    feeder_a = feeders.get("A")
    feeder_b = feeders.get("B")
    team_a = effective_winner(feeder_a, winner_of) if feeder_a is not None else None
    team_b = effective_winner(feeder_b, winner_of) if feeder_b is not None else None
    result = (team_a, team_b)
    memo[match.id] = result
    return result


def resolve_all_slots(
    matches_by_id: dict[int, Match],
    winner_of: WinnerLookup,
    feeders_by_target: dict[int, dict[str, Match]],
) -> dict[int, tuple[Optional[int], Optional[int]]]:
    memo: dict[int, tuple[Optional[int], Optional[int]]] = {}
    for match in matches_by_id.values():
        resolve_slot(match, matches_by_id, winner_of, feeders_by_target, memo)
    return memo


def matches_with_both_teams(
    matches_by_id: dict[int, Match],
    slots: dict[int, tuple[Optional[int], Optional[int]]],
) -> list[Match]:
    """Matches whose two teams are both known, in bracket order, whether or not they've been played."""
    known = [m for m in matches_by_id.values() if slots[m.id][0] is not None and slots[m.id][1] is not None]
    return sorted(known, key=bracket_order)


def stale_results(
    matches_by_id: dict[int, Match],
    feeders_by_target: dict[int, dict[str, Match]],
    results_by_match: dict[int, int],
) -> list[int]:
    """Match ids whose recorded winner is no longer one of the two teams in that match,
    which happens after an earlier result is changed or cleared. Dropping one result can
    make a later one stale too, so the bracket is walked in order against what remains."""
    remaining = dict(results_by_match)
    stale = []
    for match in sorted(matches_by_id.values(), key=bracket_order):
        winner = remaining.get(match.id)
        if winner is None:
            continue
        if winner not in resolve_slot(match, matches_by_id, remaining.get, feeders_by_target):
            del remaining[match.id]
            stale.append(match.id)
    return stale


def next_opponent(
    match: Match,
    feeders_by_target: dict[int, dict[str, Match]],
    winner_of: WinnerLookup,
) -> Optional[int]:
    """Who this match's winner will face next, or None if that isn't decided yet.
    Only meaningful when the match isn't the final."""
    other_side = "B" if match.feeds_into_side == "A" else "A"
    other_feeder = feeders_by_target.get(match.feeds_into_match_id, {}).get(other_side)
    if other_feeder is None:
        return None
    return effective_winner(other_feeder, winner_of)


def resolvable_matches(
    matches_by_id: dict[int, Match],
    feeders_by_target: dict[int, dict[str, Match]],
    winner_of: WinnerLookup,
    existing_results: set[int],
) -> list[Match]:
    """Matches whose real teams are both known but have no result recorded yet."""
    slots = resolve_all_slots(matches_by_id, winner_of, feeders_by_target)
    return [m for m in matches_with_both_teams(matches_by_id, slots) if m.id not in existing_results]


def compute_leaderboard(
    matches_by_id: dict[int, Match],
    results_by_match: dict[int, int],
    predictions_by_user: dict[int, dict[int, int]],
) -> list[UserScore]:
    """Score every user's predictions against real results.

    `predictions_by_user` maps user_id -> {match_id: predicted_winner_team_id}.
    Scoring compares predicted winner vs actual winner at the same match_id
    (tree position) -- this is correct regardless of whether the user's own
    derived matchup at that slot matches reality, because the predict flow
    only ever lets a user choose between the two teams their own prior picks
    produced.
    """
    scores: list[UserScore] = []
    for user_id, preds in predictions_by_user.items():
        points = 0
        correct_picks = 0
        possible_picks = 0
        for match in matches_by_id.values():
            actual_winner = results_by_match.get(match.id)
            if actual_winner is None:
                continue
            predicted_winner = preds.get(match.id)
            if predicted_winner is None:
                continue
            possible_picks += 1
            if predicted_winner == actual_winner:
                points += 2 ** (match.played_round - 1)
                correct_picks += 1
        scores.append(
            UserScore(
                user_id=user_id,
                points=points,
                correct_picks=correct_picks,
                possible_picks=possible_picks,
            )
        )

    scores.sort(key=lambda s: (-s.points, s.user_id))
    return scores


def pick_distribution(
    matches_by_id: dict[int, Match],
    predictions_by_user: dict[int, dict[int, int]],
    round_num: int,
    team_a_id: int,
    team_b_id: int,
) -> dict[int, int]:
    """Count how many users predicted each of team_a_id/team_b_id to win
    *some* match at `round_num` -- not a specific match_id, since different
    users' predicted brackets can place a team in different match_ids once
    round >= 2. Returns {team_id: count}, including zero counts for both.
    """
    round_match_ids = {m.id for m in matches_by_id.values() if m.played_round == round_num}
    counts = {team_a_id: 0, team_b_id: 0}
    for preds in predictions_by_user.values():
        for match_id, predicted_winner in preds.items():
            if match_id not in round_match_ids:
                continue
            if predicted_winner in counts:
                counts[predicted_winner] += 1
    return counts


def podium(
    matches_by_id: dict[int, Match],
    feeders_by_target: dict[int, dict[str, Match]],
    results_by_match: dict[int, int],
) -> Podium:
    """The final's winner and loser, and the losers of the games that fed into the final."""
    slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders_by_target)

    def loser(match: Match) -> Optional[int]:
        winner = results_by_match.get(match.id)
        if winner is None:
            return None
        team_a, team_b = slots[match.id]
        return team_b if winner == team_a else team_a

    final = final_match(matches_by_id)
    semi_finals = [m for m in feeders_by_target.get(final.id, {}).values() if m is not None and not is_bye(m)]
    third_places = [team for team in map(loser, semi_finals) if team is not None]
    return Podium(results_by_match.get(final.id), loser(final), third_places)


def top_places(scores: list[UserScore], places: int = 3) -> list[tuple[int, UserScore]]:
    """(place, score) for everyone placing in the top `places`. Tied points share a place,
    and the next place is skipped for each extra player tied (1, 1, 3)."""
    placed = []
    for i, score in enumerate(scores):  # already sorted by points, highest first
        place = i + 1 if i == 0 or score.points != scores[i - 1].points else placed[-1][0]
        if place > places:
            break
        placed.append((place, score))
    return placed
