"""Players' votes on their match results, as the database records and settles them."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
import reports
from bracket import bracket_order
from csv_import import import_tournament, parse_csv

CHANNEL = 555


@pytest.fixture
def tournament(tmp_path):
    """A 4-team tournament in progress: A vs B and C vs D, then the final."""
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tournament_id = import_tournament(conn, 1, "Test", 1, parse_csv("team_a,team_b\nA,B\nC,D\n"))
    db.set_tournament_status(conn, tournament_id, "in_progress")
    matches_by_id, _, teams_by_id = db.load_bracket(conn, tournament_id)
    semi_1 = min(matches_by_id.values(), key=bracket_order).id
    team = {row["name"]: team_id for team_id, row in teams_by_id.items()}
    yield conn, tournament_id, semi_1, team
    conn.close()


def _vote(conn, tid, match_id, side, winner):
    return db.submit_report(conn, tid, match_id, side, winner, 100 + side, CHANNEL)


def test_matching_votes_record_the_result_and_clear_the_votes(tournament):
    conn, tid, semi_1, team = tournament
    assert _vote(conn, tid, semi_1, team["A"], team["A"]) == "waiting"
    assert _vote(conn, tid, semi_1, team["B"], team["A"]) == "agreed"
    assert db.get_outcomes(conn, tid).played == {semi_1: team["A"]}
    assert db.get_reports(conn, semi_1) == []


def test_each_side_claiming_the_win_is_a_dispute(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    assert _vote(conn, tid, semi_1, team["B"], team["B"]) == "disputed"
    assert db.get_outcomes(conn, tid).decided == set()
    assert len(db.get_reports(conn, semi_1)) == 2


def test_changing_a_vote_can_settle_a_dispute(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    _vote(conn, tid, semi_1, team["B"], team["B"])
    assert _vote(conn, tid, semi_1, team["A"], team["B"]) == "agreed"
    assert db.get_results(conn, tid) == {semi_1: team["B"]}


def test_each_side_naming_the_other_calls_the_match_off(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["B"])
    assert _vote(conn, tid, semi_1, team["B"], team["A"]) == "skipped"
    assert db.get_outcomes(conn, tid).skipped == {semi_1}


def test_a_late_answer_stops_the_win_being_given(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    [stale] = [vote for vote in db.get_open_reports(conn) if vote["match_id"] == semi_1]
    _vote(conn, tid, semi_1, team["B"], team["B"])
    assert not db.give_reported_win(conn, tid, stale)
    assert db.get_outcomes(conn, tid).decided == set()


def test_withdrawing_an_entrant_drops_the_votes_on_their_match(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    db.withdraw_entrant(conn, tid, team["B"], "dropped", 101)
    assert db.get_reports(conn, semi_1) == []


def test_due_steps_warn_a_claim_and_wait_out_a_concession(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    second = min((m for m in db.get_matches(conn, tid).values() if m.id != semi_1 and m.round == 1), key=bracket_order).id
    _vote(conn, tid, second, team["C"], team["D"])
    later = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert [(step.kind, step.vote["match_id"]) for step in reports.due_steps(conn, later)] == [("warn", semi_1)]
    much_later = datetime.now(timezone.utc) + timedelta(minutes=11)
    assert {(step.kind, step.vote["match_id"]) for step in reports.due_steps(conn, much_later)} == {
        ("warn", semi_1), ("give", second)}


def test_a_warned_vote_is_given_once_the_warning_has_had_its_time(tournament):
    conn, tid, semi_1, team = tournament
    _vote(conn, tid, semi_1, team["A"], team["A"])
    reports.warn(conn, db.get_open_reports(conn)[0])
    soon = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert reports.due_steps(conn, soon) == []  # the warning hasn't been sent yet
    db.set_match_post(conn, semi_1, 1, "warning", "warning")
    assert [step.kind for step in reports.due_steps(conn, soon)] == ["give"]
