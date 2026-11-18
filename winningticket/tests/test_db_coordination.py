"""Calling matches, readying up and forfeits, as the database records them."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import coordination
import db
from bracket import bracket_order
from csv_import import import_tournament, parse_csv

CHANNEL = 555


@pytest.fixture
def tournament(tmp_path):
    """An in-progress 4-team tournament, A vs B and C vs D, every entrant linked, with two
    matches called at once."""
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tid = import_tournament(conn, 1, "Test", 1, parse_csv("team_a,team_b\nA,B\nC,D\n"))
    teams = {row["name"]: team_id for team_id, row in db.get_teams(conn, tid).items()}
    for user_id, name in enumerate("ABCD", start=10):
        db.link_team_member(conn, tid, teams[name], user_id)
    db.set_match_slots(conn, tid, 2)
    db.set_tournament_status(conn, tid, "in_progress")
    db.set_tournament_channel_if_missing(conn, tid, CHANNEL)
    semi_1, semi_2, _ = (m.id for m in sorted(db.get_matches(conn, tid).values(), key=bracket_order))
    yield conn, tid, teams, semi_1, semi_2
    conn.close()


def _tend(conn, tid, rooms, minutes=0):
    now = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    tournament = db.get_tournament(conn, tid)
    coordination.tend(conn, tournament, now, rooms)
    coordination.call_queued(conn, tournament["guild_id"], rooms)


def test_no_more_matches_are_called_than_there_are_rooms(tournament):
    conn, tid, teams, semi_1, semi_2 = tournament
    _tend(conn, tid, rooms=[1])
    assert [c["match_id"] for c in db.get_calls(conn, tid)] == [semi_1]
    _tend(conn, tid, rooms=[1, 2])
    assert {c["match_id"] for c in db.get_calls(conn, tid)} == {semi_1, semi_2}


def test_a_room_is_held_by_one_started_match_at_a_time(tournament):
    conn, tid, teams, semi_1, semi_2 = tournament
    _tend(conn, tid, rooms=[1, 2])
    for match_id, users in ((semi_1, (10, 11)), (semi_2, (12, 13))):
        for user_id in users:
            coordination.ready_up(conn, match_id, user_id, [7])
    rooms = {c["match_id"]: c["room_id"] for c in db.get_calls(conn, tid)}
    assert rooms == {semi_1: 7, semi_2: None}


def test_readying_up_just_before_the_forfeit_stops_it(tournament):
    conn, tid, teams, semi_1, semi_2 = tournament
    _tend(conn, tid, rooms=[1, 2])
    _tend(conn, tid, rooms=[1, 2], minutes=6)  # the reminder
    [stale] = [c for c in db.get_calls(conn, tid) if c["match_id"] == semi_1]
    coordination.ready_up(conn, semi_1, 10, [1, 2])
    coordination.ready_up(conn, semi_1, 11, [1, 2])
    assert not db.forfeit_match(conn, tid, stale)
    assert semi_1 not in db.get_outcomes(conn, tid).decided


def test_a_decided_match_frees_its_call(tournament):
    conn, tid, teams, semi_1, semi_2 = tournament
    _tend(conn, tid, rooms=[1, 2])
    db.record_result(conn, tid, semi_1, teams["A"], 1)
    assert [c["match_id"] for c in db.get_calls(conn, tid)] == [semi_2]


def test_older_databases_get_the_coordination_columns(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TABLE match_results;
        CREATE TABLE match_results (
            match_id        INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
            winner_team_id  INTEGER NOT NULL REFERENCES teams(id),
            recorded_by     INTEGER NOT NULL,
            recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        PRAGMA foreign_keys = ON;
        PRAGMA user_version = 7;
    """)
    tid = import_tournament(conn, 1, "Old", 1, parse_csv("team_a,team_b\nA,B\n"))
    [final] = db.get_matches(conn, tid).values()
    conn.execute("INSERT INTO match_results (match_id, winner_team_id, recorded_by) VALUES (?, ?, 1)",
                 (final.id, final.team_a_id))
    conn.commit()
    db.init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_tournament(conn, tid)["match_slots"] is None
    assert db.get_outcomes(conn, tid).played == {final.id: final.team_a_id}  # old results were all played
    conn.close()


def _forfeit_semi_1(conn, tid):
    """Call both semis, ready up only A, and let B's time run out."""
    _tend(conn, tid, rooms=[1, 2])
    coordination.ready_up(conn, db.get_calls(conn, tid)[0]["match_id"], 10, [1, 2])
    _tend(conn, tid, rooms=[1, 2], minutes=6)  # the reminder
    _tend(conn, tid, rooms=[1, 2], minutes=12)  # the forfeit


def test_a_forfeit_is_a_recorded_walkover_an_organizer_can_still_change(tournament):
    conn, tid, teams, semi_1, semi_2 = tournament
    _forfeit_semi_1(conn, tid)
    outcomes = db.get_outcomes(conn, tid)
    assert outcomes.winners[semi_1] == teams["A"] and semi_1 in outcomes.forfeits
    assert semi_1 not in outcomes.from_withdrawals
    db.record_result(conn, tid, semi_1, teams["B"], 1)  # they played after all
    assert db.get_outcomes(conn, tid).played[semi_1] == teams["B"]


def test_older_databases_move_report_posts_into_match_posts(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    tid = import_tournament(conn, 1, "Old", 1, parse_csv("team_a,team_b\nA,B\n"))
    [final] = db.get_matches(conn, tid).values()
    conn.executescript(f"""
        CREATE TABLE report_posts (
            match_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, team_a_id INTEGER NOT NULL,
            team_b_id INTEGER NOT NULL, message_id INTEGER, updated_at TEXT NOT NULL, shown TEXT);
        INSERT INTO report_posts VALUES ({final.id}, 5, {final.team_a_id}, {final.team_b_id}, 77,
                                         '2026-01-01T00:00:00.000Z', 'warning');
        CREATE TABLE replaced_report_posts (message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL);
        INSERT INTO replaced_report_posts VALUES (78, 5);
    """)
    db.init_db(conn)
    post = db.get_match_post(conn, final.id)
    assert (post["message_id"], post["shown"], post["warned_at"]) == (77, "warning", "2026-01-01T00:00:00.000Z")
    assert [(r["message_id"], r["channel_id"]) for r in db.get_replaced_match_posts(conn)] == [(78, 5)]
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"report_posts", "replaced_report_posts"} & tables
    conn.close()


def _eight_entrant_tournament(conn, name, slots, first_user_id):
    """An in-progress 8-entrant tournament in server 1, every entrant linked, with `slots`
    matches called at once. Returns its id and round-1 match ids in bracket order."""
    pairs = "\n".join(f"{name}{n},{name}{n + 1}" for n in range(1, 9, 2))
    tid = import_tournament(conn, 1, name, 1, parse_csv(f"team_a,team_b\n{pairs}\n"))
    for user_id, team_id in enumerate(sorted(db.get_teams(conn, tid)), start=first_user_id):
        db.link_team_member(conn, tid, team_id, user_id)
    db.set_match_slots(conn, tid, slots)
    db.set_tournament_status(conn, tid, "in_progress")
    db.set_tournament_channel_if_missing(conn, tid, CHANNEL)
    round_1 = [m.id for m in sorted(db.get_matches(conn, tid).values(), key=bracket_order) if m.round == 1]
    return tid, round_1


@pytest.fixture
def server(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def _called(conn, tid):
    return [call["match_id"] for call in db.get_calls(conn, tid)]


def _decide(conn, tid, match_id):
    teams = db.match_now(conn, tid, match_id).teams
    db.record_result(conn, tid, match_id, teams[0], 1)


def _tend_server(conn, rooms):
    """One check of the bot's loop: every coordinated tournament, in the order it takes them."""
    for tournament in db.get_coordinated_tournaments(conn):
        coordination.tend(conn, tournament, datetime.now(timezone.utc), rooms)
    coordination.call_queued(conn, 1, rooms)


def test_only_as_many_matches_as_places_are_called_and_a_free_place_is_refilled(server):
    tid, round_1 = _eight_entrant_tournament(server, "Cup", slots=2, first_user_id=10)
    _tend_server(server, rooms=[1, 2, 3])
    assert _called(server, tid) == round_1[:2]
    _decide(server, tid, round_1[0])
    _tend_server(server, rooms=[1, 2, 3])
    assert _called(server, tid) == [round_1[1], round_1[2]]


def test_lowering_the_places_lets_calls_finish_before_calling_more(server):
    tid, round_1 = _eight_entrant_tournament(server, "Cup", slots=2, first_user_id=10)
    _tend_server(server, rooms=[1, 2])
    db.set_match_slots(server, tid, 1)
    _decide(server, tid, round_1[0])
    _tend_server(server, rooms=[1, 2])
    assert _called(server, tid) == [round_1[1]]  # still one called, which is all one place allows
    _decide(server, tid, round_1[1])
    _tend_server(server, rooms=[1, 2])
    assert _called(server, tid) == [round_1[2]]


def test_raising_the_places_calls_another_match_on_the_next_check(server):
    tid, round_1 = _eight_entrant_tournament(server, "Cup", slots=1, first_user_id=10)
    _tend_server(server, rooms=[1, 2])
    db.set_match_slots(server, tid, 2)
    _tend_server(server, rooms=[1, 2])
    assert _called(server, tid) == round_1[:2]


def test_tournaments_in_one_server_share_its_rooms(server):
    cup, cup_round_1 = _eight_entrant_tournament(server, "Cup", slots=2, first_user_id=10)
    shield, shield_round_1 = _eight_entrant_tournament(server, "Shield", slots=2, first_user_id=20)
    _tend_server(server, rooms=[1, 2, 3])
    assert len(_called(server, cup)) + len(_called(server, shield)) == 3
    assert db.count_server_calls(server, 1) == 3


def test_a_match_that_became_ready_later_waits_behind_another_tournaments(server):
    cup, cup_round_1 = _eight_entrant_tournament(server, "Cup", slots=2, first_user_id=10)
    _tend_server(server, rooms=[1, 2])
    shield, shield_round_1 = _eight_entrant_tournament(server, "Shield", slots=2, first_user_id=20)
    _tend_server(server, rooms=[1, 2])  # Shield's matches queue behind Cup's two still waiting
    for match_id in cup_round_1[:2]:
        _decide(server, cup, match_id)
        _tend_server(server, rooms=[1, 2])
    assert _called(server, cup) == cup_round_1[2:]  # Cup's semi-final is ready now, but last in line
    assert _called(server, shield) == []

    _decide(server, cup, cup_round_1[2])
    _tend_server(server, rooms=[1, 2])
    assert _called(server, shield) == [shield_round_1[0]]
    assert _called(server, cup) == [cup_round_1[3]]


def test_a_match_whose_tournament_has_no_place_free_lets_the_next_in_line_go(server):
    cup, cup_round_1 = _eight_entrant_tournament(server, "Cup", slots=1, first_user_id=10)
    shield, shield_round_1 = _eight_entrant_tournament(server, "Shield", slots=2, first_user_id=20)
    _tend_server(server, rooms=[1, 2])
    assert _called(server, cup) == [cup_round_1[0]]
    assert _called(server, shield) == [shield_round_1[0]]


def test_turning_calls_off_takes_a_tournaments_matches_out_of_the_queue(server):
    cup, _ = _eight_entrant_tournament(server, "Cup", slots=1, first_user_id=10)
    _tend_server(server, rooms=[1])
    db.set_match_slots(server, cup, None)
    assert db.get_match_queue(server, 1) == []
