"""SQLite persistence layer. No discord.py imports here."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bracket import Match, MatchSpec, assign_played_rounds, build_feeder_index, final_match, stale_results

DB_PATH = Path(__file__).parent / "data" / "tourney.db"

# Tournament statuses, in lifecycle order. A concluded tournament keeps all its data
# for later use (such as seeding) but can no longer be looked up by name, so the
# name is free for a new tournament.
STATUSES = ("setup", "predictions_open", "in_progress", "completed", "concluded")
_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in STATUSES)


def is_next_status(current: str, new: str) -> bool:
    """True if `new` directly follows `current` in the normal lifecycle (open, lock, then conclude)."""
    return STATUSES.index(new) == STATUSES.index(current) + 1


def _tournaments_table_sql(table_name: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ({_STATUS_SQL_LIST}))
                    DEFAULT 'setup',
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


# Names only have to be unique among tournaments that haven't been concluded.
_ACTIVE_NAME_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_active_name
    ON tournaments(guild_id, name) WHERE status != 'concluded';
"""

SCHEMA = f"""
PRAGMA foreign_keys = ON;
{_tournaments_table_sql("tournaments")}
{_ACTIVE_NAME_INDEX_SQL}

CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    seed            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_teams_tournament ON teams(tournament_id);

CREATE TABLE IF NOT EXISTS matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id       INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    round               INTEGER NOT NULL,
    slot_index          INTEGER NOT NULL,
    team_a_id           INTEGER REFERENCES teams(id),
    team_b_id           INTEGER REFERENCES teams(id),
    feeds_into_match_id INTEGER REFERENCES matches(id),
    feeds_into_side     TEXT CHECK (feeds_into_side IN ('A','B')),
    UNIQUE (tournament_id, round, slot_index)
);
CREATE INDEX IF NOT EXISTS idx_matches_tournament_round ON matches(tournament_id, round);
CREATE INDEX IF NOT EXISTS idx_matches_feeds_into ON matches(feeds_into_match_id);

CREATE TABLE IF NOT EXISTS match_results (
    match_id        INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    winner_team_id  INTEGER NOT NULL REFERENCES teams(id),
    recorded_by     INTEGER NOT NULL,
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id               INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id                     INTEGER NOT NULL,
    match_id                    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    predicted_winner_team_id    INTEGER NOT NULL REFERENCES teams(id),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (tournament_id, user_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_predictions_user ON predictions(tournament_id, user_id);
CREATE INDEX IF NOT EXISTS idx_predictions_match ON predictions(match_id);
"""


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_to_concluded_status(conn)
    conn.commit()


def _migrate_to_concluded_status(conn: sqlite3.Connection) -> None:
    """Databases made before the concluded status have a tournaments table whose CHECK
    rejects it and whose UNIQUE (guild_id, name) blocks reusing a concluded name. SQLite
    can't alter either in place, so the table is rebuilt, following SQLite's documented
    steps for changing a table that other tables reference."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    conn.executescript(f"""
PRAGMA foreign_keys = OFF;
BEGIN;
{_tournaments_table_sql("tournaments_new")}
INSERT INTO tournaments_new (id, guild_id, name, status, created_by, created_at)
    SELECT id, guild_id, name, status, created_by, created_at FROM tournaments;
DROP TABLE tournaments;
ALTER TABLE tournaments_new RENAME TO tournaments;
{_ACTIVE_NAME_INDEX_SQL}
PRAGMA user_version = 1;
COMMIT;
PRAGMA foreign_keys = ON;
""")


# -- tournaments -------------------------------------------------------------

def create_tournament(conn: sqlite3.Connection, guild_id: int, name: str, created_by: int) -> int:
    cur = conn.execute(
        "INSERT INTO tournaments (guild_id, name, created_by) VALUES (?, ?, ?)",
        (guild_id, name, created_by),
    )
    conn.commit()
    return cur.lastrowid


def get_tournament_by_name(conn: sqlite3.Connection, guild_id: int, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tournaments WHERE guild_id = ? AND name = ? AND status != 'concluded'",
        (guild_id, name),
    ).fetchone()


def get_tournament(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,)).fetchone()


def get_tournament_names(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM tournaments WHERE guild_id = ? AND status != 'concluded' ORDER BY created_at DESC",
        (guild_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def set_tournament_status(conn: sqlite3.Connection, tournament_id: int, status: str) -> None:
    conn.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, tournament_id))
    conn.commit()


def delete_tournament(conn: sqlite3.Connection, tournament_id: int) -> None:
    conn.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))
    conn.commit()


# -- teams --------------------------------------------------------------------

def insert_team(conn: sqlite3.Connection, tournament_id: int, name: str, seed: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO teams (tournament_id, name, seed) VALUES (?, ?, ?)",
        (tournament_id, name, seed),
    )
    conn.commit()
    return cur.lastrowid


def get_teams(conn: sqlite3.Connection, tournament_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM teams WHERE tournament_id = ?", (tournament_id,)).fetchall()
    return {r["id"]: r for r in rows}


def get_team_by_name(conn: sqlite3.Connection, tournament_id: int, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM teams WHERE tournament_id = ? AND name = ?", (tournament_id, name)
    ).fetchone()


def update_team(conn: sqlite3.Connection, team_id: int, name: str | None, seed: int | None, seed_given: bool) -> None:
    if name is not None and seed_given:
        conn.execute("UPDATE teams SET name = ?, seed = ? WHERE id = ?", (name, seed, team_id))
    elif name is not None:
        conn.execute("UPDATE teams SET name = ? WHERE id = ?", (name, team_id))
    elif seed_given:
        conn.execute("UPDATE teams SET seed = ? WHERE id = ?", (seed, team_id))
    conn.commit()


# -- matches ------------------------------------------------------------------

def insert_match_spec(conn: sqlite3.Connection, tournament_id: int, spec: MatchSpec,
                       team_a_id: int | None, team_b_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO matches (tournament_id, round, slot_index, team_a_id, team_b_id)
           VALUES (?, ?, ?, ?, ?)""",
        (tournament_id, spec.round, spec.slot_index, team_a_id, team_b_id),
    )
    return cur.lastrowid


def link_feeders(conn: sqlite3.Connection, tournament_id: int, specs: list[MatchSpec],
                  match_ids_by_round_slot: dict[tuple[int, int], int]) -> None:
    for spec in specs:
        if spec.feeds_into_round is None:
            continue
        this_id = match_ids_by_round_slot[(spec.round, spec.slot_index)]
        target_id = match_ids_by_round_slot[(spec.feeds_into_round, spec.feeds_into_slot_index)]
        conn.execute(
            "UPDATE matches SET feeds_into_match_id = ?, feeds_into_side = ? WHERE id = ?",
            (target_id, spec.feeds_into_side, this_id),
        )
    conn.commit()


def get_matches(conn: sqlite3.Connection, tournament_id: int) -> dict[int, Match]:
    rows = conn.execute("SELECT * FROM matches WHERE tournament_id = ?", (tournament_id,)).fetchall()
    matches_by_id = {
        r["id"]: Match(
            id=r["id"],
            round=r["round"],
            slot_index=r["slot_index"],
            team_a_id=r["team_a_id"],
            team_b_id=r["team_b_id"],
            feeds_into_match_id=r["feeds_into_match_id"],
            feeds_into_side=r["feeds_into_side"],
        )
        for r in rows
    }
    assign_played_rounds(matches_by_id)
    return matches_by_id


def load_bracket(conn: sqlite3.Connection, tournament_id: int) -> tuple[dict[int, Match], dict, dict[int, sqlite3.Row]]:
    """Everything needed to walk a tournament's tree: (matches_by_id, feeders_by_target, teams_by_id)."""
    matches_by_id = get_matches(conn, tournament_id)
    return matches_by_id, build_feeder_index(list(matches_by_id.values())), get_teams(conn, tournament_id)


# -- results ------------------------------------------------------------------

def get_results(conn: sqlite3.Connection, tournament_id: int) -> dict[int, int]:
    rows = conn.execute(
        """SELECT mr.match_id, mr.winner_team_id FROM match_results mr
           JOIN matches m ON m.id = mr.match_id WHERE m.tournament_id = ?""",
        (tournament_id,),
    ).fetchall()
    return {r["match_id"]: r["winner_team_id"] for r in rows}


def record_result(conn: sqlite3.Connection, tournament_id: int, match_id: int,
                  winner_team_id: int, recorded_by: int) -> list[int]:
    """Save (or overwrite) a match's real winner. Returns the ids of later results that
    were dropped because their winner is no longer in that match."""
    with conn:
        conn.execute(
            """INSERT INTO match_results (match_id, winner_team_id, recorded_by)
               VALUES (?, ?, ?)
               ON CONFLICT(match_id) DO UPDATE SET
                   winner_team_id = excluded.winner_team_id,
                   recorded_by = excluded.recorded_by,
                   recorded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (match_id, winner_team_id, recorded_by),
        )
        return _settle_results(conn, tournament_id)


def clear_result(conn: sqlite3.Connection, tournament_id: int, match_id: int) -> list[int]:
    """Delete a match's real result. Returns the ids of later results dropped with it."""
    with conn:
        conn.execute("DELETE FROM match_results WHERE match_id = ?", (match_id,))
        return _settle_results(conn, tournament_id)


def _settle_results(conn: sqlite3.Connection, tournament_id: int) -> list[int]:
    """After a result changes, drop results it invalidated and keep the status in step:
    a decided final means completed, and an undecided one can't stay completed."""
    matches_by_id, feeders, _ = load_bracket(conn, tournament_id)
    results_by_match = get_results(conn, tournament_id)
    stale = stale_results(matches_by_id, feeders, results_by_match)
    conn.executemany("DELETE FROM match_results WHERE match_id = ?", [(match_id,) for match_id in stale])

    final_id = final_match(matches_by_id).id
    final_decided = final_id in results_by_match and final_id not in stale
    status = get_tournament(conn, tournament_id)["status"]
    if final_decided:
        status = "completed"
    elif status == "completed":
        status = "in_progress"
    conn.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, tournament_id))
    return stale


# -- predictions ----------------------------------------------------------------

def upsert_predictions(conn: sqlite3.Connection, tournament_id: int, user_id: int,
                        picks: dict[int, int]) -> None:
    with conn:
        for match_id, team_id in picks.items():
            conn.execute(
                """INSERT INTO predictions (tournament_id, user_id, match_id, predicted_winner_team_id, updated_at)
                   VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                   ON CONFLICT(tournament_id, user_id, match_id) DO UPDATE SET
                       predicted_winner_team_id = excluded.predicted_winner_team_id,
                       updated_at = excluded.updated_at""",
                (tournament_id, user_id, match_id, team_id),
            )


def get_user_predictions(conn: sqlite3.Connection, tournament_id: int, user_id: int) -> dict[int, int]:
    rows = conn.execute(
        "SELECT match_id, predicted_winner_team_id FROM predictions WHERE tournament_id = ? AND user_id = ?",
        (tournament_id, user_id),
    ).fetchall()
    return {r["match_id"]: r["predicted_winner_team_id"] for r in rows}


def get_all_predictions(conn: sqlite3.Connection, tournament_id: int) -> dict[int, dict[int, int]]:
    rows = conn.execute(
        "SELECT user_id, match_id, predicted_winner_team_id FROM predictions WHERE tournament_id = ?",
        (tournament_id,),
    ).fetchall()
    out: dict[int, dict[int, int]] = {}
    for r in rows:
        out.setdefault(r["user_id"], {})[r["match_id"]] = r["predicted_winner_team_id"]
    return out


def clear_user_predictions(conn: sqlite3.Connection, tournament_id: int, user_id: int) -> None:
    conn.execute(
        "DELETE FROM predictions WHERE tournament_id = ? AND user_id = ?",
        (tournament_id, user_id),
    )
    conn.commit()
