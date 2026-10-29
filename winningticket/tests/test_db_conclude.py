import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from csv_import import import_tournament, parse_csv

BRACKET_CSV = "team_a,team_b\nA,B\nC,D\n"

# The tournaments table as databases created before the concluded status have it.
OLD_TOURNAMENTS_TABLE = """
CREATE TABLE tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('setup', 'predictions_open', 'in_progress', 'completed'))
                    DEFAULT 'setup',
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, name)
);
"""


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def _completed_tournament(conn, name="Cup"):
    """A 4-team tournament played through to A winning."""
    tournament_id = import_tournament(conn, 1, name, 1, parse_csv(BRACKET_CSV))
    db.set_tournament_status(conn, tournament_id, "in_progress")
    matches_by_id, _, teams_by_id = db.load_bracket(conn, tournament_id)
    semi_1, semi_2, final = sorted(matches_by_id.values(), key=bracket_order)
    team = {row["name"]: team_id for team_id, row in teams_by_id.items()}
    db.record_result(conn, tournament_id, semi_1.id, team["A"], 1)
    db.record_result(conn, tournament_id, semi_2.id, team["C"], 1)
    db.record_result(conn, tournament_id, final.id, team["A"], 1)
    return tournament_id


def _concluded_tournament(conn, name="Cup"):
    tournament_id = _completed_tournament(conn, name)
    db.set_tournament_status(conn, tournament_id, "concluded")
    return tournament_id


def test_concluded_tournament_cant_be_selected_by_name(conn):
    _concluded_tournament(conn)
    assert db.get_tournament_by_name(conn, 1, "Cup") is None
    assert db.get_tournament_names(conn, 1) == []


def test_concluded_tournament_keeps_its_results(conn):
    tournament_id = _concluded_tournament(conn)
    assert len(db.get_results(conn, tournament_id)) == 3
    assert len(db.get_teams(conn, tournament_id)) == 4


def test_concluded_name_can_be_reused(conn):
    old_id = _concluded_tournament(conn)
    new_id = import_tournament(conn, 1, "Cup", 1, parse_csv(BRACKET_CSV))
    assert db.get_tournament_by_name(conn, 1, "Cup")["id"] == new_id
    assert len(db.get_results(conn, old_id)) == 3


def test_active_names_must_still_be_unique(conn):
    import_tournament(conn, 1, "Cup", 1, parse_csv(BRACKET_CSV))
    with pytest.raises(sqlite3.IntegrityError):
        db.create_tournament(conn, 1, "Cup", 1)


def test_admins_cant_force_concluded():
    assert "concluded" not in db.FORCEABLE_STATUSES


def test_new_database_starts_at_the_current_schema_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migration_keeps_the_id_counter(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    conn.executescript(OLD_TOURNAMENTS_TABLE + db.SCHEMA)
    db.create_tournament(conn, 1, "First", 1)
    deleted_id = db.create_tournament(conn, 1, "Second", 1)
    db.delete_tournament(conn, deleted_id)

    db.init_db(conn)
    assert db.create_tournament(conn, 1, "Third", 1) > deleted_id
    conn.close()


def test_old_database_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "old.db"
    conn = db.get_connection(path)
    # The current SCHEMA leaves an existing tournaments table alone, so this builds an old database.
    conn.executescript(OLD_TOURNAMENTS_TABLE + db.SCHEMA)
    tournament_id = _completed_tournament(conn)

    db.init_db(conn)
    db.set_tournament_status(conn, tournament_id, "concluded")
    assert len(db.get_results(conn, tournament_id)) == 3
    conn.close()

    # Reopening must not migrate again, and the rebuilt table must keep its links to child rows.
    conn = db.get_connection(path)
    db.init_db(conn)
    assert db.get_tournament(conn, tournament_id)["status"] == "concluded"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.delete_tournament(conn, tournament_id)
    assert db.get_teams(conn, tournament_id) == {}
    conn.close()


def test_final_results_name_the_podium_and_score_the_players(conn):
    tournament_id = _completed_tournament(conn)
    db.upsert_predictions(conn, tournament_id, 7, db.get_results(conn, tournament_id))
    result, teams_by_id, scores = db.load_final_results(conn, tournament_id)
    name = {team_id: row["name"] for team_id, row in teams_by_id.items()}
    assert (name[result.champion], name[result.runner_up]) == ("A", "C")
    assert sorted(name[t] for t in result.third_places) == ["B", "D"]
    assert [(s.user_id, s.points) for s in scores] == [(7, 4)]
    assert db.load_scores(conn, tournament_id) == scores


def test_a_concluded_tournament_can_still_be_recognised_by_name(conn):
    _concluded_tournament(conn)
    assert db.has_concluded_tournament(conn, 1, "Cup")
    assert not db.has_concluded_tournament(conn, 1, "Other")
    assert not db.has_concluded_tournament(conn, 2, "Cup")
