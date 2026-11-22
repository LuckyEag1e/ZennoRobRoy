import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contribution import PlayedLobby, contributions, entrant_contributions, lobby_scores


def test_lobby_scores_rank_the_whole_lobby_with_win_bonuses():
    points = {1: 20, 2: 15, 3: 10, 4: 5}
    scores = lobby_scores(points, winners={2, 3}, winning_captain=2)
    assert scores == {1: 10, 2: 9 + 3 + 1, 3: 8 + 3, 4: 7}


def test_lobby_scores_share_the_better_place_on_a_tie():
    scores = lobby_scores({1: 10, 2: 10, 3: 4}, winners=set(), winning_captain=None)
    assert scores == {1: 10, 2: 10, 3: 8}


def test_contributions_average_the_last_three_lobbies():
    def lobby(points_for_user_1):
        return PlayedLobby({1: points_for_user_1, 2: 10}, frozenset(), None, {1: 100, 2: 200})

    # User 100 places second, then first three times: only the last three count.
    history = [lobby(5), lobby(20), lobby(20), lobby(20)]
    assert contributions(history)[100] == 10


def test_contributions_skip_entrants_without_an_account():
    history = [PlayedLobby({1: 10, 2: 20}, frozenset({2}), 2, {1: 100})]
    assert contributions(history) == {100: 9}


def test_entrant_contributions_give_newcomers_the_average():
    values = entrant_contributions({1: 100, 2: 200, 3: None, 4: 400}, {100: 6.0, 200: 10.0})
    assert values == {1: 6.0, 2: 10.0, 3: 8.0, 4: 8.0}


def test_entrant_contributions_with_no_history_at_all():
    values = entrant_contributions({1: 100, 2: None}, {})
    assert values[1] == values[2]
