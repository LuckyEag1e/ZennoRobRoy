"""Withdrawals, walkovers and skipped matches, as the database records and works them out."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from bracket_changes import skip_and_describe, withdraw_and_describe
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


def test_a_withdrawal_hands_the_opponent_a_walkover_that_scores_no_picks(tournament):
    conn, tid, semi_1, _, _, team = tournament
    db.upsert_predictions(conn, tid, 7, {semi_1: team["A"]})
    db.withdraw_entrant(conn, tid, team["B"], "dropped", 2)
    outcomes = db.get_outcomes(conn, tid)
    assert outcomes.winners[semi_1] == team["A"]
    assert semi_1 in outcomes.walkovers
    [score] = db.load_scores(conn, tid)
    assert (score.points, score.possible_picks) == (0, 0)


def test_a_finalist_withdrawing_completes_the_tournament(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.withdraw_entrant(conn, tid, team["C"], "disqualified", 1)
    assert _status(conn, tid) == "completed"
    result, _, _ = db.load_final_results(conn, tid)
    assert (result.champion, result.runner_up) == (team["A"], None)


def test_reinstating_undoes_the_walkover_and_what_followed_it(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.withdraw_entrant(conn, tid, team["B"], "withdrawn", 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.record_result(conn, tid, final, team["A"], 1)
    assert db.reinstate_entrant(conn, tid, team["B"]) == [final]
    assert db.get_results(conn, tid) == {semi_2: team["C"]}
    assert db.get_teams(conn, tid)[team["B"]]["withdrawn"] is None
    assert _status(conn, tid) == "in_progress"


def test_a_skipped_final_completes_the_tournament_with_no_champion(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.skip_match(conn, tid, final, (team["A"], team["C"]), 1)
    assert _status(conn, tid) == "completed"
    assert db.final_decided(conn, tid)
    result, _, _ = db.load_final_results(conn, tid)
    assert result.champion is None


def test_recording_a_winner_replaces_a_skip(tournament):
    conn, tid, semi_1, _, _, team = tournament
    db.skip_match(conn, tid, semi_1, (team["A"], team["B"]), 1)
    db.record_result(conn, tid, semi_1, team["B"], 1)
    outcomes = db.get_outcomes(conn, tid)
    assert outcomes.skipped == set()
    assert outcomes.played == {semi_1: team["B"]}


def test_changing_an_earlier_result_drops_a_later_skip(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.record_result(conn, tid, semi_1, team["A"], 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    db.skip_match(conn, tid, final, (team["A"], team["C"]), 1)
    assert db.record_result(conn, tid, semi_1, team["B"], 1) == [final]
    assert final not in db.get_outcomes(conn, tid).decided
    assert db.match_now(conn, tid, final).skipped is False


def test_clearing_a_skip_reopens_the_match(tournament):
    conn, tid, semi_1, semi_2, final, team = tournament
    db.skip_match(conn, tid, semi_1, (team["A"], team["B"]), 1)
    db.record_result(conn, tid, semi_2, team["C"], 1)
    assert db.get_outcomes(conn, tid).walkovers == {final}
    db.clear_result(conn, tid, semi_1)
    assert db.get_outcomes(conn, tid).decided == {semi_2}


def test_walkovers_dont_count_toward_ratings(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tid = import_tournament(conn, 1, "Cup", 1, parse_csv("team_a,team_b\nA,B\nC,D\n"))
    teams = {row["name"]: team_id for team_id, row in db.get_teams(conn, tid).items()}
    for user_id, name in enumerate("ABCD", start=10):
        db.link_team_member(conn, tid, teams[name], user_id)
    matches_by_id, _, _ = db.load_bracket(conn, tid)
    semi_1, semi_2, _ = sorted(matches_by_id.values(), key=bracket_order)
    db.record_result(conn, tid, semi_1.id, teams["A"], 1)
    db.withdraw_entrant(conn, tid, teams["D"], "dropped", 13)
    db.withdraw_entrant(conn, tid, teams["C"], "dropped", 12)
    db.set_tournament_status(conn, tid, "concluded")
    history = db.load_match_history(conn, 1)
    assert [(game.winners, game.losers) for game in history] == [((10,), (11,))]
    conn.close()


def test_announcements_say_who_withdrew_and_who_advances(tournament):
    conn, tid, semi_1, semi_2, _, team = tournament
    db.record_result(conn, tid, semi_2, team["C"], 1)
    tournament_row = db.get_tournament(conn, tid)
    lines, dropped = withdraw_and_describe(conn, tournament_row, db.get_teams(conn, tid)[team["B"]], "dropped", 2, str)
    assert dropped == []
    assert lines == [
        "🚪 **B** dropped out of **Test**.",
        "Round 1: **A** wins by walkover. **A** advances to Round 2 to face C.",
    ]


def test_a_skip_announcement_follows_the_walkover_it_gives(tournament):
    conn, tid, semi_1, semi_2, _, team = tournament
    db.record_result(conn, tid, semi_2, team["C"], 1)
    lines, _ = skip_and_describe(conn, db.get_tournament(conn, tid), semi_1, (team["A"], team["B"]), 1,
                                 lambda user_id: f"user {user_id}")
    assert lines == [
        "📣 Round 1: **A** vs **B** was called off by user 1 with no winner.",
        "Round 2: **C** wins by walkover. 🏆 **C** is the champion of Test!",
    ]


def test_older_databases_get_the_withdrawal_columns(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TABLE teams;
        CREATE TABLE teams (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            seed            INTEGER
        );
        DROP TABLE skipped_matches;
        PRAGMA foreign_keys = ON;
    """)
    tid = db.create_tournament(conn, 1, "Old", 1)
    team_id = db.insert_team(conn, tid, "A", None)
    db.init_db(conn)
    assert db.get_teams(conn, tid)[team_id]["withdrawn"] is None
    conn.execute("UPDATE teams SET withdrawn = 'dropped' WHERE id = ?", (team_id,))
    with pytest.raises(Exception):
        conn.execute("UPDATE teams SET withdrawn = 'bored' WHERE id = ?", (team_id,))
    assert conn.execute("SELECT COUNT(*) FROM skipped_matches").fetchone()[0] == 0
    conn.close()
