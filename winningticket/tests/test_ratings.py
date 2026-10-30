import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ratings import BASE_RATING, K_FACTOR, PlayedMatch, Ratings, compute_ratings, entrant_rating, expected_score
from seeding import place_new_entrants, rating_order


def test_evenly_matched_players_swap_half_the_k_factor():
    ratings = compute_ratings([PlayedMatch((1,), (2,))])
    assert ratings.singles == {1: BASE_RATING + K_FACTOR / 2, 2: BASE_RATING - K_FACTOR / 2}
    assert ratings.team == {}


def test_beating_a_stronger_player_gains_more_than_beating_a_weaker_one():
    history = [PlayedMatch((1,), (2,))] * 3  # 1 is now well ahead of 2
    upset = compute_ratings(history + [PlayedMatch((2,), (1,))])
    expected = compute_ratings(history + [PlayedMatch((1,), (2,))])
    before = compute_ratings(history)
    assert upset.singles[2] - before.singles[2] > expected.singles[1] - before.singles[1]


def test_expected_scores_add_up_to_one():
    assert expected_score(1600, 1400) + expected_score(1400, 1600) == pytest.approx(1)
    assert expected_score(1500, 1500) == 0.5


def test_games_against_an_entrant_with_no_account_are_skipped():
    assert compute_ratings([PlayedMatch((1,), ()), PlayedMatch((), (2,))]) == Ratings()


def test_a_team_game_moves_team_ratings_starting_from_singles():
    ratings = compute_ratings([PlayedMatch((1,), (2,)), PlayedMatch((1, 3), (2, 4))])
    assert ratings.singles == {1: 1516, 2: 1484}  # team games leave singles alone
    # Team (1, 3) averaged 1508 against 1492, so it was expected to win and gained under half of K.
    change = K_FACTOR * (1 - expected_score(1508, 1492))
    assert change < K_FACTOR / 2
    assert ratings.team == pytest.approx({1: 1516 + change, 3: 1500 + change, 2: 1484 - change, 4: 1500 - change})


def test_entrant_rating_is_none_until_someone_behind_it_has_played():
    ratings = compute_ratings([PlayedMatch((1,), (2,))])
    assert entrant_rating([1], ratings) == 1516
    assert entrant_rating([5], ratings) is None
    assert entrant_rating([], ratings) is None
    assert entrant_rating([5, 6], ratings) is None
    # A team with one rated member averages in the newcomer at the base rating.
    assert entrant_rating([1, 5], ratings) == (1516 + BASE_RATING) / 2


def test_a_solo_entrant_with_only_team_games_is_still_unrated_for_singles():
    ratings = compute_ratings([PlayedMatch((1, 2), (3, 4))])
    assert entrant_rating([1], ratings) is None


def test_new_entrants_count_as_average_players():
    order = place_new_entrants({10: 1600, 20: 1400}, [30], random.Random(0))
    assert order == [10, 30, 20]


def test_equal_ratings_are_put_in_random_order():
    orders = {tuple(place_new_entrants({}, [1, 2, 3, 4], random.Random(seed))) for seed in range(20)}
    assert len(orders) > 1
    assert all(sorted(order) == [1, 2, 3, 4] for order in orders)


def test_rating_order_seeds_teams_by_their_members_ratings():
    ratings = compute_ratings([PlayedMatch((1,), (2,)), PlayedMatch((1,), (3,))])
    members_by_team = {100: [3], 200: [1], 300: [], 400: [2]}
    # Player 1 won twice; 2 and 3 each lost once, but 3 lost to a stronger player and dropped less.
    assert rating_order(members_by_team, ratings, random.Random(0)) == [200, 300, 100, 400]
