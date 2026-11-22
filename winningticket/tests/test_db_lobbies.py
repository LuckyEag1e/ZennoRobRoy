import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
import lobby_db
import lobby_flow
import lobby_posts
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


def sign_up(conn, tournament_id, user_id, pref="no", linked=True, place=False) -> int:
    """Sign a player up; with `place`, after the draw, as a late sign-up."""
    team_id, _ = db.register_entrant(conn, tournament_id, f"P{user_id}", user_id if linked else None,
                                     also=lambda team_id: lobby_db.save_signup(conn, tournament_id, team_id, pref, place))
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


def _playing(conn, seed):
    """A drawn 5v5 lobby past check-in, with its room set up: (tournament, lobby, captain
    A's user id and role, captain B's user id and role)."""
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(seed))
    tournament = db.get_tournament(conn, tournament_id)
    lobby_flow.start_checkin(conn, tournament, 15, datetime.now(timezone.utc) - timedelta(minutes=20))
    lobby_db.finish_checkin(conn, tournament_id)
    tournament = db.get_tournament(conn, tournament_id)
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    users = {team_id: users[0] for team_id, users in db.get_members_by_team(conn, tournament_id).items()}
    captain_a, captain_b = users[lobby.teams["A"].captain], users[lobby.teams["B"].captain]
    role_a = lobby_flow.role_in(conn, lobby, captain_a, organizer=False)
    role_b = lobby_flow.role_in(conn, lobby, captain_b, organizer=False)
    assert "has no room yet" in lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    lobby_flow.submit_room_code(conn, tournament, lobby, role_a, captain_a, "ROOM1")
    return tournament, lobby_db.get_lobby(conn, lobby.id), (captain_a, role_a), (captain_b, role_b)


def _points(lobby, a_scores=10, b_scores=3):
    return {p: (a_scores if p in lobby.teams["A"].players else b_scores) for p in lobby.players}


def test_both_captains_finish_then_matching_points_are_final(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 6)
    points = _points(lobby)
    assert "both captains press" in lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, points)
    assert "Waiting for" in lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    assert "already pressed" in lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    assert lobby_db.get_post(conn, tournament["id"], lobby_db.results_post_key(lobby.id)) is None
    assert "results post" in lobby_flow.finish(conn, tournament, lobby, role_b, captain_b)
    assert lobby_db.get_post(conn, tournament["id"], lobby_db.results_post_key(lobby.id)) is not None
    organizer = lobby_flow.Role(None, None, False, True)
    assert "Only lobby" in lobby_flow.enter_points(conn, tournament, lobby, organizer, ORGANIZER, points)
    assert "They're final once" in lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, points)
    assert "result is in" in lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, points)
    assert lobby_db.get_result(conn, lobby.id).winning_side == "A"
    assert db.get_tournament(conn, tournament["id"])["status"] == "completed"


def test_captains_enter_a_team_at_a_time(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 11)
    lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    lobby_flow.finish(conn, tournament, lobby, role_b, captain_b)
    points = _points(lobby)
    team = {side: {p: points[p] for p in lobby.teams[side].players} for side in "AB"}
    assert lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, team["B"]) is None
    post = lobby_posts.describe_results(conn, lobby, str)
    assert "still to enter" in post.text
    assert "They're final once" in lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, team["A"])
    # Half an entry beside a complete one isn't compared, and doesn't stop the complete one's time.
    assert lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, {**team["A"], **{
        p: 0 for p in team["A"]}}) is None
    assert lobby_db.mismatched_players(lobby, lobby_db.get_entries(conn, lobby.id)) == []
    lobby_flow.accept_due_entries(conn, datetime.now(timezone.utc) + timedelta(minutes=11))
    assert lobby_db.get_result(conn, lobby.id).points == points


def test_points_that_tie_once_complete_are_refused(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 12)
    lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    lobby_flow.finish(conn, tournament, lobby, role_b, captain_b)
    level = _points(lobby, 4, 4)
    lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, {p: level[p] for p in lobby.teams["A"].players})
    reply = lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a,
                                    {p: level[p] for p in lobby.teams["B"].players})
    assert "level on points" in reply
    assert not any(p in lobby_db.get_entries(conn, lobby.id)["A"].points for p in lobby.teams["B"].players)


def test_one_captains_points_are_accepted_when_the_other_enters_none(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 7)
    organizer = lobby_flow.Role(None, None, False, True)
    assert "Finished lobby" in lobby_flow.finish(conn, tournament, lobby, organizer, ORGANIZER)
    lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, _points(lobby, 1, 7))
    lobby_flow.accept_due_entries(conn, datetime.now(timezone.utc))
    assert lobby_db.get_result(conn, lobby.id) is None
    lobby_flow.accept_due_entries(conn, datetime.now(timezone.utc) + timedelta(minutes=11))
    assert lobby_db.get_result(conn, lobby.id).winning_side == "B"


def test_points_that_dont_match_wait_until_corrected_or_finalized(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 8)
    lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    lobby_flow.finish(conn, tournament, lobby, role_b, captain_b)
    points = _points(lobby)
    lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, points)
    wrong = dict(points)
    first = lobby.players[0]
    wrong[first] = 2
    reply = lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, wrong)
    first_name = db.get_teams(conn, tournament["id"])[first]["name"]
    assert "don't match" in reply and first_name in reply
    post = lobby_posts.describe_results(conn, lobby, lambda user_id: f"<@{user_id}>")
    assert "The points don't match" in post.text and lobby_posts.FINALIZE in [b.label for b in post.buttons]
    assert {user_id for _, user_id in post.audience} == {captain_a, captain_b}
    # Mismatched points never time out; the captain who was wrong corrects theirs.
    lobby_flow.accept_due_entries(conn, datetime.now(timezone.utc) + timedelta(hours=1))
    assert lobby_db.get_result(conn, lobby.id) is None
    assert lobby_flow.points_defaults(conn, lobby, "B") == wrong
    assert first not in lobby_flow.points_defaults(conn, lobby, None)
    assert "result is in" in lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, points)
    post = lobby_posts.describe_results(conn, lobby, lambda user_id: f"<@{user_id}>")
    assert "wins!" in post.text and post.buttons == []


def test_an_organizer_finalizes_points_that_dont_match(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 9)
    lobby_flow.finish(conn, tournament, lobby, role_a, captain_a)
    lobby_flow.finish(conn, tournament, lobby, role_b, captain_b)
    lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, _points(lobby))
    lobby_flow.enter_points(conn, tournament, lobby, role_b, captain_b, _points(lobby, 1, 7))
    assert "Only organizers" in lobby_flow.finalize(conn, tournament, lobby, role_a, captain_a, _points(lobby))
    organizer = lobby_flow.Role(None, None, False, True)
    assert "final points" in lobby_flow.finalize(conn, tournament, lobby, organizer, ORGANIZER, _points(lobby, 1, 7))
    assert lobby_db.get_result(conn, lobby.id).winning_side == "B"


def test_a_captain_who_is_an_organizer_can_finish_for_both(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 10)
    both = lobby_flow.Role(role_a.team_id, role_a.side, True, True)
    assert "press **Finish** again" in lobby_flow.finish(conn, tournament, lobby, both, captain_a)
    assert "Finished lobby" in lobby_flow.finish(conn, tournament, lobby, both, captain_a)
    assert lobby_db.races_over(lobby, lobby_db.get_finished_sides(conn, lobby.id))


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
    late = sign_up(conn, tournament_id, 50, place=True)
    assert lobby_db.find_lobby_of(conn, tournament_id, late) is not None
    lobby = lobby_db.get_lobby(conn, lobby.id)
    assert lobby.teams[small].players == before[small] + [late]
    other = lobby_db.other_side(small)
    assert lobby.teams[other].players == before[other]
    # Now 4v4, then 5v4, then 5v5: the next two play, and the one after waits.
    for user_id in (51, 52):
        assert lobby_db.find_lobby_of(conn, tournament_id, sign_up(conn, tournament_id, user_id, place=True)) is not None
    waiting = sign_up(conn, tournament_id, 53, place=True)
    assert lobby_db.find_lobby_of(conn, tournament_id, waiting) is None
    assert lobby_db.get_waitlist(conn, tournament_id) == [waiting]


def test_upgrading_drops_the_old_reports_table(conn):
    conn.execute("CREATE TABLE lobby_reports (lobby_id INTEGER PRIMARY KEY)")
    db.init_db(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'lobby_reports'").fetchone() is None
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'lobby_entries'").fetchone() is not None


def test_a_concluded_event_takes_no_more_points(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 13)
    organizer = lobby_flow.Role(None, None, False, True)
    lobby_flow.finish(conn, tournament, lobby, organizer, ORGANIZER)
    db.set_tournament_status(conn, tournament["id"], "concluded")
    tournament = db.get_tournament(conn, tournament["id"])
    assert "is concluded" in lobby_flow.enter_points(conn, tournament, lobby, role_a, captain_a, _points(lobby))
    assert "is concluded" in lobby_flow.finalize(conn, tournament, lobby, organizer, ORGANIZER, _points(lobby))
    assert lobby_db.get_result(conn, lobby.id) is None


def test_teams_are_fixed_once_the_races_are_over(conn):
    tournament_id = event_with(conn, 11, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(14))
    tournament = db.get_tournament(conn, tournament_id)
    lobby_flow.start_checkin(conn, tournament, 15, datetime.now(timezone.utc) - timedelta(minutes=20))
    for signup in lobby_db.get_signups(conn, tournament_id):
        lobby_db.check_in(conn, signup.team_id)
    lobby_db.finish_checkin(conn, tournament_id)
    tournament = db.get_tournament(conn, tournament_id)
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    lobby_flow.submit_room_code(conn, tournament, lobby, lobby_flow.Role(None, None, False, True), ORGANIZER, "R")
    lobby = lobby_db.get_lobby(conn, lobby.id)
    lobby_flow.finish(conn, tournament, lobby, lobby_flow.Role(None, None, False, True), ORGANIZER)
    [waiting] = lobby_db.get_waitlist(conn, tournament_id)
    player = next(p for p in lobby.players if p not in lobby.captains)
    assert "races are over" in lobby_flow.swap_in(conn, tournament, player, waiting)
    assert "races are over" in lobby_flow.dropout(conn, tournament, player, "dropped", None)
    assert "races are over" in lobby_flow.move_player(conn, tournament, player)
    assert lobby_db.get_lobby(conn, lobby.id).players == lobby.players


def test_swapping_out_a_captain_hands_the_captaincy_on(conn):
    tournament_id = event_with(conn, 11, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(15))
    tournament = db.get_tournament(conn, tournament_id)
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    captain = lobby.teams["A"].captain
    [waiting] = lobby_db.get_waitlist(conn, tournament_id)
    teammate = next(p for p in lobby.teams["A"].players if p != captain)
    lobby_db.change_captain_pref(conn, tournament_id, teammate, "can")
    assert "takes" in lobby_flow.swap_in(conn, tournament, captain, waiting)
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == teammate


def test_changing_your_captain_choice_after_the_draw_takes_effect(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(16))
    tournament = db.get_tournament(conn, tournament_id)
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    captain = lobby.teams["A"].captain
    user_of = {team_id: users[0] for team_id, users in db.get_members_by_team(conn, tournament_id).items()}
    # The team's only captain candidate can't step down.
    assert "can't step down" in lobby_flow.sign_up(conn, tournament, "x", user_of[captain], True, "no")
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == captain
    # With a teammate who can captain, they hand it over.
    teammate = next(p for p in lobby.teams["A"].players if p != captain)
    lobby_flow.sign_up(conn, tournament, "x", user_of[teammate], True, "can")
    reply = lobby_flow.sign_up(conn, tournament, "x", user_of[captain], True, "no")
    assert "captains Team" in reply
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == teammate


def test_a_player_on_a_team_with_no_captain_takes_it_by_choosing_to_captain(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(17))
    tournament = db.get_tournament(conn, tournament_id)
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    lobby_db.set_captain(conn, lobby.id, "A", None)
    player = next(p for p in lobby.teams["A"].players if p != lobby.teams["A"].captain)
    user_of = {team_id: users[0] for team_id, users in db.get_members_by_team(conn, tournament_id).items()}
    assert "You captain Team" in lobby_flow.sign_up(conn, tournament, "x", user_of[player], True, "want")
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == player


def test_a_no_show_captain_is_replaced_as_captain_by_a_teammate_who_checked_in(conn):
    tournament_id = event_with(conn, 11, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(18))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    captain = lobby.teams["A"].captain
    absent, present = [p for p in lobby.teams["A"].players if p != captain][:2]
    lobby_db.change_captain_pref(conn, tournament_id, absent, "want")
    lobby_db.change_captain_pref(conn, tournament_id, present, "can")
    tournament = db.get_tournament(conn, tournament_id)
    lobby_flow.start_checkin(conn, tournament, 15, datetime.now(timezone.utc) - timedelta(minutes=20))
    for signup in lobby_db.get_signups(conn, tournament_id):
        if signup.team_id not in (captain, absent):
            lobby_db.check_in(conn, signup.team_id)
    assert lobby_db.finish_checkin(conn, tournament_id) is not None
    assert lobby_db.get_lobby(conn, lobby.id).teams["A"].captain == present


def test_a_sign_up_that_fails_partway_saves_nothing(conn):
    tournament_id = event_with(conn, 10, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(19))

    def fail(team_id):
        raise RuntimeError("placing failed")

    with pytest.raises(RuntimeError):
        db.register_entrant(conn, tournament_id, "Late", 77, also=fail)
    assert db.get_entrant_team_id(conn, tournament_id, 77) is None


def test_an_organizers_result_before_finish_still_gets_a_results_post(conn):
    tournament, lobby, (captain_a, role_a), (captain_b, role_b) = _playing(conn, 20)
    lobby_db.add_event_post(conn, tournament["id"], 1)
    organizer = lobby_flow.Role(None, None, False, True)
    assert "final points" in lobby_flow.finalize(conn, tournament, lobby, organizer, ORGANIZER, _points(lobby))
    assert lobby_db.get_post(conn, tournament["id"], lobby_db.results_post_key(lobby.id)) is not None


def test_the_event_post_lists_who_dropped_out_and_how(conn):
    tournament_id = event_with(conn, 12, captains=2)
    close_lobby_registration(conn, tournament_id, rng=random.Random(21))
    lobby = lobby_db.get_lobbies(conn, tournament_id)[0]
    dropped, disqualified = [p for p in lobby.players if p not in lobby.captains][:2]
    lobby_db.leave(conn, tournament_id, dropped, "dropped", None)
    lobby_db.leave(conn, tournament_id, disqualified, "disqualified", ORGANIZER)
    names = {t: row["name"] for t, row in db.get_teams(conn, tournament_id).items()}
    post = lobby_posts.describe_event(conn, db.get_tournament(conn, tournament_id), str)
    assert f"**Dropped out:** " in post.text
    dropout_line = next(line for line in post.text.splitlines() if line.startswith("**Dropped out:**"))
    users = {t: u[0] for t, u in db.get_members_by_team(conn, tournament_id).items()}
    assert f"{users[dropped]}, {users[disqualified]} (disqualified)" in dropout_line
