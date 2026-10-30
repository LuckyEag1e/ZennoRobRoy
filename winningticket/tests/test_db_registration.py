import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import BracketError, is_bye
from tournament_builder import RegistrationClosedError, close_overdue_registration, close_registration

# The tournaments table as schema version 1 databases have it.
V1_TOURNAMENTS_TABLE = """
CREATE TABLE tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('setup', 'predictions_open', 'in_progress', 'completed', 'concluded'))
                    DEFAULT 'setup',
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
PRAGMA user_version = 1;
"""


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def _open_with(conn, players: int, closes_at=None) -> int:
    tournament_id = db.open_registration(conn, 1, "Cup", 99, 555, closes_at)
    for user_id in range(1, players + 1):
        db.register_entrant(conn, tournament_id, f"Player {user_id}", user_id)
    return tournament_id


def test_registering_makes_a_one_person_team(conn):
    tournament_id = _open_with(conn, 1)
    [entrant] = db.get_entrants(conn, tournament_id)
    assert (entrant["name"], entrant["user_id"]) == ("Player 1", 1)
    assert db.get_entrant_team_id(conn, tournament_id, 1) == entrant["team_id"]
    assert db.get_tournament(conn, tournament_id)["status"] == "registration"


def test_a_taken_name_gets_a_number(conn):
    tournament_id = _open_with(conn, 0)
    names = [db.register_entrant(conn, tournament_id, "Sam", user_id)[1] for user_id in (1, 2, 3)]
    assert names == ["Sam", "Sam (2)", "Sam (3)"]
    assert [e["name"] for e in db.get_entrants(conn, tournament_id)] == names


def test_a_name_only_entrant_cant_take_a_name_already_in_use(conn):
    tournament_id = _open_with(conn, 1)
    assert db.register_entrant(conn, tournament_id, "Guest", None) is not None
    assert db.register_entrant(conn, tournament_id, "Guest", None) is None
    assert db.register_entrant(conn, tournament_id, "Player 1", None) is None
    assert len(db.get_teams(conn, tournament_id)) == 2


def test_a_member_cant_sign_up_under_a_name_only_entrants_name(conn):
    tournament_id = _open_with(conn, 0)
    db.register_entrant(conn, tournament_id, "Sam", None)
    assert db.register_entrant(conn, tournament_id, "Sam", 7) is None
    assert [(e["name"], e["user_id"]) for e in db.get_entrants(conn, tournament_id)] == [("Sam", None)]


def test_unlinking_leaves_the_entry_under_its_name(conn):
    tournament_id = _open_with(conn, 1)
    db.record_agreement(conn, 1, 1)
    team_id = db.get_entrant_team_id(conn, tournament_id, 1)
    assert db.unlink_entrant(conn, team_id) == 1
    assert [(e["name"], e["user_id"]) for e in db.get_entrants(conn, tournament_id)] == [("Player 1", None)]
    assert db.has_agreed(conn, 1, 1)
    assert db.unlink_entrant(conn, team_id) is None  # nobody left to unlink
    assert db.register_entrant(conn, tournament_id, "Player 1", 1) is None  # the name stays with the entry


def test_unregistering_removes_the_team(conn):
    tournament_id = _open_with(conn, 2)
    assert db.unregister_entrant(conn, tournament_id, 1)
    assert not db.unregister_entrant(conn, tournament_id, 1)
    assert [e["user_id"] for e in db.get_entrants(conn, tournament_id)] == [2]
    assert len(db.get_teams(conn, tournament_id)) == 1


def test_entrants_without_an_account_are_listed_and_drawn(conn):
    tournament_id = _open_with(conn, 1)
    guest_id, _ = db.register_entrant(conn, tournament_id, "Guest", None)
    assert [(e["name"], e["user_id"]) for e in db.get_entrants(conn, tournament_id)] == [
        ("Player 1", 1), ("Guest", None),
    ]
    close_registration(conn, tournament_id)
    [final] = db.get_matches(conn, tournament_id).values()
    assert guest_id in (final.team_a_id, final.team_b_id)


def test_deleting_a_team_withdraws_it(conn):
    tournament_id = _open_with(conn, 1)
    guest_id, _ = db.register_entrant(conn, tournament_id, "Guest", None)
    db.delete_team(conn, guest_id)
    db.delete_team(conn, db.get_entrant_team_id(conn, tournament_id, 1))
    assert db.get_entrants(conn, tournament_id) == []
    assert conn.execute("SELECT COUNT(*) FROM team_members").fetchone()[0] == 0


def test_closing_draws_everyone_into_a_bracket(conn):
    tournament_id = _open_with(conn, 5)
    assert close_registration(conn, tournament_id, random.Random(1)) == 5
    assert db.get_tournament(conn, tournament_id)["status"] == "setup"
    matches_by_id, _, teams_by_id = db.load_bracket(conn, tournament_id)
    round_1 = [m for m in matches_by_id.values() if m.round == 1]
    assert len(round_1) == 4
    assert sum(is_bye(m) for m in round_1) == 3
    placed = {team for m in round_1 for team in (m.team_a_id, m.team_b_id) if team is not None}
    assert placed == set(teams_by_id)


def test_closing_with_too_few_entrants_changes_nothing(conn):
    tournament_id = _open_with(conn, 1)
    with pytest.raises(BracketError):
        close_registration(conn, tournament_id)
    assert db.get_tournament(conn, tournament_id)["status"] == "registration"
    assert db.get_matches(conn, tournament_id) == {}


def test_a_close_that_fails_partway_saves_nothing_and_can_be_retried(conn, monkeypatch):
    tournament_id = _open_with(conn, 3)

    def fail(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "link_feeders", fail)
    with pytest.raises(sqlite3.OperationalError):
        close_registration(conn, tournament_id)
    assert db.get_matches(conn, tournament_id) == {}
    assert db.get_tournament(conn, tournament_id)["status"] == "registration"

    monkeypatch.undo()
    assert close_registration(conn, tournament_id) == 3


def test_a_tournament_cant_be_closed_twice(conn):
    tournament_id = _open_with(conn, 2)
    close_registration(conn, tournament_id)
    with pytest.raises(RegistrationClosedError):
        close_registration(conn, tournament_id)
    assert len(db.get_matches(conn, tournament_id)) == 1


def test_an_overdue_close_with_too_few_drops_the_deadline(conn):
    past = db.to_db_time(datetime.now(timezone.utc) - timedelta(minutes=1))
    tournament_id = _open_with(conn, 1, closes_at=past)
    with pytest.raises(BracketError):
        close_overdue_registration(conn, tournament_id)
    tournament = db.get_tournament(conn, tournament_id)
    assert (tournament["status"], tournament["registration_closes_at"]) == ("registration", None)


def test_only_passed_deadlines_are_due(conn):
    now = datetime.now(timezone.utc)
    past = db.open_registration(conn, 1, "Past", 99, 555, db.to_db_time(now - timedelta(minutes=1)))
    db.open_registration(conn, 1, "Future", 99, 555, db.to_db_time(now + timedelta(hours=1)))
    db.open_registration(conn, 1, "By hand", 99, 555, None)
    assert [t["id"] for t in db.get_tournaments_due_to_close(conn)] == [past]

    db.clear_registration_deadline(conn, past)
    assert db.get_tournaments_due_to_close(conn) == []


def test_db_times_round_trip():
    moment = datetime(2026, 3, 4, 5, 6, 7, 890000, tzinfo=timezone.utc)
    assert db.to_db_time(moment) == "2026-03-04T05:06:07.890Z"
    assert db.from_db_time(db.to_db_time(moment)) == moment


def test_tournament_names_can_be_filtered_by_status(conn):
    _open_with(conn, 0)
    db.create_tournament(conn, 1, "Imported", 99)
    assert db.get_tournament_names(conn, 1, "registration") == ["Cup"]
    assert sorted(db.get_tournament_names(conn, 1)) == ["Cup", "Imported"]


def test_registration_cant_be_forced():
    assert "registration" not in db.FORCEABLE_STATUSES


def test_version_1_database_is_migrated(tmp_path):
    conn = db.get_connection(tmp_path / "v1.db")
    conn.executescript(V1_TOURNAMENTS_TABLE)
    old_id = db.create_tournament(conn, 1, "Old", 99)

    db.init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_tournament(conn, old_id)["name"] == "Old"
    new_id = _open_with(conn, 2)
    assert db.get_tournament(conn, new_id)["channel_id"] == 555
    conn.close()


def test_version_2_database_keeps_its_registration_details(tmp_path):
    conn = db.get_connection(tmp_path / "v2.db")
    conn.executescript(V1_TOURNAMENTS_TABLE.replace(
        "'setup', 'predictions_open'", "'registration', 'setup', 'predictions_open'"
    ).replace(
        "created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        "created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),\n"
        "    channel_id  INTEGER,\n    registration_closes_at TEXT",
    ).replace("PRAGMA user_version = 1;", "PRAGMA user_version = 2;"))
    conn.execute(
        """INSERT INTO tournaments (guild_id, name, created_by, status, channel_id, registration_closes_at)
           VALUES (1, 'Open', 99, 'registration', 555, '2026-01-01T00:00:00.000Z')"""
    )
    conn.commit()

    db.init_db(conn)
    tournament = db.get_tournament_by_name(conn, 1, "Open")
    assert (tournament["channel_id"], tournament["registration_closes_at"]) == (555, "2026-01-01T00:00:00.000Z")
    assert tournament["announcement_message_id"] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_the_unused_took_over_column_is_dropped(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    conn.execute("ALTER TABLE team_members ADD COLUMN took_over INTEGER NOT NULL DEFAULT 0")
    db.init_db(conn)
    assert "took_over" not in {row["name"] for row in conn.execute("PRAGMA table_info(team_members)")}
    conn.close()


def test_a_team_cant_be_renamed_to_another_teams_name(conn):
    tournament_id = _open_with(conn, 2)
    first, second = (e["team_id"] for e in db.get_entrants(conn, tournament_id))
    assert db.team_name_taken(conn, tournament_id, "Player 2", first)
    assert not db.team_name_taken(conn, tournament_id, "Player 2", second)  # keeping its own name
    assert not db.team_name_taken(conn, tournament_id, "Someone new", first)


def test_a_saved_close_is_still_announced_if_the_bracket_cant_be_drawn(conn, monkeypatch):
    os.environ.setdefault("DISCORD_BOT_TOKEN", "test")
    import cogs.registration as registration

    def broken_embed(*args):
        raise RuntimeError("rendering failed")

    monkeypatch.setattr(registration, "load_bracket_embed", broken_embed)
    tournament_id = _open_with(conn, 2)
    closed, content, embed = registration._close_and_describe(conn, db.get_tournament(conn, tournament_id))
    assert closed and embed is None and "is closed" in content
    assert db.get_tournament(conn, tournament_id)["status"] == "setup"
