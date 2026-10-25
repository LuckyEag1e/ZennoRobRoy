import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket import Match, pick_distribution
from helpers import numbered


def _bracket_4_teams():
    m1 = Match(id=1, round=1, slot_index=0, team_a_id=1, team_b_id=2,
               feeds_into_match_id=3, feeds_into_side="A")
    m2 = Match(id=2, round=1, slot_index=1, team_a_id=3, team_b_id=4,
               feeds_into_match_id=3, feeds_into_side="B")
    m3 = Match(id=3, round=2, slot_index=0)
    return numbered({1: m1, 2: m2, 3: m3})


def test_pick_distribution_round1_simple_majority():
    matches = _bracket_4_teams()
    preds = {
        10: {1: 1},
        20: {1: 1},
        30: {1: 2},
    }
    counts = pick_distribution(matches, preds, round_num=1, team_a_id=1, team_b_id=2)
    assert counts == {1: 2, 2: 1}


def test_pick_distribution_round2_aggregates_across_different_match_ids():
    # In round 2 every user's bracket places the final at match_id 3 in this
    # fixture (single match), but the aggregate must work by round, not by
    # a specific match_id, since in a larger bracket different users could
    # have different match_ids at the same round for the same team.
    matches = _bracket_4_teams()
    preds = {
        10: {3: 1},
        20: {3: 3},
        30: {3: 1},
    }
    counts = pick_distribution(matches, preds, round_num=2, team_a_id=1, team_b_id=3)
    assert counts == {1: 2, 3: 1}


def test_pick_distribution_ignores_other_rounds_and_other_teams():
    matches = _bracket_4_teams()
    preds = {
        10: {1: 1, 3: 1},
        20: {1: 2, 3: 3},
    }
    # asking about round 1 picks for teams 1 vs 2 should ignore the round-2 picks
    counts = pick_distribution(matches, preds, round_num=1, team_a_id=1, team_b_id=2)
    assert counts == {1: 1, 2: 1}


def test_pick_distribution_zero_counts_when_nobody_picked_either_team():
    matches = _bracket_4_teams()
    preds = {10: {1: 1}}
    counts = pick_distribution(matches, preds, round_num=2, team_a_id=1, team_b_id=3)
    assert counts == {1: 0, 3: 0}
