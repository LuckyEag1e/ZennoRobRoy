import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket import Podium
from labels import podium_lines, team_label

TEAMS = {1: {"name": "Alpha", "seed": 1}, 2: {"name": "Bravo", "seed": 4}, 3: {"name": "Charlie", "seed": None}}


def test_team_label_shows_a_seed_when_there_is_one():
    assert team_label(TEAMS, 1) == "(1) Alpha"
    assert team_label(TEAMS, 3) == "Charlie"
    assert team_label(TEAMS, None) == "TBD"


def test_podium_lines_leave_out_seeds():
    lines = podium_lines(Podium(champion=1, runner_up=2, third_places=[3]), TEAMS)
    assert lines == ["🥇 Alpha", "🥈 Bravo", "🥉 Charlie"]
