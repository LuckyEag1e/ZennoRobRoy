import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bracket import (
    BracketError,
    Match,
    Podium,
    bracket_order,
    build_feeder_index,
    bye_team_id,
    byes_in_round,
    compute_leaderboard,
    final_match,
    generate_tree,
    is_bye,
    next_match_to_pick,
    podium,
    resolvable_matches,
    resolve_all_slots,
    resolve_slot,
    stale_results,
)
from helpers import matches_from_pairs, numbered


def test_generate_tree_4_teams():
    specs = generate_tree([(1, 2), (3, 4)])
    round1 = [s for s in specs if s.round == 1]
    round2 = [s for s in specs if s.round == 2]
    assert len(round1) == 2
    assert len(round2) == 1
    assert round1[0].feeds_into_round == 2
    assert round1[0].feeds_into_slot_index == 0
    assert round1[0].feeds_into_side == "A"
    assert round1[1].feeds_into_side == "B"
    assert round2[0].feeds_into_round is None


def test_generate_tree_16_teams():
    team_pairs = [(i, i + 1) for i in range(0, 16, 2)]
    specs = generate_tree(team_pairs)
    assert len(specs) == 15  # N-1 matches for N teams
    by_round = {}
    for s in specs:
        by_round.setdefault(s.round, []).append(s)
    assert len(by_round[1]) == 8
    assert len(by_round[2]) == 4
    assert len(by_round[3]) == 2
    assert len(by_round[4]) == 1

    for r in (1, 2, 3):
        slots = sorted(s.slot_index for s in by_round[r])
        assert slots == list(range(len(by_round[r])))
        for s in by_round[r]:
            assert s.feeds_into_round == r + 1
    assert by_round[4][0].feeds_into_round is None

    # every round r+1 match has exactly one A feeder and one B feeder
    feeders_by_target = {}
    for s in specs:
        if s.feeds_into_round is None:
            continue
        key = (s.feeds_into_round, s.feeds_into_slot_index)
        feeders_by_target.setdefault(key, {})[s.feeds_into_side] = s
    for r in (2, 3, 4):
        for slot in range(len(by_round[r])):
            sides = feeders_by_target[(r, slot)]
            assert set(sides.keys()) == {"A", "B"}


@pytest.mark.parametrize("n", [0, 3, 5, 17])
def test_generate_tree_rejects_non_power_of_two(n):
    with pytest.raises(BracketError):
        generate_tree([(i, i + 1) for i in range(n)])


def test_generate_tree_1_match():
    specs = generate_tree([(1, 2)])
    assert len(specs) == 1
    assert specs[0].feeds_into_round is None


def _build_4_team_fixture():
    # round 1: match 100 (team 1 vs 2), match 101 (team 3 vs 4)
    # round 2: match 200 (final)
    m100 = Match(id=100, round=1, slot_index=0, team_a_id=1, team_b_id=2,
                 feeds_into_match_id=200, feeds_into_side="A")
    m101 = Match(id=101, round=1, slot_index=1, team_a_id=3, team_b_id=4,
                 feeds_into_match_id=200, feeds_into_side="B")
    m200 = Match(id=200, round=2, slot_index=0)
    matches_by_id = numbered({100: m100, 101: m101, 200: m200})
    feeders = build_feeder_index(list(matches_by_id.values()))
    return matches_by_id, feeders


def test_resolve_slot_round1_returns_fixed_teams():
    matches_by_id, feeders = _build_4_team_fixture()
    result = resolve_slot(matches_by_id[100], matches_by_id, lambda mid: None, feeders)
    assert result == (1, 2)


def test_resolve_slot_round2_unresolved_when_feeder_incomplete():
    matches_by_id, feeders = _build_4_team_fixture()
    winners = {100: 1}  # only match 100 decided
    result = resolve_slot(matches_by_id[200], matches_by_id, lambda mid: winners.get(mid), feeders)
    assert result == (1, None)


def test_resolve_slot_round2_resolved_when_both_feeders_decided():
    matches_by_id, feeders = _build_4_team_fixture()
    winners = {100: 1, 101: 3}
    result = resolve_slot(matches_by_id[200], matches_by_id, lambda mid: winners.get(mid), feeders)
    assert result == (1, 3)


def test_resolve_all_slots_full_bracket_no_results():
    matches_by_id, feeders = _build_4_team_fixture()
    slots = resolve_all_slots(matches_by_id, lambda mid: None, feeders)
    assert slots[200] == (None, None)


def test_resolvable_matches_only_returns_matches_with_both_slots_known_and_no_result():
    matches_by_id, feeders = _build_4_team_fixture()
    winners = {}
    existing_results = set()

    resolvable = resolvable_matches(matches_by_id, feeders, lambda mid: winners.get(mid), existing_results)
    assert {m.id for m in resolvable} == {100, 101}

    winners[100] = 1
    existing_results.add(100)
    resolvable = resolvable_matches(matches_by_id, feeders, lambda mid: winners.get(mid), existing_results)
    assert {m.id for m in resolvable} == {101}  # final still missing feeder B

    winners[101] = 3
    existing_results.add(101)
    resolvable = resolvable_matches(matches_by_id, feeders, lambda mid: winners.get(mid), existing_results)
    assert {m.id for m in resolvable} == {200}


def test_user_upset_round1_changes_round2_options():
    matches_by_id, feeders = _build_4_team_fixture()
    # user predicts team 2 (upset) beats team 1, and team 3 beats team 4
    user_picks = {100: 2, 101: 3}
    team_a, team_b = resolve_slot(matches_by_id[200], matches_by_id,
                                   lambda mid: user_picks.get(mid), feeders)
    assert (team_a, team_b) == (2, 3)


def test_user_predictions_independent_of_real_results():
    matches_by_id, feeders = _build_4_team_fixture()
    real_winners = {100: 1, 101: 3}  # no upset in reality
    user_picks = {100: 2, 101: 3}  # user predicted the upset

    real_slots = resolve_slot(matches_by_id[200], matches_by_id,
                               lambda mid: real_winners.get(mid), feeders)
    predicted_slots = resolve_slot(matches_by_id[200], matches_by_id,
                                    lambda mid: user_picks.get(mid), feeders)

    assert real_slots == (1, 3)
    assert predicted_slots == (2, 3)


def _build_4_team_fixture_with_bye():
    # round 1: match 100 (team 1 vs team 2, real match), match 101 (team 3, bye -- no opponent)
    # round 2: match 200 (final)
    m100 = Match(id=100, round=1, slot_index=0, team_a_id=1, team_b_id=2,
                 feeds_into_match_id=200, feeds_into_side="A")
    m101 = Match(id=101, round=1, slot_index=1, team_a_id=3, team_b_id=None,
                 feeds_into_match_id=200, feeds_into_side="B")
    m200 = Match(id=200, round=2, slot_index=0)
    matches_by_id = numbered({100: m100, 101: m101, 200: m200})
    feeders = build_feeder_index(list(matches_by_id.values()))
    return matches_by_id, feeders


def test_is_bye_true_for_round1_single_team():
    matches_by_id, _ = _build_4_team_fixture_with_bye()
    assert is_bye(matches_by_id[101]) is True
    assert bye_team_id(matches_by_id[101]) == 3


def test_is_bye_false_for_round1_both_teams():
    matches_by_id, _ = _build_4_team_fixture_with_bye()
    assert is_bye(matches_by_id[100]) is False
    assert bye_team_id(matches_by_id[100]) is None


def test_is_bye_false_for_later_round_even_with_one_slot_unknown():
    matches_by_id, _ = _build_4_team_fixture_with_bye()
    # round 2 match naturally starts with both slots None -- that's "undecided", not a bye
    assert is_bye(matches_by_id[200]) is False


def test_resolve_slot_bye_feeder_auto_advances_without_a_recorded_result():
    matches_by_id, feeders = _build_4_team_fixture_with_bye()
    # no match_results/predictions recorded for match 101 at all
    winners = {100: 1}
    team_a, team_b = resolve_slot(matches_by_id[200], matches_by_id, lambda mid: winners.get(mid), feeders)
    assert (team_a, team_b) == (1, 3)  # team 3 auto-advanced from its bye


def test_resolvable_matches_excludes_bye_matches():
    matches_by_id, feeders = _build_4_team_fixture_with_bye()
    winners = {100: 1}
    resolvable = resolvable_matches(matches_by_id, feeders, lambda mid: winners.get(mid), {100})
    # match 101 is a bye (never needs a recorded result); final (200) is now resolvable
    # since team 3 auto-advanced from its bye and team 1 won match 100
    assert {m.id for m in resolvable} == {200}


def test_final_match_is_the_one_feeding_nothing():
    matches_by_id, _ = _build_4_team_fixture()
    assert final_match(matches_by_id).id == 200


def test_stale_results_keeps_consistent_results():
    matches_by_id, feeders = _build_4_team_fixture()
    assert stale_results(matches_by_id, feeders, {100: 1, 101: 3, 200: 1}) == []


def test_stale_results_drops_later_winner_after_earlier_result_changes():
    matches_by_id, feeders = _build_4_team_fixture()
    # team 1 won the final, but match 100 was then changed so team 2 advanced instead
    assert stale_results(matches_by_id, feeders, {100: 2, 101: 3, 200: 1}) == [200]


def test_stale_results_keeps_later_winner_who_is_still_in_the_match():
    matches_by_id, feeders = _build_4_team_fixture()
    # team 3 won the final from the other side, so changing match 100 doesn't affect it
    assert stale_results(matches_by_id, feeders, {100: 2, 101: 3, 200: 3}) == []


def test_stale_results_drops_later_winner_after_earlier_result_is_cleared():
    matches_by_id, feeders = _build_4_team_fixture()
    assert stale_results(matches_by_id, feeders, {101: 3, 200: 1}) == [200]


def test_next_match_to_pick_follows_bracket_order_and_skips_byes():
    matches_by_id, _ = _build_4_team_fixture_with_bye()
    assert next_match_to_pick(matches_by_id, {}).id == 100
    assert next_match_to_pick(matches_by_id, {100: 1}).id == 200
    assert next_match_to_pick(matches_by_id, {100: 1, 200: 3}) is None


def _match_between(matches_by_id, team_a, team_b):
    feeders = build_feeder_index(list(matches_by_id.values()))
    slots = resolve_all_slots(matches_by_id, lambda mid: None, feeders)
    return next(m for m in matches_by_id.values() if set(slots[m.id]) == {team_a, team_b})


def test_two_byes_meeting_is_a_round_1_game():
    # 1 and 2 both have byes into a game against each other, so it's played alongside 3 vs 4.
    matches_by_id = matches_from_pairs([(1, None), (2, None), (3, 4), (5, None)])
    assert _match_between(matches_by_id, 1, 2).played_round == 1
    assert _match_between(matches_by_id, 3, 4).played_round == 1
    final = final_match(matches_by_id)
    assert final.played_round == 3


def test_byes_in_round_covers_round_1_byes_and_later_waits():
    # 5 waits out round 1 for the 3/4 winner; the 1/2 winner then waits out round 2.
    matches_by_id = matches_from_pairs([(1, None), (2, None), (3, 4), (5, None)])
    [round_1_bye] = byes_in_round(matches_by_id, 1)
    assert bye_team_id(round_1_bye) == 5
    [round_2_bye] = byes_in_round(matches_by_id, 2)
    assert round_2_bye is _match_between(matches_by_id, 1, 2)
    assert byes_in_round(matches_by_id, 3) == []


def test_played_rounds_survive_a_missing_feeder_link():
    matches_by_id = matches_from_pairs([(1, 2), (3, 4)])
    for match in matches_by_id.values():
        if match.round == 1:
            match.feeds_into_match_id = None
    numbered(matches_by_id)
    assert final_match(matches_by_id).played_round in (1, 2)
    assert all(m.played_round is not None for m in matches_by_id.values())


def test_next_match_to_pick_finishes_round_1_before_round_2():
    matches_by_id = matches_from_pairs([(1, None), (2, None), (3, 4), (5, None)])
    first = next_match_to_pick(matches_by_id, {})
    second = next_match_to_pick(matches_by_id, {first.id: 1})
    assert {first.played_round, second.played_round} == {1}
    assert first.round == 2  # top of the bracket first, even though it's a later tree column


def test_a_game_between_two_byes_scores_as_round_1():
    matches_by_id = matches_from_pairs([(1, None), (2, None), (3, 4), (5, None)])
    game = _match_between(matches_by_id, 1, 2)
    [score] = compute_leaderboard(matches_by_id, {game.id: 1}, {7: {game.id: 1}})
    assert score.points == 1


def _play_out(matches_by_id, winner_of_pair):
    """Results for every game, decided by `winner_of_pair(team_a, team_b)`."""
    feeders = build_feeder_index(list(matches_by_id.values()))
    results = {}
    for match in sorted(matches_by_id.values(), key=bracket_order):
        if not is_bye(match):
            results[match.id] = winner_of_pair(*resolve_slot(match, matches_by_id, results.get, feeders))
    return feeders, results


def test_podium_has_both_semi_final_losers_in_third():
    matches_by_id = matches_from_pairs([(1, 2), (3, 4), (5, 6), (7, 8)])
    feeders, results = _play_out(matches_by_id, min)
    assert podium(matches_by_id, feeders, results) == Podium(champion=1, runner_up=5, third_places=[3, 7])


def test_podium_leaves_out_a_bye_into_the_final():
    # 3 goes straight to the final, so only the other side's semi-final has a loser.
    matches_by_id = matches_from_pairs([(1, 2), (3, None)])
    feeders, results = _play_out(matches_by_id, min)
    assert podium(matches_by_id, feeders, results) == Podium(champion=1, runner_up=3, third_places=[2])


def test_podium_before_the_final_is_played():
    matches_by_id = matches_from_pairs([(1, 2), (3, 4)])
    feeders = build_feeder_index(list(matches_by_id.values()))
    assert podium(matches_by_id, feeders, {}) == Podium(champion=None, runner_up=None, third_places=[])
