"""Rating history from concluded tournaments, and seeding by it when sign-ups close."""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import resolvable_matches, resolve_slot
from ratings import PlayedMatch, compute_ratings
from tournament_builder import close_registration

GUILD = 1


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def _signups(conn, entrants: list[tuple[str, int | None]], seeding_mode: str = "auto", guild: int = GUILD) -> int:
    tournament_id = db.open_registration(conn, guild, "Cup", 99, 555, None, seeding_mode)
    for name, user_id in entrants:
        db.register_entrant(conn, tournament_id, name, user_id)
    return tournament_id


def _play_out(conn, tournament_id: int, winner_of) -> None:
    """Record every game, with `winner_of(team_a_name, team_b_name)` naming each winner."""
    teams = db.get_teams(conn, tournament_id)
    while True:
        matches_by_id, feeders, _ = db.load_bracket(conn, tournament_id)
        results = db.get_results(conn, tournament_id)
        ready = resolvable_matches(matches_by_id, feeders, results.get, set(results))
        if not ready:
            return
        match = ready[0]
        team_a, team_b = resolve_slot(match, matches_by_id, results.get, feeders)
        winner_name = winner_of(teams[team_a]["name"], teams[team_b]["name"])
        winner = team_a if teams[team_a]["name"] == winner_name else team_b
        db.record_result(conn, tournament_id, match.id, winner, 99)


def _concluded(conn, entrants, winner_of=min, guild: int = GUILD) -> int:
    """A finished, concluded tournament. By default the alphabetically first name wins every game."""
    tournament_id = _signups(conn, entrants, "random", guild)
    close_registration(conn, tournament_id, random.Random(0))
    _play_out(conn, tournament_id, winner_of)
    db.set_tournament_status(conn, tournament_id, "concluded")
    return tournament_id


def test_history_has_every_decided_game_of_concluded_tournaments_only(conn):
    _concluded(conn, [("Ace", 1), ("Blaze", 2)])
    unfinished = _signups(conn, [("Ace", 1), ("Blaze", 2)])
    close_registration(conn, unfinished, random.Random(0))
    _play_out(conn, unfinished, max)  # completed but not concluded, so it doesn't count yet
    _concluded(conn, [("Comet", 3), ("Dash", 4)], guild=2)  # another server's
    assert db.load_match_history(conn, GUILD) == [PlayedMatch((1,), (2,))]


def test_history_runs_oldest_first_and_leaves_no_account_entrants_empty(conn):
    tournament_id = _concluded(conn, [("Ace", 1), ("Blaze", 2), ("Comet", None), ("Dash", 4)])
    history = db.load_match_history(conn, GUILD)
    assert len(history) == 3
    # Ace wins every game, and the final is recorded last; Comet has no account.
    assert history[-1].winners == (1,) and len(history[-1].losers) <= 1
    assert sum(game.winners == () or game.losers == () for game in history) >= 1
    assert db.get_tournament(conn, tournament_id)["status"] == "concluded"


def test_closing_by_rating_writes_seeds_in_rating_order(conn):
    _concluded(conn, [("Ace", 1), ("Blaze", 2), ("Comet", 3), ("Dash", 4)])  # Ace wins every game
    tournament_id = _signups(conn, [("Dash", 4), ("Newcomer", 5), ("Ace", 1), ("Comet", 3), ("Blaze", 2)])
    close_registration(conn, tournament_id, random.Random(0))

    ratings = compute_ratings(db.load_match_history(conn, GUILD))
    rating_by_name = {"Ace": ratings.singles[1], "Blaze": ratings.singles[2], "Comet": ratings.singles[3],
                      "Dash": ratings.singles[4], "Newcomer": 1500}  # an unrated player counts as average
    seeds = {team["name"]: team["seed"] for team in db.get_teams(conn, tournament_id).values()}
    in_seed_order = [rating_by_name[name] for name in sorted(seeds, key=seeds.get)]
    assert in_seed_order == sorted(in_seed_order, reverse=True)  # the two first-round losers tie, in either order
    assert seeds["Ace"] == 1
    matches_by_id, _, _ = db.load_bracket(conn, tournament_id)
    byes = {m.team_a_id or m.team_b_id for m in matches_by_id.values() if m.round == 1 and None in (m.team_a_id, m.team_b_id)}
    assert {db.get_teams(conn, tournament_id)[team]["seed"] for team in byes} == {1, 2, 3}


def test_closing_can_override_the_seeding_chosen_at_opening(conn):
    _concluded(conn, [("Ace", 1), ("Blaze", 2)])
    tournament_id = _signups(conn, [("Ace", 1), ("Blaze", 2), ("Comet", 3)], seeding_mode="auto")
    close_registration(conn, tournament_id, random.Random(0), seeding_mode="random")
    assert all(team["seed"] is None for team in db.get_teams(conn, tournament_id).values())


def test_random_seeding_is_stored_and_used_by_default(conn):
    tournament_id = _signups(conn, [("Ace", 1), ("Blaze", 2)], seeding_mode="random")
    assert db.get_tournament(conn, tournament_id)["seeding_mode"] == "random"
    close_registration(conn, tournament_id, random.Random(0))
    assert all(team["seed"] is None for team in db.get_teams(conn, tournament_id).values())


def test_linking_an_old_team_adds_its_games_to_the_members_rating(conn):
    tournament_id = _concluded(conn, [("Ace", None), ("Blaze", 2)])
    assert compute_ratings(db.load_match_history(conn, GUILD)).singles == {}
    ace = db.get_team_by_name(conn, tournament_id, "Ace")["id"]
    assert db.link_team_member(conn, tournament_id, ace, 1)
    assert compute_ratings(db.load_match_history(conn, GUILD)).singles == {1: 1516, 2: 1484}
    # Someone can only be on one team per tournament.
    blaze = db.get_team_by_name(conn, tournament_id, "Blaze")["id"]
    assert not db.link_team_member(conn, tournament_id, blaze, 1)


def test_removing_a_concluded_tournament_takes_it_out_of_the_ratings(conn):
    keep = _concluded(conn, [("Ace", 1), ("Blaze", 2)])
    mistake = _concluded(conn, [("Ace", 1), ("Blaze", 2)], winner_of=max)
    assert [t["id"] for t in db.get_concluded_tournaments(conn, GUILD)] == [mistake, keep]
    db.delete_tournament(conn, mistake)
    assert db.load_match_history(conn, GUILD) == [PlayedMatch((1,), (2,))]
    assert db.get_concluded_tournament(conn, GUILD, mistake) is None
    assert db.get_concluded_tournament(conn, 2, keep) is None  # not from another server


def test_version_3_database_gets_random_seeding_for_existing_tournaments(tmp_path):
    conn = db.get_connection(tmp_path / "v3.db")
    v3_table = db._tournaments_table_sql("tournaments").replace(
        "announcement_message_id INTEGER,", "announcement_message_id INTEGER"
    )
    v3_table = "\n".join(line for line in v3_table.splitlines() if "seeding_mode" not in line)
    conn.executescript(v3_table + "PRAGMA user_version = 3;")
    conn.execute("INSERT INTO tournaments (guild_id, name, created_by, status) VALUES (1, 'Open', 99, 'registration')")
    conn.commit()

    db.init_db(conn)
    assert db.get_tournament_by_name(conn, 1, "Open")["seeding_mode"] == "random"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
