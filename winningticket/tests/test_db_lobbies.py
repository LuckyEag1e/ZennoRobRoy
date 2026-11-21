import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
import lobby_db
import lobby_flow
import lobbies
from tournament_builder import RegistrationClosedError, close_lobby_registration, lobby_split_options

ORGANIZER = 999


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def open_event(conn, name="Cup", spread="balanced") -> int:
    return db.open_registration(conn, 1, name, ORGANIZER, 555, None, format="lobbies",
                                starts_at=db.to_db_time(datetime.now(timezone.utc) + timedelta(days=1)),
                                lobby_spread=spread)


def sign_up(conn, tournament_id, user_id, pref="no", linked=True) -> int:
    team_id, _ = db.register_entrant(conn, tournament_id, f"P{user_id}", user_id if linked else None)
    lobby_db.set_captain_pref(conn, tournament_id, team_id, pref)
    return team_id


def event_with(conn, players: int, captains: int) -> int:
    tournament_id = open_event(conn)
    for user_id in range(1, players + 1):
        sign_up(conn, tournament_id, user_id, "want" if user_id <= captains else "no")
    return tournament_id


def team_of(conn, tournament_id, user_id) -> int:
    return db.get_entrant_team_id(conn, tournament_id, user_id)


def test_close_draws_lobbies_with_two_captains_and_a_waitlist(conn):
    tournament_id = event_with(conn, 23, captains=4)
    split = close_lobby_registration(conn, tournament_id, rng=random.Random(1))
    assert split == lobbies.Split((10, 10), 3)
    tournament = db.get_tournament(conn, tournament_id)
    assert tournament["status"] == "in_progress"
    drawn = lobby_db.get_lobbies(conn, tournament_id)
    assert len(drawn) == 2
    for lobby in drawn:
        assert len(lobby.captains) == 2
        assert [len(lobby.teams[side].players) for side in "AB"] == [5, 5]
    # The latest three sign-ups wait, in sign-up order.
    assert lobby_db.get_waitlist(conn, tournament_id) == [team_of(conn, tournament_id, u) for u in (21, 22, 23)]
    keys = [post["post_key"] for post in lobby_db.get_posts(conn, tournament_id)]
    assert keys == ["event"]


def test_close_refuses_without_enough_captains(conn):
    tournament_id = event_with(conn, 10, captains=1)
    assert lobby_split_options(conn, tournament_id) == (None, [])
    with pytest.raises(lobbies.LobbyError):
        close_lobby_registration(conn, tournament_id)
    assert db.get_tournament(conn, tournament_id)["status"] == "registration"


def test_name_only_entrants_never_captain(conn):
    tournament_id = open_event(conn)
    for user_id in range(1, 11):
        sign_up(conn, tournament_id, user_id, "want", linked=user_id > 8)
    close_lobby_registration(conn, tournament_id, rng=random.Random(2))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    assert sorted(lobby.captains) == sorted(team_of(conn, tournament_id, u) for u in (9, 10))


def test_close_can_only_happen_once(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id)
    with pytest.raises(RegistrationClosedError):
        close_lobby_registration(conn, tournament_id)


def test_dropout_is_replaced_in_place_from_the_waitlist(conn):
    tournament_id = event_with(conn, 12, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(3))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    side = "A"
    leaving = next(p for p in lobby.teams[side].players if p not in lobby.captains)
    spot = lobby.teams[side].players.index(leaving)
    first_waiting = lobby_db.get_waitlist(conn, tournament_id)[0]
    change = lobby_db.leave(conn, tournament_id, leaving, "dropped", 5)
    assert change.newcomer == first_waiting
    after = lobby_db.get_lobby(conn, lobby.id)
    assert after.teams[side].players[spot] == first_waiting
    assert after.teams["B"].players == lobby.teams["B"].players
    assert db.get_teams(conn, tournament_id)[leaving]["withdrawn"] == "dropped"


def test_captain_dropout_brings_in_a_waiting_candidate_when_no_teammate_can(conn):
    tournament_id = open_event(conn)
    for user_id in range(1, 11):
        sign_up(conn, tournament_id, user_id, "want" if user_id <= 2 else "no")
    waiting_plain = sign_up(conn, tournament_id, 11, "no")
    waiting_captain = sign_up(conn, tournament_id, 12, "can")
    # 12 more than fits, so 11 and 12 wait; captains aren't needed from them yet.
    close_lobby_registration(conn, tournament_id, rng=random.Random(4))
    assert lobby_db.get_waitlist(conn, tournament_id) == [waiting_plain, waiting_captain]
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    captain = lobby.teams["A"].captain
    change = lobby_db.leave(conn, tournament_id, captain, "dropped", 1)
    assert change.newcomer == waiting_captain
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == waiting_captain


def test_finish_checkin_replaces_no_shows_with_checked_in_waiters(conn):
    tournament_id = event_with(conn, 12, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(5))
    tournament = db.get_tournament(conn, tournament_id)
    now = datetime.now(timezone.utc)
    lobby_flow.start_checkin(conn, tournament, 15, now - timedelta(minutes=20))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    no_show = next(p for p in lobby.players if p not in lobby.captains)
    waiting_first, waiting_second = lobby_db.get_waitlist(conn, tournament_id)
    for player in lobby.players + [waiting_second]:
        if player != no_show:
            lobby_db.check_in(conn, player)
    assert lobby_db.get_checkins_due(conn)
    changes = lobby_db.finish_checkin(conn, tournament_id)
    # The first waiter didn't check in, so the second takes the spot.
    assert [(c.leaving, c.newcomer) for c in changes] == [(no_show, waiting_second)]
    assert lobby_db.finish_checkin(conn, tournament_id) is None
    assert lobby_db.get_waitlist(conn, tournament_id) == [waiting_first]


def test_report_confirm_and_completion(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(6))
    tournament = db.get_tournament(conn, tournament_id)
    lobby_flow.start_checkin(conn, tournament, 15, datetime.now(timezone.utc) - timedelta(minutes=20))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    for player in lobby.players:
        lobby_db.check_in(conn, player)
    lobby_db.finish_checkin(conn, tournament_id)
    tournament = db.get_tournament(conn, tournament_id)
    users = {team_id: users[0] for team_id, users in db.get_members_by_team(conn, tournament_id).items()}
    captain_a, captain_b = users[lobby.teams["A"].captain], users[lobby.teams["B"].captain]
    role_a = lobby_flow.role_in(conn, lobby, captain_a, organizer=False)

    tied = lobby_flow.parse_points(lobby, {p: "5" for p in lobby.players})
    assert isinstance(tied, str) and "tiebreaker" in tied
    points = lobby_flow.parse_points(lobby, {p: str(10 if p in lobby.teams["A"].players else 3) for p in lobby.players})
    assert "has no room yet" in lobby_flow.submit_report(conn, tournament, lobby, role_a, captain_a, points)
    lobby_flow.submit_room_code(conn, tournament, lobby, role_a, captain_a, "ROOM1")
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    lobby_flow.submit_report(conn, tournament, lobby, role_a, captain_a, points)
    assert "Only the other" in lobby_flow.confirm_report(conn, lobby, role_a, captain_a)
    role_b = lobby_flow.role_in(conn, lobby, captain_b, organizer=False)
    assert "Confirmed" in lobby_flow.confirm_report(conn, lobby, role_b, captain_b)
    assert lobby_db.get_result(conn, lobby.id).winning_side == "A"
    assert db.get_tournament(conn, tournament_id)["status"] == "completed"


def test_unanswered_report_is_accepted_and_a_dispute_holds(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(7))
    tournament = db.get_tournament(conn, tournament_id)
    lobby_flow.start_checkin(conn, tournament, 15, datetime.now(timezone.utc) - timedelta(minutes=20))
    # Nobody checked in, but nobody is waiting to replace them, so they all keep their spots.
    assert lobby_db.finish_checkin(conn, tournament_id) == []
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    assert len(lobby.players) == 10
    points = {p: (7 if p in lobby.teams["B"].players else 1) for p in lobby.players}
    lobby_db.submit_report(conn, lobby.id, 1, "A", points)
    report = lobby_db.get_report(conn, lobby.id)
    lobby_flow.accept_due_reports(conn, datetime.now(timezone.utc))
    assert lobby_db.get_result(conn, lobby.id) is None
    lobby_db.dispute_report(conn, report)
    lobby_flow.accept_due_reports(conn, datetime.now(timezone.utc) + timedelta(hours=1))
    assert lobby_db.get_result(conn, lobby.id) is None
    lobby_db.submit_report(conn, lobby.id, 1, "A", points)  # a new report clears the dispute
    lobby_flow.accept_due_reports(conn, datetime.now(timezone.utc) + timedelta(hours=1))
    assert lobby_db.get_result(conn, lobby.id).winning_side == "B"


def test_history_feeds_contribution_into_the_next_draw(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(8))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    points = {p: (5 if p in lobby.teams["A"].players else 1) for p in lobby.players}
    lobby_db.record_result(conn, lobby.id, points, ORGANIZER)
    db.set_tournament_status(conn, tournament_id, "concluded")
    history = lobby_db.load_lobby_history(conn, 1)
    assert len(history) == 1
    assert set(history[0].winners) == set(lobby.teams["A"].players)


def test_upgrade_from_version_8_keeps_tournaments(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    db.create_tournament(conn, 1, "Old Cup", 5)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    db.init_db(conn)
    assert db.get_tournament_by_name(conn, 1, "Old Cup")["format"] == "single_elim"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert open_event(conn, "New Cup")


def test_a_late_signup_fills_a_team_with_room_before_the_waitlist(conn):
    tournament_id = event_with(conn, 7, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(1))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    small = min("AB", key=lambda side: len(lobby.teams[side].players))
    before = {side: list(lobby.teams[side].players) for side in "AB"}
    late = sign_up(conn, tournament_id, 50)
    assert lobby_db.place_late_signup(conn, tournament_id, late) is not None
    lobby = lobby_db.get_lobby(conn, lobby.id)
    assert lobby.teams[small].players == before[small] + [late]
    other = "B" if small == "A" else "A"
    assert lobby.teams[other].players == before[other]
    # Now 4v4, then 5v4, then 5v5: the next two play, and the one after waits.
    for user_id in (51, 52):
        assert lobby_db.place_late_signup(conn, tournament_id, sign_up(conn, tournament_id, user_id)) is not None
    waiting = sign_up(conn, tournament_id, 53)
    assert lobby_db.place_late_signup(conn, tournament_id, waiting) is None
    assert lobby_db.get_waitlist(conn, tournament_id) == [waiting]
