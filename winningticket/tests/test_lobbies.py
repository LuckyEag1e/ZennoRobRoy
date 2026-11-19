import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random

import pytest

from lobbies import (
    LobbyError,
    Split,
    assign_lobbies,
    choose_waitlist,
    ensure_captains,
    form_teams,
    is_candidate,
    possible_splits,
    propose_split,
    replace_in_place,
    team_sizes,
)


@pytest.mark.parametrize("players, expected", [
    (2, Split((2,), 0)),
    (7, Split((7,), 0)),
    (10, Split((10,), 0)),
    (12, Split((6, 6), 0)),
    (17, Split((9, 8), 0)),
    (18, Split((9, 9), 0)),
    (20, Split((10, 10), 0)),
    (23, Split((8, 8, 7), 0)),
    (24, Split((8, 8, 8), 0)),
    (40, Split((10, 10, 10, 10), 0)),
])
def test_propose_split_places_the_most_players_in_the_fullest_lobbies(players, expected):
    assert propose_split(players, captain_count=players) == expected


def test_propose_split_is_capped_by_captains():
    assert propose_split(20, captain_count=3) == Split((10,), 10)
    assert propose_split(12, captain_count=2) == Split((10,), 2)


def test_propose_split_needs_two_captains():
    assert propose_split(1, captain_count=1) is None
    assert propose_split(20, captain_count=1) is None
    assert propose_split(2, captain_count=2) == Split((2,), 0)


def test_possible_splits_lists_every_lobby_count():
    assert possible_splits(23, captain_count=6) == [Split((10,), 13), Split((10, 10), 3), Split((8, 8, 7), 0)]


def test_team_sizes():
    assert team_sizes(10) == (5, 5)
    assert team_sizes(9) == (5, 4)
    assert team_sizes(8) == (4, 4)


def test_choose_waitlist_keeps_sign_up_order():
    order = list(range(1, 13))
    prefs = {player: "want" for player in order}
    assert choose_waitlist(order, prefs, Split((10,), 2)) == (order[:10], [11, 12])


def test_choose_waitlist_brings_in_a_wanting_captain_before_a_willing_one():
    order = list(range(1, 14))
    prefs = {player: "no" for player in order}
    prefs[1] = "want"
    prefs[11] = "can"
    prefs[12] = "want"
    placed, waiting = choose_waitlist(order, prefs, Split((10,), 3))
    # 12 wants to captain, so they're placed ahead of 11, who only can; 10, the latest
    # placed player who can't captain, waits instead, ahead of the later sign-ups.
    assert placed == [1, 2, 3, 4, 5, 6, 7, 8, 9, 12]
    assert waiting == [10, 11, 13]


def test_choose_waitlist_never_bumps_a_captain_candidate():
    order = list(range(1, 12))
    prefs = {player: "can" for player in order}
    prefs[11] = "no"
    assert choose_waitlist(order, prefs, Split((10,), 1)) == (order[:10], [11])


def test_assign_lobbies_by_tier_puts_the_strongest_together():
    players = list(range(1, 21))
    contributions = {player: float(player) for player in players}
    lobbies = assign_lobbies(players, contributions, Split((10, 10), 0), "tier", random.Random(1))
    assert set(lobbies[0]) == set(range(11, 21))
    assert set(lobbies[1]) == set(range(1, 11))


def test_assign_lobbies_balanced_snakes_between_lobbies():
    players = list(range(1, 21))
    contributions = {player: float(player) for player in players}
    lobbies = assign_lobbies(players, contributions, Split((10, 10), 0), "balanced", random.Random(1))
    totals = [sum(contributions[p] for p in lobby) for lobby in lobbies]
    assert sorted(len(lobby) for lobby in lobbies) == [10, 10]
    assert abs(totals[0] - totals[1]) <= 10


def test_assign_lobbies_balanced_fills_uneven_lobbies():
    players = list(range(1, 18))
    contributions = {player: float(player) for player in players}
    lobbies = assign_lobbies(players, contributions, Split((9, 8), 0), "balanced", random.Random(1))
    assert [len(lobby) for lobby in lobbies] == [9, 8]
    assert sorted(sum(lobbies, [])) == players


def test_assign_lobbies_random_uses_everyone_once():
    players = list(range(1, 19))
    lobbies = assign_lobbies(players, {p: 0.0 for p in players}, Split((9, 9), 0), "random", random.Random(3))
    assert sorted(sum(lobbies, [])) == players


def test_ensure_captains_swaps_the_closest_spare_candidate():
    prefs = {p: "no" for p in range(1, 21)}
    for p in (1, 2, 3, 4):
        prefs[p] = "want"
    contributions = {p: float(p) for p in range(1, 21)}
    lobbies = [list(range(1, 11)), list(range(11, 21))]
    fixed = ensure_captains(lobbies, prefs, contributions)
    assert all(sum(is_candidate(prefs[p]) for p in lobby) >= 2 for lobby in fixed)
    # Candidate 4 is the closest to lobby 2's weakest player, 11.
    assert 4 in fixed[1] and 11 in fixed[0]
    assert sorted(sum(fixed, [])) == list(range(1, 21))


def test_ensure_captains_refuses_when_there_are_too_few():
    prefs = {p: "no" for p in range(1, 21)}
    prefs[1] = prefs[2] = prefs[3] = "can"
    with pytest.raises(LobbyError):
        ensure_captains([list(range(1, 11)), list(range(11, 21))], prefs, {p: 0.0 for p in range(1, 21)})


def test_form_teams_with_two_candidates_puts_them_on_opposing_teams():
    players = list(range(1, 11))
    prefs = {p: "no" for p in players}
    prefs[3] = prefs[7] = "can"
    contributions = {p: float(p) for p in players}
    teams = form_teams(players, prefs, (5, 5), contributions, random.Random(0))
    assert {teams.captain_a, teams.captain_b} == {3, 7}
    assert teams.captain_a in teams.team_a and teams.captain_b in teams.team_b
    assert sorted(teams.team_a + teams.team_b) == players
    assert len(teams.team_a) == len(teams.team_b) == 5


def test_form_teams_with_more_candidates_finds_the_closest_totals():
    players = list(range(1, 11))
    prefs = {p: "can" for p in players}
    contributions = {p: float(p) for p in players}
    teams = form_teams(players, prefs, (5, 5), contributions, random.Random(0))
    gap = abs(sum(teams.team_a) - sum(teams.team_b))
    assert gap <= 1


def test_form_teams_only_considers_wanting_captains_when_two_want():
    players = list(range(1, 11))
    prefs = {p: "can" for p in players}
    prefs[9] = prefs[10] = "want"
    teams = form_teams(players, prefs, (5, 5), {p: float(p) for p in players}, random.Random(0))
    assert {teams.captain_a, teams.captain_b} == {9, 10}


def test_form_teams_fills_uneven_team_sizes():
    players = list(range(1, 10))
    prefs = {p: "can" for p in players}
    teams = form_teams(players, prefs, (5, 4), {p: float(p) for p in players}, random.Random(0))
    assert (len(teams.team_a), len(teams.team_b)) == (5, 4)


def test_form_teams_needs_two_candidates():
    players = list(range(1, 11))
    prefs = {p: "no" for p in players}
    prefs[1] = "want"
    with pytest.raises(LobbyError):
        form_teams(players, prefs, (5, 5), {p: 0.0 for p in players}, random.Random(0))


def test_replace_in_place_takes_the_first_waiting_player():
    prefs = {p: "no" for p in range(1, 9)}
    prefs[1] = "want"
    assert replace_in_place([1, 2, 3, 4, 5], 1, 3, prefs, [6, 7]).__dict__ == {"newcomer": 6, "captain": 1}


def test_replace_in_place_hands_the_captaincy_to_a_teammate():
    prefs = {p: "no" for p in range(1, 9)}
    prefs[1] = "want"
    prefs[4] = "can"
    prefs[7] = "want"
    replacement = replace_in_place([1, 2, 3, 4, 5], 1, 1, prefs, [6, 7])
    assert (replacement.newcomer, replacement.captain) == (6, 4)


def test_replace_in_place_brings_in_a_waiting_captain_when_no_teammate_can():
    prefs = {p: "no" for p in range(1, 10)}
    prefs[1] = "want"
    prefs[7] = "can"
    prefs[8] = "want"
    replacement = replace_in_place([1, 2, 3, 4, 5], 1, 1, prefs, [6, 7, 8])
    assert (replacement.newcomer, replacement.captain) == (8, 8)


def test_replace_in_place_with_nobody_suitable_left():
    prefs = {p: "no" for p in range(1, 8)}
    prefs[1] = "want"
    replacement = replace_in_place([1, 2, 3, 4, 5], 1, 1, prefs, [6])
    assert (replacement.newcomer, replacement.captain) == (6, None)
    assert replace_in_place([1, 2, 3, 4, 5], 1, 2, prefs, []).newcomer is None


def test_replace_in_place_fills_a_missing_captain():
    prefs = {p: "no" for p in range(1, 8)}
    prefs[7] = "can"
    replacement = replace_in_place([1, 2, 3, 4, 5], None, 2, prefs, [6, 7])
    assert (replacement.newcomer, replacement.captain) == (7, 7)
