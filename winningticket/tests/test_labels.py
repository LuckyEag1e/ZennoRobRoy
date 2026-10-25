import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket import Podium, UserScore
from labels import join_within, pickem_podium_lines, podium_lines, team_label

TEAMS = {1: {"name": "Alpha", "seed": 1}, 2: {"name": "Bravo", "seed": 4}, 3: {"name": "Charlie", "seed": None}}


def test_team_label_shows_a_seed_when_there_is_one():
    assert team_label(TEAMS, 1) == "(1) Alpha"
    assert team_label(TEAMS, 3) == "Charlie"
    assert team_label(TEAMS, None) == "TBD"


def test_podium_lines_leave_out_seeds():
    lines = podium_lines(Podium(champion=1, runner_up=2, third_places=[3]), TEAMS)
    assert lines == ["🥇 Alpha", "🥈 Bravo", "🥉 Charlie"]


def test_pickem_lines_explain_an_empty_podium():
    assert pickem_podium_lines([], str) == ["Nobody made predictions."]
    nobody_scored = [UserScore(user_id=1, points=0, correct_picks=0, possible_picks=3)]
    assert pickem_podium_lines(nobody_scored, str) == ["Nobody scored any points."]


def test_join_within_drops_lines_that_dont_fit_and_says_how_many():
    lines = [f"line {i}" for i in range(10)]
    assert join_within(lines, 1000) == "\n".join(lines)
    text = join_within(lines, 40)
    assert len(text) <= 40
    assert text.startswith("line 0\nline 1")
    assert text.endswith("more")
