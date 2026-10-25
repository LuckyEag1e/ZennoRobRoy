import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket import Match, compute_leaderboard
from helpers import numbered


def _bracket_4_teams():
    # round1: m1 (team1 vs team2), m2 (team3 vs team4); round2: m3 (final)
    m1 = Match(id=1, round=1, slot_index=0, team_a_id=1, team_b_id=2,
               feeds_into_match_id=3, feeds_into_side="A")
    m2 = Match(id=2, round=1, slot_index=1, team_a_id=3, team_b_id=4,
               feeds_into_match_id=3, feeds_into_side="B")
    m3 = Match(id=3, round=2, slot_index=0)
    return numbered({1: m1, 2: m2, 3: m3})


def test_scoring_basic_round1_and_round2_points():
    matches = _bracket_4_teams()
    results = {1: 1, 2: 3, 3: 1}  # team1 wins m1, team3 wins m2, team1 wins final
    preds = {42: {1: 1, 2: 3, 3: 1}}  # user gets everything right
    scores = compute_leaderboard(matches, results, preds)
    assert len(scores) == 1
    assert scores[0].points == 1 + 1 + 2  # m1(r1)=1, m2(r1)=1, m3(r2)=2
    assert scores[0].correct_picks == 3


def test_scoring_wrong_pick_awards_zero():
    matches = _bracket_4_teams()
    results = {1: 1}
    preds = {42: {1: 2}}  # wrong
    scores = compute_leaderboard(matches, results, preds)
    assert scores[0].points == 0
    assert scores[0].correct_picks == 0
    assert scores[0].possible_picks == 1


def test_scoring_incomplete_real_results_skips_unscored_matches():
    matches = _bracket_4_teams()
    results = {1: 1}  # m2 and m3 have no real result yet
    preds = {42: {1: 1, 2: 3, 3: 1}}
    scores = compute_leaderboard(matches, results, preds)
    assert scores[0].points == 1  # only m1 scoreable
    assert scores[0].possible_picks == 1


def test_scoring_user_with_no_prediction_for_a_decided_match_gets_zero_for_that_slot():
    matches = _bracket_4_teams()
    results = {1: 1, 2: 3}
    preds = {42: {1: 1}}  # never picked m2
    scores = compute_leaderboard(matches, results, preds)
    assert scores[0].points == 1
    assert scores[0].possible_picks == 1


def test_leaderboard_sort_order_descending_by_points_with_user_id_tiebreak():
    matches = _bracket_4_teams()
    results = {1: 1, 2: 3}
    preds = {
        99: {1: 1, 2: 3},  # 2 correct, 2 points
        10: {1: 1, 2: 3},  # tie with 99
        5: {1: 2, 2: 3},   # 1 correct, 1 point
    }
    scores = compute_leaderboard(matches, results, preds)
    assert [s.user_id for s in scores] == [10, 99, 5]  # tie broken by lower user_id first


def test_scoring_bye_match_never_scored_even_if_somehow_predicted():
    # m2 is a bye (team 3, no opponent) -- the predict flow never asks about it, so
    # normally no prediction row exists for it. Even if one did (e.g. stale data),
    # it must never contribute points since byes are never recorded in match_results.
    m1 = Match(id=1, round=1, slot_index=0, team_a_id=1, team_b_id=2,
               feeds_into_match_id=3, feeds_into_side="A")
    m2 = Match(id=2, round=1, slot_index=1, team_a_id=3, team_b_id=None,
               feeds_into_match_id=3, feeds_into_side="B")
    m3 = Match(id=3, round=2, slot_index=0)
    matches = numbered({1: m1, 2: m2, 3: m3})

    results = {1: 1, 3: 1}  # m2 (the bye) never appears in match_results
    preds = {42: {1: 1, 2: 3, 3: 1}}  # user "predicted" the bye anyway
    scores = compute_leaderboard(matches, results, preds)
    assert scores[0].points == 3  # 1 (round1 m1 correct) + 2 (round2 m3 correct); m2 never scored
    assert scores[0].possible_picks == 2  # only m1 and m3 are ever scoreable


def test_leaderboard_16_team_bracket_full_run():
    from bracket import generate_tree

    team_pairs = [(i, i + 1) for i in range(0, 16, 2)]
    specs = generate_tree(team_pairs)

    # assign sequential match ids matching insertion order, build Match objects
    matches: dict[int, Match] = {}
    id_by_round_slot = {}
    next_id = 1
    for spec in specs:
        id_by_round_slot[(spec.round, spec.slot_index)] = next_id
        next_id += 1

    for spec in specs:
        mid = id_by_round_slot[(spec.round, spec.slot_index)]
        team_a_id = team_pairs[spec.slot_index][0] if spec.round == 1 else None
        team_b_id = team_pairs[spec.slot_index][1] if spec.round == 1 else None
        feeds_into_match_id = None
        if spec.feeds_into_round is not None:
            feeds_into_match_id = id_by_round_slot[(spec.feeds_into_round, spec.feeds_into_slot_index)]
        matches[mid] = Match(
            id=mid, round=spec.round, slot_index=spec.slot_index,
            team_a_id=team_a_id, team_b_id=team_b_id,
            feeds_into_match_id=feeds_into_match_id, feeds_into_side=spec.feeds_into_side,
        )

    numbered(matches)

    # team ids 0,2,4,...,14 always win (highest seed always wins, no upsets in reality)
    from bracket import build_feeder_index, resolve_slot

    feeders = build_feeder_index(list(matches.values()))

    def chalk_winner(team_a, team_b):
        return min(team_a, team_b)  # lower id = "better seed", always wins

    results_by_match: dict[int, int] = {}

    def real_winner_of(mid):
        return results_by_match.get(mid)

    for round_num in sorted({m.round for m in matches.values()}):
        for match in [m for m in matches.values() if m.round == round_num]:
            team_a, team_b = resolve_slot(match, matches, real_winner_of, feeders)
            if team_a is not None and team_b is not None:
                results_by_match[match.id] = chalk_winner(team_a, team_b)

    assert len(results_by_match) == 15  # fully decided

    # user A picks chalk all the way (should get max possible points)
    user_a_preds: dict[int, int] = {}
    picks_winner_of = {}

    def pred_winner_of(mid):
        return picks_winner_of.get(mid)

    for round_num in sorted({m.round for m in matches.values()}):
        for match in [m for m in matches.values() if m.round == round_num]:
            team_a, team_b = resolve_slot(match, matches, pred_winner_of, feeders)
            if team_a is not None and team_b is not None:
                winner = chalk_winner(team_a, team_b)
                picks_winner_of[match.id] = winner
                user_a_preds[match.id] = winner

    # user B picks the opposite (max id) every time -> always wrong
    user_b_preds: dict[int, int] = {}
    b_picks = {}

    def b_winner_of(mid):
        return b_picks.get(mid)

    for round_num in sorted({m.round for m in matches.values()}):
        for match in [m for m in matches.values() if m.round == round_num]:
            team_a, team_b = resolve_slot(match, matches, b_winner_of, feeders)
            if team_a is not None and team_b is not None:
                winner = max(team_a, team_b)
                b_picks[match.id] = winner
                user_b_preds[match.id] = winner

    scores = compute_leaderboard(matches, results_by_match,
                                  {1: user_a_preds, 2: user_b_preds})
    scores_by_user = {s.user_id: s for s in scores}

    # user A (chalk) should match every real result -> max possible points:
    # 8 matches round1 (1pt) + 4 round2 (2pt) + 2 round3 (4pt) + 1 round4 (8pt) = 8+8+8+8=32
    assert scores_by_user[1].points == 32
    assert scores_by_user[1].correct_picks == 15

    # user B should score 0 -- every pick is the wrong team
    assert scores_by_user[2].points == 0
    assert scores_by_user[2].correct_picks == 0


def test_top_places_share_a_place_on_tied_points():
    from bracket import UserScore, top_places

    scores = [UserScore(user_id=u, points=p, correct_picks=0, possible_picks=0)
              for u, p in [(1, 9), (2, 9), (3, 7), (4, 5), (5, 5)]]
    assert [(place, s.user_id) for place, s in top_places(scores)] == [(1, 1), (1, 2), (3, 3)]


def test_top_places_includes_everyone_tied_for_the_last_place():
    from bracket import UserScore, top_places

    scores = [UserScore(user_id=u, points=p, correct_picks=0, possible_picks=0)
              for u, p in [(1, 9), (2, 7), (3, 5), (4, 5), (5, 1)]]
    assert [(place, s.user_id) for place, s in top_places(scores)] == [(1, 1), (2, 2), (3, 3), (3, 4)]
