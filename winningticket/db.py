"""SQLite persistence layer. No discord.py imports here."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bracket import Match, MatchSpec, build_feeder_index

DB_PATH = Path(__file__).parent / "data" / "tourney.db"

# Tournament statuses, in lifecycle order.
STATUSES = ("setup", "predictions_open", "in_progress", "completed")
_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in STATUSES)

SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ({_STATUS_SQL_LIST}))
                    DEFAULT 'setup',
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (guild_id, name)
);

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
    conn.commit()


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
        "SELECT * FROM tournaments WHERE guild_id = ? AND name = ?",
        (guild_id, name),
    ).fetchone()


def get_tournament_names(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM tournaments WHERE guild_id = ? ORDER BY created_at DESC",
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
    return {
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


def upsert_result(conn: sqlite3.Connection, match_id: int, winner_team_id: int, recorded_by: int) -> None:
    conn.execute(
        """INSERT INTO match_results (match_id, winner_team_id, recorded_by)
           VALUES (?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET
               winner_team_id = excluded.winner_team_id,
               recorded_by = excluded.recorded_by,
               recorded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (match_id, winner_team_id, recorded_by),
    )
    conn.commit()


def delete_result(conn: sqlite3.Connection, match_id: int) -> None:
    conn.execute("DELETE FROM match_results WHERE match_id = ?", (match_id,))
    conn.commit()


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
