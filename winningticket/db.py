"""SQLite persistence layer. No discord.py imports here."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bracket import (
    Match,
    MatchSpec,
    Podium,
    UserScore,
    assign_played_rounds,
    build_feeder_index,
    compute_leaderboard,
    final_match,
    podium,
    stale_results,
)

DB_PATH = Path(__file__).parent / "data" / "tourney.db"

# Tournament statuses, in lifecycle order. A tournament made from a CSV starts at setup;
# one that takes sign-ups starts at registration and has no matches until it closes.
# A concluded tournament keeps all its data for later use (such as seeding) but can no
# longer be looked up by name, so the name is free for a new tournament.
STATUSES = ("registration", "setup", "predictions_open", "in_progress", "completed", "concluded")
# Concluding can't be undone from Discord, so admins can't force it; /tournament
# conclude only allows it once a tournament is completed. Registration can't be forced
# either way: going back to it would leave a bracket behind, and leaving it any other
# way than closing registration would leave a tournament with no bracket.
FORCEABLE_STATUSES = tuple(status for status in STATUSES if status not in ("registration", "concluded"))
_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in STATUSES)


def to_db_time(moment: datetime) -> str:
    """A time in the format SQLite's strftime writes for created_at, so the two compare as text."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_db_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


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
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- Where registration was opened, so announcements made without a command land there.
    channel_id  INTEGER,
    registration_closes_at TEXT,
    -- The sign-ups announcement, edited once sign-ups close so its countdown stops.
    announcement_message_id INTEGER
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

-- The Discord users behind a team. Teams imported from a CSV have none.
CREATE TABLE IF NOT EXISTS team_members (
    team_id         INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    UNIQUE (tournament_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_team_members_team ON team_members(team_id);

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

-- Users an admin has blocked from using the bot in a server.
CREATE TABLE IF NOT EXISTS bans (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    reason      TEXT,
    banned_by   INTEGER NOT NULL,
    banned_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, user_id)
);

-- Anonymous feedback. There's deliberately no user column, and only the date is kept,
-- so a message can't be matched to whoever was active at that moment.
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    message     TEXT NOT NULL,
    created_on  TEXT NOT NULL DEFAULT (date('now'))
);
"""


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Bump when an older database needs migrating, and add the migration to init_db().
SCHEMA_VERSION = 3


def init_db(conn: sqlite3.Connection) -> None:
    is_new = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tournaments'").fetchone() is None
    conn.executescript(SCHEMA)
    if is_new:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    else:
        _rebuild_tournaments_table(conn)
    conn.commit()


def _rebuild_tournaments_table(conn: sqlite3.Connection) -> None:
    """Older databases have a tournaments table whose CHECK rejects newer statuses, and
    before version 1 a UNIQUE (guild_id, name) that blocks reusing a concluded name.
    SQLite can't alter either in place, so the table is rebuilt, following SQLite's
    documented steps for changing a table that other tables reference. Columns the old
    table already has are copied across; newer ones start empty."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # has no effect inside a transaction
    try:
        conn.execute("BEGIN")
        conn.execute(_tournaments_table_sql("tournaments_new"))
        old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tournaments)")}
        columns = ", ".join(
            row["name"] for row in conn.execute("PRAGMA table_info(tournaments_new)") if row["name"] in old_columns
        )
        conn.execute(f"INSERT INTO tournaments_new ({columns}) SELECT {columns} FROM tournaments")
        # Keep the id counter, so ids of deleted tournaments aren't handed out again.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tournaments_new'")
        conn.execute("""INSERT INTO sqlite_sequence (name, seq)
                        SELECT 'tournaments_new', seq FROM sqlite_sequence WHERE name = 'tournaments'""")
        conn.execute("DROP TABLE tournaments")
        conn.execute("ALTER TABLE tournaments_new RENAME TO tournaments")
        conn.execute(_ACTIVE_NAME_INDEX_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# -- tournaments -------------------------------------------------------------

def create_tournament(conn: sqlite3.Connection, guild_id: int, name: str, created_by: int) -> int:
    cur = conn.execute(
        "INSERT INTO tournaments (guild_id, name, created_by) VALUES (?, ?, ?)",
        (guild_id, name, created_by),
    )
    conn.commit()
    return cur.lastrowid


def open_registration(conn: sqlite3.Connection, guild_id: int, name: str, created_by: int,
                      channel_id: int | None, closes_at: str | None) -> int:
    """Create a tournament that takes sign-ups. `closes_at` is a UTC time in the same
    format as created_at, or None to close it by hand."""
    cur = conn.execute(
        """INSERT INTO tournaments (guild_id, name, created_by, status, channel_id, registration_closes_at)
           VALUES (?, ?, ?, 'registration', ?, ?)""",
        (guild_id, name, created_by, channel_id, closes_at),
    )
    conn.commit()
    return cur.lastrowid


def get_tournaments_due_to_close(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tournaments still in registration whose closing time has passed."""
    return conn.execute(
        """SELECT * FROM tournaments WHERE status = 'registration'
           AND registration_closes_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')"""
    ).fetchall()


def get_tournament_by_name(conn: sqlite3.Connection, guild_id: int, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tournaments WHERE guild_id = ? AND name = ? AND status != 'concluded'",
        (guild_id, name),
    ).fetchone()


def has_concluded_tournament(conn: sqlite3.Connection, guild_id: int, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tournaments WHERE guild_id = ? AND name = ? AND status = 'concluded'", (guild_id, name)
    ).fetchone() is not None


def get_tournament(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,)).fetchone()


def get_tournament_names(conn: sqlite3.Connection, guild_id: int, status: str | None = None) -> list[str]:
    """Names of tournaments that haven't been concluded, newest first, optionally only those at `status`."""
    rows = conn.execute(
        """SELECT name FROM tournaments WHERE guild_id = ? AND status != 'concluded'
           AND (? IS NULL OR status = ?) ORDER BY created_at DESC""",
        (guild_id, status, status),
    ).fetchall()
    return [r["name"] for r in rows]


def set_tournament_status(conn: sqlite3.Connection, tournament_id: int, status: str) -> None:
    conn.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, tournament_id))
    conn.commit()


def set_announcement_message(conn: sqlite3.Connection, tournament_id: int, message_id: int) -> None:
    conn.execute("UPDATE tournaments SET announcement_message_id = ? WHERE id = ?", (message_id, tournament_id))
    conn.commit()


def claim_registration_close(conn: sqlite3.Connection, tournament_id: int) -> bool:
    """Move a tournament from registration to setup without committing, so it lands
    together with the bracket. False if it had already left registration. The status
    check and the change are one statement, so two closes can't both claim it."""
    cur = conn.execute(
        "UPDATE tournaments SET status = 'setup' WHERE id = ? AND status = 'registration'", (tournament_id,)
    )
    return cur.rowcount > 0


def clear_registration_deadline(conn: sqlite3.Connection, tournament_id: int) -> None:
    conn.execute("UPDATE tournaments SET registration_closes_at = NULL WHERE id = ?", (tournament_id,))
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


def register_entrant(conn: sqlite3.Connection, tournament_id: int, name: str,
                     user_id: int | None) -> tuple[int, str]:
    """Sign up a one-person team and return its (team id, name). With no `user_id` it's
    just a name, with no Discord account behind it. A name already taken in this
    tournament gets a number added, so admin commands that find teams by name still work."""
    taken = {team["name"] for team in get_teams(conn, tournament_id).values()}
    unique_name = name
    number = 2
    while unique_name in taken:
        unique_name = f"{name} ({number})"
        number += 1
    with conn:
        team_id = conn.execute(
            "INSERT INTO teams (tournament_id, name, seed) VALUES (?, ?, NULL)", (tournament_id, unique_name)
        ).lastrowid
        if user_id is not None:
            conn.execute(
                "INSERT INTO team_members (team_id, tournament_id, user_id) VALUES (?, ?, ?)",
                (team_id, tournament_id, user_id),
            )
    return team_id, unique_name


def unregister_entrant(conn: sqlite3.Connection, tournament_id: int, user_id: int) -> bool:
    """Withdraw a user's team. Returns False if they weren't registered."""
    with conn:
        cur = conn.execute(
            """DELETE FROM teams WHERE id = (
                   SELECT team_id FROM team_members WHERE tournament_id = ? AND user_id = ?)""",
            (tournament_id, user_id),
        )
    return cur.rowcount > 0


def get_entrant_team_id(conn: sqlite3.Connection, tournament_id: int, user_id: int) -> int | None:
    row = conn.execute(
        "SELECT team_id FROM team_members WHERE tournament_id = ? AND user_id = ?", (tournament_id, user_id)
    ).fetchone()
    return row["team_id"] if row is not None else None


def get_entrants(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    """The tournament's teams with their members' user ids, in sign-up order. A team with
    no Discord account behind it has one row, with user_id None."""
    return conn.execute(
        """SELECT t.id AS team_id, t.name, tm.user_id FROM teams t
           LEFT JOIN team_members tm ON tm.team_id = t.id
           WHERE t.tournament_id = ? ORDER BY t.id""",
        (tournament_id,),
    ).fetchall()


def delete_team(conn: sqlite3.Connection, team_id: int) -> None:
    conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    conn.commit()


def get_team_by_name(conn: sqlite3.Connection, tournament_id: int, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM teams WHERE tournament_id = ? AND name = ?", (tournament_id, name)
    ).fetchone()


def team_name_taken(conn: sqlite3.Connection, tournament_id: int, name: str, team_id: int) -> bool:
    """True if a team other than `team_id` in this tournament is already called `name`.
    Admin commands find teams by name, so two with one name would be ambiguous."""
    other = get_team_by_name(conn, tournament_id, name)
    return other is not None and other["id"] != team_id


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


def get_matches(conn: sqlite3.Connection, tournament_id: int) -> dict[int, Match]:
    return _load_matches(conn, tournament_id)[0]


def _load_matches(conn: sqlite3.Connection, tournament_id: int) -> tuple[dict[int, Match], dict]:
    """A tournament's matches, numbered by played round, and the feeder index used to number them."""
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
    feeders_by_target = build_feeder_index(list(matches_by_id.values()))
    assign_played_rounds(matches_by_id, feeders_by_target)
    return matches_by_id, feeders_by_target


def load_bracket(conn: sqlite3.Connection, tournament_id: int) -> tuple[dict[int, Match], dict, dict[int, sqlite3.Row]]:
    """Everything needed to walk a tournament's tree: (matches_by_id, feeders_by_target, teams_by_id)."""
    matches_by_id, feeders_by_target = _load_matches(conn, tournament_id)
    return matches_by_id, feeders_by_target, get_teams(conn, tournament_id)


def load_scores(conn: sqlite3.Connection, tournament_id: int) -> list[UserScore]:
    """Every predicting player's score so far, best first."""
    return compute_leaderboard(
        get_matches(conn, tournament_id), get_results(conn, tournament_id), get_all_predictions(conn, tournament_id)
    )


def load_final_results(conn: sqlite3.Connection, tournament_id: int) -> tuple[Podium, dict[int, sqlite3.Row], list[UserScore]]:
    """What a finished tournament's results post needs: (podium, teams_by_id, scores)."""
    matches_by_id, feeders_by_target, teams_by_id = load_bracket(conn, tournament_id)
    results_by_match = get_results(conn, tournament_id)
    scores = compute_leaderboard(matches_by_id, results_by_match, get_all_predictions(conn, tournament_id))
    return podium(matches_by_id, feeders_by_target, results_by_match), teams_by_id, scores


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


# -- moderation -----------------------------------------------------------------

def ban_user(conn: sqlite3.Connection, guild_id: int, user_id: int, banned_by: int, reason: str | None) -> None:
    """Block a user from the bot in a server. Banning someone already banned updates the reason."""
    conn.execute(
        """INSERT INTO bans (guild_id, user_id, reason, banned_by) VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET
               reason = excluded.reason, banned_by = excluded.banned_by,
               banned_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (guild_id, user_id, reason, banned_by),
    )
    conn.commit()


def unban_user(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """Returns False if the user wasn't banned."""
    cur = conn.execute("DELETE FROM bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def is_banned(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM bans WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone() is not None


def get_bans(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bans WHERE guild_id = ? ORDER BY banned_at", (guild_id,)).fetchall()


def add_feedback(conn: sqlite3.Connection, guild_id: int, message: str) -> None:
    conn.execute("INSERT INTO feedback (guild_id, message) VALUES (?, ?)", (guild_id, message))
    conn.commit()


def get_feedback(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """A server's feedback, newest first."""
    return conn.execute("SELECT * FROM feedback WHERE guild_id = ? ORDER BY id DESC", (guild_id,)).fetchall()


def delete_feedback(conn: sqlite3.Connection, guild_id: int, feedback_id: int) -> bool:
    """Returns False if there's no such feedback in this server."""
    cur = conn.execute("DELETE FROM feedback WHERE guild_id = ? AND id = ?", (guild_id, feedback_id))
    conn.commit()
    return cur.rowcount > 0
