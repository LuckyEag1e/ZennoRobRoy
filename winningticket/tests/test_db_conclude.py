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


def test_concluded_name_cant_be_reused(conn):
    _concluded_tournament(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_tournament(conn, 1, "Cup", 1)
    db.create_tournament(conn, 2, "Cup", 1)  # another server's names are its own


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


def test_a_concluded_tournament_can_still_be_found_by_name(conn):
    tournament_id = _concluded_tournament(conn)
    assert db.get_concluded_tournament_by_name(conn, 1, "Cup")["id"] == tournament_id
    assert db.get_concluded_tournament_by_name(conn, 1, "Other") is None
    assert db.get_concluded_tournament_by_name(conn, 2, "Cup") is None
    _completed_tournament(conn, "Live")
    assert db.get_concluded_tournament_by_name(conn, 1, "Live") is None


def test_version_6_database_gets_single_elimination_for_existing_tournaments(tmp_path):
    conn = db.get_connection(tmp_path / "v6.db")
    v6_table = "\n".join(line for line in db._tournaments_table_sql("tournaments").splitlines() if "format" not in line)
    v6_table = v6_table.replace("DEFAULT 'random',", "DEFAULT 'random'")
    conn.executescript(v6_table + "PRAGMA user_version = 6;")
    conn.execute("INSERT INTO tournaments (guild_id, name, created_by) VALUES (1, 'Cup', 1)")
    conn.commit()

    db.init_db(conn)
    assert db.get_tournament_by_name(conn, 1, "Cup")["format"] == "single_elim"
    conn.close()


def test_migration_renames_all_but_the_newest_of_a_reused_name(tmp_path):
    conn = db.get_connection(tmp_path / "v5.db")
    v5_table = db._tournaments_table_sql("tournaments")
    conn.executescript(v5_table + """
        CREATE UNIQUE INDEX idx_tournaments_active_name ON tournaments(guild_id, name) WHERE status != 'concluded';
        PRAGMA user_version = 5;""")
    for guild, name, status, created_at in [
        (1, "Cup", "concluded", "2026-03-01T10:00:00.000Z"),
        (1, "Cup", "concluded", "2026-03-01T18:00:00.000Z"),
        (1, "Cup (2026-03-01)", "concluded", "2026-04-01T10:00:00.000Z"),
        (1, "Cup", "setup", "2026-05-01T10:00:00.000Z"),
        (2, "Cup", "concluded", "2026-03-01T10:00:00.000Z"),
    ]:
        conn.execute("INSERT INTO tournaments (guild_id, name, created_by, status, created_at) VALUES (?, ?, 1, ?, ?)",
                     (guild, name, status, created_at))
    conn.commit()

    db.init_db(conn)
    names = [row["name"] for row in conn.execute("SELECT name FROM tournaments ORDER BY id")]
    assert names == ["Cup (2026-03-01, 2)", "Cup (2026-03-01, 3)", "Cup (2026-03-01)", "Cup", "Cup"]
    with pytest.raises(sqlite3.IntegrityError):
        db.create_tournament(conn, 1, "Cup (2026-03-01)", 1)
    conn.close()
