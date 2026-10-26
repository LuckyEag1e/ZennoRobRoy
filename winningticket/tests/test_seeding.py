import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bracket import BracketError
from seeding import bracket_positions, seeded_pairs


def test_bracket_positions_keep_top_seeds_apart():
    assert bracket_positions(2) == [1, 2]
    assert bracket_positions(4) == [1, 4, 2, 3]
    assert bracket_positions(8) == [1, 8, 4, 5, 2, 7, 3, 6]


def test_full_bracket_pairs_best_against_worst():
    assert seeded_pairs([10, 20, 30, 40]) == [(10, 40), (20, 30)]


def test_byes_go_to_the_top_seeds():
    pairs = seeded_pairs([1, 2, 3, 4, 5])
    assert len(pairs) == 4
    byes = [a for a, b in pairs if b is None]
    assert sorted(byes) == [1, 2, 3]
    assert (4, 5) in pairs or (5, 4) in pairs


@pytest.mark.parametrize("count", [2, 3, 6, 9, 16, 17])
def test_every_entrant_is_placed_once_with_no_empty_match(count):
    pairs = seeded_pairs(list(range(1, count + 1)))
    placed = [team for pair in pairs for team in pair if team is not None]
    assert sorted(placed) == list(range(1, count + 1))
    assert all(pair != (None, None) for pair in pairs)


@pytest.mark.parametrize("count", [0, 1])
def test_too_few_entrants_is_an_error(count):
    with pytest.raises(BracketError):
        seeded_pairs(list(range(count)))
