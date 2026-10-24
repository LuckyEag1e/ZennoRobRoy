import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from csv_import import import_tournament, parse_csv


@pytest.fixture
def tournament(tmp_path):
    """A 4-team tournament: round 1 is A vs B and C vs D, then the final."""
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tournament_id = import_tournament(conn, 1, "Test", 1, parse_csv("team_a,team_b\nA,B\nC,D\n"))
    db.set_tournament_status(conn, tournament_id, "in_progress")
    matches_by_id, _, teams_by_id = db.load_bracket(conn, tournament_id)
    semi_1, semi_2, final = sorted(matches_by_id.values(), key=bracket_order)
    team = {row["name"]: team_id for team_id, row in teams_by_id.items()}
    yield conn, tournament_id, semi_1.id, semi_2.id, final.id, team
    conn.close()


def _status(conn, tournament_id):
    return db.get_tournament(conn, tournament_id)["status"]


def test_recording_the_final_completes_the_tournament(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    assert _status(conn, tid) == "in_progress"
    db.record_result(conn, tid, final, team["A"], 1)
    assert _status(conn, tid) == "completed"


def test_changing_an_earlier_result_drops_the_final_and_reopens(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.record_result(conn, tid, final, team["A"], 1)

    dropped = db.record_result(conn, tid, semi_1, team["B"], 1)

    assert dropped == [final]
    assert final not in db.get_results(conn, tid)
    assert _status(conn, tid) == "in_progress"


def test_clearing_the_final_reopens_the_tournament(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.record_result(conn, tid, final, team["C"], 1)

    assert db.clear_result(conn, tid, final) == []
    assert _status(conn, tid) == "in_progress"


def test_is_next_status_only_allows_one_step_forward():
    assert db.is_next_status("setup", "predictions_open")
    assert db.is_next_status("predictions_open", "in_progress")
    assert not db.is_next_status("in_progress", "predictions_open")
    assert not db.is_next_status("completed", "predictions_open")
    assert not db.is_next_status("setup", "in_progress")
