"""SQLite persistence layer. No discord.py imports here."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from bracket import (
    Match,
    MatchSpec,
    Outcomes,
    Podium,
    UserScore,
    assign_played_rounds,
    build_feeder_index,
    compute_leaderboard,
    final_match,
    later_matches,
    played_games,
    podium,
    resolve_all_slots,
    resolve_slot,
    settle,
)
from ratings import PlayedMatch

DB_PATH = Path(__file__).parent / "data" / "tourney.db"

# Tournament statuses, in lifecycle order. A tournament made from a CSV starts at setup;
# one that takes sign-ups starts at registration and has no matches until it closes.
# A concluded tournament keeps all its data for later use (such as seeding). Only the
# history commands and a few admin commands look it up, and its name is never reused, so
# past tournaments can always be told apart.
STATUSES = ("registration", "setup", "predictions_open", "in_progress", "completed", "concluded")
# Concluding can't be undone from Discord, so admins can't force it; /tournament
# conclude only allows it once a tournament is completed. Registration can't be forced
# either way: going back to it would leave a bracket behind, and leaving it any other
# way than closing registration would leave a tournament with no bracket.
FORCEABLE_STATUSES = tuple(status for status in STATUSES if status not in ("registration", "concluded"))
_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in STATUSES)
# How closing registration places the entrants: seeded by Elo from past tournaments, with
# the top seeds facing the lowest (standard) or the middle of the field (slide), or drawn
# at random. seeding.seeded_pairs shows the difference.
SEEDING_MODES = ("standard", "slide", "random")
_SEEDING_SQL_LIST = ", ".join(f"'{mode}'" for mode in SEEDING_MODES)
# A 5v5 event ("lobbies") has no bracket: its players are drawn into lobbies of two teams
# instead (lobbies.py, lobby_db.py).
FORMATS = ("single_elim", "lobbies")
# Why an entrant stopped playing: they dropped out themselves, or an organizer withdrew or
# disqualified them. Each has its own value, so it's always clear who decided.
WITHDRAWAL_REASONS = ("dropped", "withdrawn", "disqualified")
_WITHDRAWAL_SQL_LIST = ", ".join(f"'{reason}'" for reason in WITHDRAWAL_REASONS)
_WITHDRAWN_DEFINITION = f"TEXT CHECK (withdrawn IN ({_WITHDRAWAL_SQL_LIST}))"
_FORMAT_SQL_LIST = ", ".join(f"'{format}'" for format in FORMATS)


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
    announcement_message_id INTEGER,
    seeding_mode TEXT NOT NULL CHECK (seeding_mode IN ({_SEEDING_SQL_LIST})) DEFAULT 'random',
    format      TEXT NOT NULL CHECK (format IN ({_FORMAT_SQL_LIST})) DEFAULT 'single_elim',
    -- How many matches the bot calls at once, or NULL if it doesn't coordinate them.
    match_slots INTEGER CHECK (match_slots >= 1),
    -- For 5v5 events: when play starts, how players are shared between lobbies, and when
    -- check-in ends once an organizer starts it.
    starts_at   TEXT,
    lobby_spread TEXT CHECK (lobby_spread IN ('tier', 'balanced', 'random')),
    checkin_ends_at TEXT,
    checkin_finished INTEGER NOT NULL DEFAULT 0
);
"""


# Created by init_db rather than SCHEMA, because older databases may hold duplicate names
# that have to be renamed first.
_NAME_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_name ON tournaments(guild_id, name);
"""

SCHEMA = f"""
PRAGMA foreign_keys = ON;
{_tournaments_table_sql("tournaments")}

CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    seed            INTEGER,
    -- Set once the entrant stops playing, which hands their remaining matches to their opponents.
    withdrawn       {_WITHDRAWN_DEFINITION},
    withdrawn_by    INTEGER,
    withdrawn_at    TEXT
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
    recorded_by     INTEGER NOT NULL,  -- 0 when the bot recorded it
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- 1 when the loser never readied up for a called match, so it wasn't played.
    walkover        INTEGER NOT NULL DEFAULT 0
);

-- Matches called off with no winner. The matchup is kept so the skip can be dropped if an
-- earlier result changes who's in the match. Walkovers, and matches nobody is left to
-- play, are never stored: bracket.settle works them out from these and withdrawals.
CREATE TABLE IF NOT EXISTS skipped_matches (
    match_id        INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    team_a_id       INTEGER REFERENCES teams(id),
    team_b_id       INTEGER REFERENCES teams(id),
    skipped_by      INTEGER NOT NULL,
    skipped_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Players' votes on who won their match, one per side. Any member of a side can cast,
-- change or take back its vote, and it's dropped once the match is decided.
CREATE TABLE IF NOT EXISTS result_reports (
    match_id        INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    winner_team_id  INTEGER NOT NULL REFERENCES teams(id),
    reported_by     INTEGER NOT NULL,
    reported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- Where it was reported, so a warning about it is posted there.
    channel_id      INTEGER NOT NULL,
    PRIMARY KEY (match_id, team_id)
);

-- The one public message about a match, from when it's called (or first needs a warning
-- about a vote, or a dispute) until it shows how the match ended, when the row goes. It's
-- edited as things change, or posted afresh when someone needs notifying, which an edit
-- doesn't do. The matchup is kept so it still names the right players if an earlier
-- result changes.
CREATE TABLE IF NOT EXISTS match_posts (
    match_id    INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL,
    team_a_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    team_b_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    message_id  INTEGER,  -- NULL until the message is sent
    shown       TEXT,
    pinged      TEXT,  -- what the current message notified people about (match_posts.PostState.ping)
    -- When a warning about an unanswered vote went up, moved on each time the votes change
    -- after it, since that restarts the time the other side has to answer.
    warned_at   TEXT,
    -- Set while a new message for the post is being sent, so nothing else sends one too.
    sending_at  TEXT
);

-- Matches the bot has called, in tournaments that coordinate their matches. A call holds
-- one of the tournament's match_slots until the match is decided, and once both sides are
-- ready, one of the server's match rooms (a voice channel).
CREATE TABLE IF NOT EXISTS match_calls (
    match_id    INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    team_a_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    team_b_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    called_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reminded_at TEXT,
    started_at  TEXT,
    room_id     INTEGER
);
-- Matches ready to be called, in the order they became ready, across every coordinated
-- tournament in a server. A room that comes free goes to the longest-waiting match.
CREATE TABLE IF NOT EXISTS match_queue (
    position    INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL UNIQUE REFERENCES matches(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS match_ready (
    match_id    INTEGER NOT NULL REFERENCES match_calls(match_id) ON DELETE CASCADE,
    team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    ready_by    INTEGER NOT NULL,
    PRIMARY KEY (match_id, team_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id                INTEGER PRIMARY KEY,
    -- Every voice channel in this category is a room the bot can send a called match to.
    match_room_category_id  INTEGER
);

-- Match post messages a new message took the place of, which couldn't be deleted from
-- Discord at the time. Each is deleted once it can be, so a match keeps one post.
CREATE TABLE IF NOT EXISTS replaced_match_posts (
    message_id  INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL
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

-- The latest bracket or leaderboard posted in a channel for each tournament, which the
-- bot keeps up to date so people can be pointed at it instead of getting a new post.
CREATE TABLE IF NOT EXISTS live_posts (
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('bracket', 'leaderboard')),
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    -- A fingerprint of what the post shows, so a restart doesn't re-edit (and mark as
    -- "edited") a post that's already current.
    shown           TEXT,
    PRIMARY KEY (tournament_id, kind)
);

-- 5v5 events. Each sign-up says whether they'll captain, and checks in once an organizer
-- starts check-in. Entrants with no account count as checked in, since they can't press
-- anything.
CREATE TABLE IF NOT EXISTS lobby_signups (
    team_id         INTEGER PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    captain_pref    TEXT NOT NULL CHECK (captain_pref IN ('want', 'can', 'no')),
    checked_in_at   TEXT
);
CREATE TABLE IF NOT EXISTS lobbies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,
    -- The drafter room the lobby plays in, and who gave it to the bot.
    room_code       TEXT,
    host_user_id    INTEGER,
    UNIQUE (tournament_id, number)
);
-- A lobby's two teams. The captain is NULL when nobody left on the team can captain.
CREATE TABLE IF NOT EXISTS lobby_sides (
    lobby_id        INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    side            TEXT NOT NULL CHECK (side IN ('A', 'B')),
    captain_team_id INTEGER REFERENCES teams(id),
    PRIMARY KEY (lobby_id, side)
);
-- Who plays where. A replacement takes over the spot of the player they replace, so
-- teams never change once they're announced except one player for another.
CREATE TABLE IF NOT EXISTS lobby_players (
    lobby_id        INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    side            TEXT NOT NULL,
    spot            INTEGER NOT NULL,
    team_id         INTEGER NOT NULL UNIQUE REFERENCES teams(id) ON DELETE CASCADE,
    PRIMARY KEY (lobby_id, side, spot)
);
CREATE TABLE IF NOT EXISTS lobby_waitlist (
    team_id         INTEGER PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL
);
-- A captain's report of everyone's points, waiting for the other captain. `points` is
-- JSON, entrant id to points.
CREATE TABLE IF NOT EXISTS lobby_reports (
    lobby_id        INTEGER PRIMARY KEY REFERENCES lobbies(id) ON DELETE CASCADE,
    reported_by     INTEGER NOT NULL,
    reporting_side  TEXT NOT NULL,
    points          TEXT NOT NULL,
    changed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    disputed_at     TEXT
);
CREATE TABLE IF NOT EXISTS lobby_results (
    lobby_id        INTEGER PRIMARY KEY REFERENCES lobbies(id) ON DELETE CASCADE,
    winning_side    TEXT NOT NULL,
    recorded_by     INTEGER,  -- NULL when a report was accepted for want of an answer
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS lobby_points (
    lobby_id        INTEGER NOT NULL REFERENCES lobby_results(lobby_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    points          INTEGER NOT NULL,
    PRIMARY KEY (lobby_id, team_id)
);
-- A 5v5 event's one public post (`post_key` 'event'; other keys are from older versions,
-- which had a post per lobby, and are skipped). It's edited as things change, and sent
-- afresh when someone needs notifying, like match_posts.
CREATE TABLE IF NOT EXISTS lobby_posts (
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    post_key        TEXT NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    shown           TEXT,
    pinged          TEXT,
    sending_at      TEXT,
    PRIMARY KEY (tournament_id, post_key)
);

-- Members who agreed to have their entries, results and rating tied to their Discord
-- account. Only agreement is kept: someone who opts out is asked again at their next
-- sign-up, and entered by name only meanwhile.
CREATE TABLE IF NOT EXISTS consents (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    agreed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (guild_id, user_id)
);
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
SCHEMA_VERSION = 9


def init_db(conn: sqlite3.Connection) -> None:
    is_new = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tournaments'").fetchone() is None
    conn.executescript(SCHEMA)
    if is_new:
        conn.execute(_NAME_INDEX_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    else:
        _rebuild_tournaments_table(conn)
        _add_column_if_missing(conn, "live_posts", "shown", "TEXT")
        _add_column_if_missing(conn, "teams", "withdrawn", _WITHDRAWN_DEFINITION)
        _add_column_if_missing(conn, "teams", "withdrawn_by", "INTEGER")
        _add_column_if_missing(conn, "teams", "withdrawn_at", "TEXT")
        _add_column_if_missing(conn, "match_results", "walkover", "INTEGER NOT NULL DEFAULT 0")
        _move_report_posts(conn)
    conn.commit()


def _move_report_posts(conn: sqlite3.Connection) -> None:
    """Move posts from report_posts and replaced_report_posts, which match_posts and
    replaced_match_posts took over from. Every report post was a warning or a dispute,
    so its last change is when its warning went up, and a warning has already notified
    the side it's waiting for, so moving it mustn't send it again."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "report_posts" in tables:
        for post in conn.execute("SELECT * FROM report_posts").fetchall():
            votes = conn.execute("SELECT team_id FROM result_reports WHERE match_id = ?", (post["match_id"],)).fetchall()
            sides = (post["team_a_id"], post["team_b_id"])
            pinged = f"warning:{next(s for s in sides if s != votes[0]['team_id'])}" if len(votes) == 1 else None
            conn.execute(
                """INSERT OR IGNORE INTO match_posts
                       (match_id, channel_id, team_a_id, team_b_id, message_id, shown, warned_at, pinged)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (post["match_id"], post["channel_id"], *sides, post["message_id"], post["shown"], post["updated_at"],
                 pinged))
        conn.execute("DROP TABLE report_posts")
    if "replaced_report_posts" in tables:
        conn.execute("INSERT OR IGNORE INTO replaced_match_posts SELECT message_id, channel_id FROM replaced_report_posts")
        conn.execute("DROP TABLE replaced_report_posts")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """For a new column on a table that has no constraint changes, which SQLite can add in place."""
    if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _rebuild_tournaments_table(conn: sqlite3.Connection) -> None:
    """Older databases have a tournaments table whose CHECKs reject newer values. SQLite
    can't alter them in place, so the table is rebuilt, following SQLite's documented
    steps for changing a table that other tables reference. Columns the old table already
    has are copied across; newer ones start empty."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # has no effect inside a transaction
    try:
        conn.execute("BEGIN")
        conn.execute(_tournaments_table_sql("tournaments_new"))
        old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tournaments)")}
        columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(tournaments_new)") if row["name"] in old_columns
        ]
        # Older databases call standard seeding 'auto'.
        values = ["CASE seeding_mode WHEN 'auto' THEN 'standard' ELSE seeding_mode END" if column == "seeding_mode"
                  else column for column in columns]
        conn.execute(f"INSERT INTO tournaments_new ({', '.join(columns)}) SELECT {', '.join(values)} FROM tournaments")
        # Keep the id counter, so ids of deleted tournaments aren't handed out again.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tournaments_new'")
        conn.execute("""INSERT INTO sqlite_sequence (name, seq)
                        SELECT 'tournaments_new', seq FROM sqlite_sequence WHERE name = 'tournaments'""")
        conn.execute("DROP TABLE tournaments")
        conn.execute("ALTER TABLE tournaments_new RENAME TO tournaments")
        _rename_duplicate_names(conn)
        conn.execute(_NAME_INDEX_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _rename_duplicate_names(conn: sqlite3.Connection) -> None:
    """Versions 1 to 5 let a new tournament reuse a concluded one's name. The newest with
    each name keeps it, and older ones get their creation date added: 'Cup (2026-03-01)'."""
    taken = {(row["guild_id"], row["name"]) for row in conn.execute("SELECT guild_id, name FROM tournaments")}
    older = conn.execute(
        """SELECT id, guild_id, name, created_at FROM tournaments t WHERE EXISTS (
               SELECT 1 FROM tournaments newer WHERE newer.guild_id = t.guild_id AND newer.name = t.name
               AND newer.id > t.id) ORDER BY id"""
    ).fetchall()
    for row in older:
        date = row["created_at"][:10]
        new_name = f"{row['name']} ({date})"
        number = 2
        while (row["guild_id"], new_name) in taken:
            new_name = f"{row['name']} ({date}, {number})"
            number += 1
        taken.add((row["guild_id"], new_name))
        conn.execute("UPDATE tournaments SET name = ? WHERE id = ?", (new_name, row["id"]))


# -- tournaments -------------------------------------------------------------

def create_tournament(conn: sqlite3.Connection, guild_id: int, name: str, created_by: int) -> int:
    cur = conn.execute(
        "INSERT INTO tournaments (guild_id, name, created_by) VALUES (?, ?, ?)",
        (guild_id, name, created_by),
    )
    conn.commit()
    return cur.lastrowid


def open_registration(conn: sqlite3.Connection, guild_id: int, name: str, created_by: int,
                      channel_id: int | None, closes_at: str | None, seeding_mode: str = "standard",
                      format: str = "single_elim", starts_at: str | None = None,
                      lobby_spread: str | None = None) -> int:
    """Create a tournament that takes sign-ups. `closes_at` is a UTC time in the same
    format as created_at, or None to close it by hand. A 5v5 event (format "lobbies")
    also has a start time and a way of spreading players between lobbies."""
    cur = conn.execute(
        """INSERT INTO tournaments (guild_id, name, created_by, status, channel_id, registration_closes_at,
                                    seeding_mode, format, starts_at, lobby_spread)
           VALUES (?, ?, ?, 'registration', ?, ?, ?, ?, ?, ?)""",
        (guild_id, name, created_by, channel_id, closes_at, seeding_mode, format, starts_at, lobby_spread),
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


class ConcludedSummary(NamedTuple):
    """One line of /history list."""

    name: str
    champion: Optional[str]
    entrants: int
    format: str
    has_teams: bool  # any entrant with more than one Discord user behind it
    finished_at: Optional[str]  # when its final (or a 5v5 event's last lobby) was recorded


def get_concluded_summaries(conn: sqlite3.Connection, guild_id: int) -> list[ConcludedSummary]:
    """The server's concluded tournaments, newest first, summed up for /history list."""
    # The final is the one match that feeds into nothing.
    rows = conn.execute(
        """SELECT t.name, t.format,
                   (SELECT w.name FROM matches m JOIN match_results r ON r.match_id = m.id
                    JOIN teams w ON w.id = r.winner_team_id
                    WHERE m.tournament_id = t.id AND m.feeds_into_match_id IS NULL) AS champion,
                   COALESCE((SELECT r.recorded_at FROM matches m JOIN match_results r ON r.match_id = m.id
                             WHERE m.tournament_id = t.id AND m.feeds_into_match_id IS NULL),
                            (SELECT MAX(r.recorded_at) FROM lobbies l JOIN lobby_results r ON r.lobby_id = l.id
                             WHERE l.tournament_id = t.id)) AS finished_at,
                   (SELECT COUNT(*) FROM teams WHERE tournament_id = t.id) AS entrants,
                   EXISTS (SELECT 1 FROM team_members WHERE tournament_id = t.id
                           GROUP BY team_id HAVING COUNT(*) > 1) AS has_teams
            FROM tournaments t WHERE t.guild_id = ? AND t.status = 'concluded'
            ORDER BY t.created_at DESC, t.id DESC""",
        (guild_id,),
    ).fetchall()
    return [ConcludedSummary(r["name"], r["champion"], r["entrants"], r["format"], bool(r["has_teams"]), r["finished_at"])
            for r in rows]


def get_tournament_by_name_any_status(conn: sqlite3.Connection, guild_id: int, name: str) -> sqlite3.Row | None:
    """Names are never reused, so this is the one tournament that has had `name`."""
    return conn.execute("SELECT * FROM tournaments WHERE guild_id = ? AND name = ?", (guild_id, name)).fetchone()


def get_concluded_tournament_by_name(conn: sqlite3.Connection, guild_id: int, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tournaments WHERE guild_id = ? AND name = ? AND status = 'concluded'", (guild_id, name)
    ).fetchone()


def get_tournament(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,)).fetchone()


def get_concluded_tournaments(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """The server's concluded tournaments, newest first."""
    return conn.execute(
        "SELECT * FROM tournaments WHERE guild_id = ? AND status = 'concluded' ORDER BY created_at DESC, id DESC",
        (guild_id,),
    ).fetchall()


def get_tournament_names(conn: sqlite3.Connection, guild_id: int, status: str | None = None) -> list[str]:
    """Names of tournaments that haven't been concluded, newest first, optionally only those at `status`."""
    rows = conn.execute(
        """SELECT name FROM tournaments WHERE guild_id = ? AND status != 'concluded'
           AND (? IS NULL OR status = ?) ORDER BY created_at DESC""",
        (guild_id, status, status),
    ).fetchall()
    return [r["name"] for r in rows]


def set_tournament_status(conn: sqlite3.Connection, tournament_id: int, status: str) -> str:
    """Returns the status stored, which is completed instead of in_progress when
    walkovers already decided the final."""
    with conn:
        conn.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, tournament_id))
        matches_by_id, _ = _load_matches(conn, tournament_id)
        if status == "in_progress" and matches_by_id:
            _keep_completion_in_step(conn, tournament_id,
                                     final_match(matches_by_id).id in get_outcomes(conn, tournament_id).decided)
        return get_tournament(conn, tournament_id)["status"]


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


def set_seeding_mode(conn: sqlite3.Connection, tournament_id: int, seeding_mode: str) -> None:
    """Without committing, so it lands together with the bracket it describes."""
    conn.execute("UPDATE tournaments SET seeding_mode = ? WHERE id = ?", (seeding_mode, tournament_id))


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
                     user_id: int | None) -> tuple[int, str] | None:
    """Sign up a one-person team and return its (team id, name). With no `user_id` it's
    just a name, with no Discord account behind it.

    Nothing is saved, and None is returned, if the name belongs to an entrant with no
    account behind it, or to anyone when there's no `user_id`: two name-only entrants
    with one name couldn't be told apart. That entrant may well be this member, entered
    by name before they agreed, but anyone can set their display name to match, so only
    an organizer links them to it (/admin linkplayer). A name taken by another member
    gets a number added, so admin commands that find teams by name still work."""
    existing = get_team_by_name(conn, tournament_id, name)
    if existing is not None and (user_id is None or not team_member_ids(conn, existing["id"])):
        return None
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


def unlink_entrant(conn: sqlite3.Connection, team_id: int) -> int | None:
    """Unlink the member behind one entrant, leaving the entrant under its name and the
    member's agreement and other entries as they are. Returns their user id, or None if
    nobody was linked."""
    user_ids = team_member_ids(conn, team_id)
    if not user_ids:
        return None
    with conn:
        conn.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_ids[0]))
    return user_ids[0]


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


def entrant_count(conn: sqlite3.Connection, tournament_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM teams WHERE tournament_id = ?", (tournament_id,)).fetchone()[0]


def get_entrants(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    """The tournament's teams with their members' user ids, in sign-up order. A team with
    no Discord account behind it has one row, with user_id None."""
    return conn.execute(
        """SELECT t.id AS team_id, t.name, tm.user_id FROM teams t
           LEFT JOIN team_members tm ON tm.team_id = t.id
           WHERE t.tournament_id = ? ORDER BY t.id""",
        (tournament_id,),
    ).fetchall()


def get_members_by_team(conn: sqlite3.Connection, tournament_id: int) -> dict[int, list[int]]:
    """Each team's Discord users, an empty list for a team with no account behind it."""
    members: dict[int, list[int]] = {}
    for entrant in get_entrants(conn, tournament_id):
        members.setdefault(entrant["team_id"], [])
        if entrant["user_id"] is not None:
            members[entrant["team_id"]].append(entrant["user_id"])
    return members


def team_member_ids(conn: sqlite3.Connection, team_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM team_members WHERE team_id = ?", (team_id,))]


def link_team_member(conn: sqlite3.Connection, tournament_id: int, team_id: int, user_id: int) -> bool:
    """Record that a Discord user played on a team, such as one imported from a CSV before
    they could be linked. False if they're already on a team in that tournament."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO team_members (team_id, tournament_id, user_id) VALUES (?, ?, ?)",
                (team_id, tournament_id, user_id),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def set_team_seeds(conn: sqlite3.Connection, ordered_team_ids: list[int]) -> None:
    """Number teams 1, 2, 3... in the order given, without committing."""
    conn.executemany("UPDATE teams SET seed = ? WHERE id = ?",
                     [(seed, team_id) for seed, team_id in enumerate(ordered_team_ids, start=1)])


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
        get_matches(conn, tournament_id), get_outcomes(conn, tournament_id).played,
        get_all_predictions(conn, tournament_id),
    )


def load_final_results(conn: sqlite3.Connection, tournament_id: int) -> tuple[Podium, dict[int, sqlite3.Row], list[UserScore]]:
    """What a finished tournament's results post needs: (podium, teams_by_id, scores)."""
    matches_by_id, feeders_by_target, teams_by_id = load_bracket(conn, tournament_id)
    outcomes = get_outcomes(conn, tournament_id)
    scores = compute_leaderboard(matches_by_id, outcomes.played, get_all_predictions(conn, tournament_id))
    return podium(matches_by_id, feeders_by_target, outcomes), teams_by_id, scores


def load_match_history(conn: sqlite3.Connection, guild_id: int) -> list[PlayedMatch]:
    """Every game played in the server's concluded tournaments, as the Discord users on
    each side, in the order they were played: oldest tournament first, then round by
    round. Ratings are worked out from this. The order comes from the bracket rather than
    when results were recorded, because correcting a result re-records it."""
    games = []
    for tournament in reversed(get_concluded_tournaments(conn, guild_id)):
        matches_by_id, feeders = _load_matches(conn, tournament["id"])
        members = get_members_by_team(conn, tournament["id"])
        played = played_games(matches_by_id, feeders, get_outcomes(conn, tournament["id"]))
        for match, winner, loser in sorted(played, key=lambda game: game[0].played_round):
            games.append(PlayedMatch(tuple(members.get(winner, [])), tuple(members.get(loser, []))))
    return games


# -- results ------------------------------------------------------------------

class MatchNow(NamedTuple):
    status: str  # the tournament's
    teams: tuple[Optional[int], Optional[int]]
    winner: Optional[int]
    skipped: bool
    from_withdrawals: bool  # see Outcomes.from_withdrawals


def match_now(conn: sqlite3.Connection, tournament_id: int, match_id: int) -> MatchNow | None:
    """A match's teams and how it ended, as they stand now, with its tournament's status,
    or None if the tournament is gone. A menu can sit open while others change the
    bracket, so menus check this before saving what was picked from them."""
    tournament = get_tournament(conn, tournament_id)
    if tournament is None:
        return None
    matches_by_id, feeders, _ = load_bracket(conn, tournament_id)
    if match_id not in matches_by_id:
        return None
    outcomes = get_outcomes(conn, tournament_id)
    teams = resolve_slot(matches_by_id[match_id], matches_by_id, outcomes.winners.get, feeders)
    return MatchNow(tournament["status"], teams, outcomes.winners.get(match_id), match_id in outcomes.skipped,
                    match_id in outcomes.from_withdrawals)


def get_outcomes(conn: sqlite3.Connection, tournament_id: int) -> Outcomes:
    """How every decided match ended, including the walkovers and skips that follow from
    withdrawals and other skips."""
    matches_by_id, feeders = _load_matches(conn, tournament_id)
    rows = conn.execute(
        """SELECT mr.match_id, mr.winner_team_id, mr.walkover FROM match_results mr
           JOIN matches m ON m.id = mr.match_id WHERE m.tournament_id = ?""",
        (tournament_id,),
    ).fetchall()
    recorded = {r["match_id"]: r["winner_team_id"] for r in rows}
    forfeits = frozenset(r["match_id"] for r in rows if r["walkover"])
    skips = {
        r["match_id"]: (r["team_a_id"], r["team_b_id"]) for r in conn.execute(
            """SELECT sm.match_id, sm.team_a_id, sm.team_b_id FROM skipped_matches sm
               JOIN matches m ON m.id = sm.match_id WHERE m.tournament_id = ?""",
            (tournament_id,),
        )
    }
    withdrawn = {
        r["id"] for r in conn.execute(
            "SELECT id FROM teams WHERE tournament_id = ? AND withdrawn IS NOT NULL", (tournament_id,)
        )
    }
    return settle(matches_by_id, feeders, recorded, skips, withdrawn, forfeits)


def final_decided(conn: sqlite3.Connection, tournament_id: int) -> bool:
    """Whether the final has a winner, or was called off."""
    return final_match(get_matches(conn, tournament_id)).id in get_outcomes(conn, tournament_id).decided


def get_results(conn: sqlite3.Connection, tournament_id: int) -> dict[int, int]:
    """Each decided match's winner, whether the game was played or won by walkover."""
    return get_outcomes(conn, tournament_id).winners


def record_result(conn: sqlite3.Connection, tournament_id: int, match_id: int,
                  winner_team_id: int, recorded_by: int) -> list[int]:
    """Save (or overwrite) a match's real winner, replacing any skip. Returns the ids of
    later results that were dropped because they no longer fit the bracket."""
    with conn:
        return _record_result(conn, tournament_id, match_id, winner_team_id, recorded_by)


def _record_result(conn: sqlite3.Connection, tournament_id: int, match_id: int,
                   winner_team_id: int, recorded_by: int, walkover: bool = False) -> list[int]:
    if match_id in get_outcomes(conn, tournament_id).from_withdrawals:
        # Withdrawals settled it with no game, so a played result would be made up.
        # Reinstating the entrant reopens the match.
        raise ValueError(f"match {match_id} was settled by withdrawals, so it has no game to record")
    conn.execute("DELETE FROM skipped_matches WHERE match_id = ?", (match_id,))
    conn.execute(
        """INSERT INTO match_results (match_id, winner_team_id, recorded_by, walkover)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET
               winner_team_id = excluded.winner_team_id,
               recorded_by = excluded.recorded_by,
               recorded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
               walkover = excluded.walkover""",
        (match_id, winner_team_id, recorded_by, int(walkover)),
    )
    return _settle_results(conn, tournament_id)


def skip_match(conn: sqlite3.Connection, tournament_id: int, match_id: int,
               teams: tuple[int, int], skipped_by: int) -> list[int]:
    """Call a match between `teams` off with no winner, replacing any result. Returns the
    ids of later results dropped with it."""
    with conn:
        return _skip_match(conn, tournament_id, match_id, teams, skipped_by)


def _skip_match(conn: sqlite3.Connection, tournament_id: int, match_id: int,
                teams: tuple[int, int], skipped_by: int) -> list[int]:
    conn.execute("DELETE FROM match_results WHERE match_id = ?", (match_id,))
    conn.execute(
        """INSERT INTO skipped_matches (match_id, team_a_id, team_b_id, skipped_by) VALUES (?, ?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET
               team_a_id = excluded.team_a_id, team_b_id = excluded.team_b_id,
               skipped_by = excluded.skipped_by, skipped_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (match_id, *teams, skipped_by),
    )
    return _settle_results(conn, tournament_id)


def clear_result(conn: sqlite3.Connection, tournament_id: int, match_id: int) -> list[int]:
    """Delete a match's recorded result or skip. Returns the ids of later results dropped with it."""
    with conn:
        conn.execute("DELETE FROM match_results WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM skipped_matches WHERE match_id = ?", (match_id,))
        return _settle_results(conn, tournament_id)


def withdraw_entrant(conn: sqlite3.Connection, tournament_id: int, team_id: int, reason: str,
                     withdrawn_by: int) -> list[int]:
    """Take an entrant out of their remaining matches, which their opponents then win by
    walkover. Results they've already played stand. Returns the ids of results dropped."""
    with conn:
        conn.execute(
            """UPDATE teams SET withdrawn = ?, withdrawn_by = ?, withdrawn_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE id = ?""",
            (reason, withdrawn_by, team_id),
        )
        return _settle_results(conn, tournament_id)


def stored_later_results(conn: sqlite3.Connection, tournament_id: int, match_id: int) -> list[int]:
    """The later matches on `match_id`'s path that have a recorded result or were called
    off, nearest first. Changing `match_id` would change who played them."""
    matches_by_id, _ = _load_matches(conn, tournament_id)
    stored = {row[0] for row in conn.execute(
        """SELECT r.match_id FROM match_results r JOIN matches m ON m.id = r.match_id WHERE m.tournament_id = ?
           UNION SELECT s.match_id FROM skipped_matches s JOIN matches m ON m.id = s.match_id
           WHERE m.tournament_id = ?""", (tournament_id, tournament_id))}
    return [m.id for m in later_matches(matches_by_id[match_id], matches_by_id) if m.id in stored]


def reinstate_entrant(conn: sqlite3.Connection, tournament_id: int, team_id: int) -> list[int]:
    """Undo a withdrawal. The walkovers it gave go away, along with any later results that
    followed from them, whose ids are returned."""
    with conn:
        conn.execute("UPDATE teams SET withdrawn = NULL, withdrawn_by = NULL, withdrawn_at = NULL WHERE id = ?",
                     (team_id,))
        return _settle_results(conn, tournament_id)


def _settle_results(conn: sqlite3.Connection, tournament_id: int) -> list[int]:
    """After a result, skip or withdrawal changes, drop recorded results and skips it
    invalidated, and votes on matches that are now decided or whose matchup changed. Keep
    the status in step: a decided final means completed, and an undecided one can't stay
    completed."""
    matches_by_id, feeders = _load_matches(conn, tournament_id)
    stale = get_outcomes(conn, tournament_id).stale
    for table in ("match_results", "skipped_matches"):
        conn.executemany(f"DELETE FROM {table} WHERE match_id = ?", [(match_id,) for match_id in stale])
    outcomes = get_outcomes(conn, tournament_id)
    slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
    for vote in conn.execute(
        """SELECT r.match_id, r.team_id, r.winner_team_id FROM result_reports r
           JOIN matches m ON m.id = r.match_id WHERE m.tournament_id = ?""", (tournament_id,)
    ).fetchall():
        teams = slots[vote["match_id"]]
        if vote["match_id"] in outcomes.decided or vote["team_id"] not in teams or vote["winner_team_id"] not in teams:
            conn.execute("DELETE FROM result_reports WHERE match_id = ? AND team_id = ?",
                         (vote["match_id"], vote["team_id"]))
    # A decided match frees its call, and so does one whose matchup changed, so it can be
    # called again for whoever is in it now.
    for call in get_calls(conn, tournament_id):
        if call["match_id"] in outcomes.decided or set(slots[call["match_id"]]) != {call["team_a_id"], call["team_b_id"]}:
            conn.execute("DELETE FROM match_calls WHERE match_id = ?", (call["match_id"],))

    _keep_completion_in_step(conn, tournament_id, final_match(matches_by_id).id in outcomes.decided)
    return stale


def _keep_completion_in_step(conn: sqlite3.Connection, tournament_id: int, final_decided: bool) -> None:
    """Once play has started, a decided final means completed and an undecided one means
    in progress. Before that, a final settled early by walkovers waits for the lock."""
    status = get_tournament(conn, tournament_id)["status"]
    if status in ("in_progress", "completed"):
        status = "completed" if final_decided else "in_progress"
        conn.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, tournament_id))


# -- result reports -------------------------------------------------------------

def get_reports(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM result_reports WHERE match_id = ? ORDER BY reported_at", (match_id,)).fetchall()


def submit_report(conn: sqlite3.Connection, tournament_id: int, match_id: int, team_id: int, winner_team_id: int,
                  reported_by: int, channel_id: int) -> str:
    """Save one side's vote on who won, replacing any earlier vote of theirs. Repeating the
    same vote changes nothing, so it doesn't restart the other side's time to answer. The caller
    checks the match is waiting for a result and the side is in it. Returns what the
    votes now say:
    - 'waiting' while only this side has voted;
    - 'agreed' when both name the same winner, which is recorded;
    - 'skipped' when each names the other, which calls the match off;
    - 'disputed' when each names itself."""
    with conn:
        changed = conn.execute(
            """INSERT INTO result_reports (match_id, team_id, winner_team_id, reported_by, channel_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(match_id, team_id) DO UPDATE SET
                   winner_team_id = excluded.winner_team_id, reported_by = excluded.reported_by,
                   channel_id = excluded.channel_id, reported_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE result_reports.winner_team_id != excluded.winner_team_id""",
            (match_id, team_id, winner_team_id, reported_by, channel_id),
        ).rowcount > 0
        if changed:
            _restart_answer_time(conn, match_id)
        votes = get_reports(conn, match_id)
        if len(votes) < 2:
            return "waiting"
        first, second = votes
        if first["winner_team_id"] == second["winner_team_id"]:
            _record_result(conn, tournament_id, match_id, winner_team_id, reported_by)
            return "agreed"
        if first["winner_team_id"] == second["team_id"]:
            _skip_match(conn, tournament_id, match_id, match_now(conn, tournament_id, match_id).teams, reported_by)
            return "skipped"
        return "disputed"


def take_back_report(conn: sqlite3.Connection, match_id: int, team_id: int) -> bool:
    """Returns False if the side had no vote to take back."""
    with conn:
        _restart_answer_time(conn, match_id)
        taken = conn.execute("DELETE FROM result_reports WHERE match_id = ? AND team_id = ?",
                             (match_id, team_id)).rowcount > 0
        call = get_call(conn, match_id)
        if not get_reports(conn, match_id) and call is not None and call["started_at"] is not None:
            # With no votes left, a called match's post goes back to the match being played.
            # Recording that as already sent ('start', match_posts' ping for it) makes going
            # back an edit, and a later claim's warning a new notification.
            conn.execute("UPDATE match_posts SET pinged = 'start' WHERE match_id = ? AND pinged LIKE 'warning:%'",
                         (match_id,))
        return taken


def _restart_answer_time(conn: sqlite3.Connection, match_id: int) -> None:
    conn.execute(
        """UPDATE match_posts SET warned_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
           WHERE match_id = ? AND warned_at IS NOT NULL""", (match_id,))


def get_open_reports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every vote on a match in a tournament that's in progress, with its tournament_id."""
    return conn.execute(
        """SELECT r.*, m.tournament_id FROM result_reports r
           JOIN matches m ON m.id = r.match_id JOIN tournaments t ON t.id = m.tournament_id
           WHERE t.status = 'in_progress' ORDER BY r.match_id, r.reported_at"""
    ).fetchall()


def give_reported_win(conn: sqlite3.Connection, tournament_id: int, vote: sqlite3.Row) -> bool:
    """Record the win `vote` names, because the other side didn't answer it in time.
    False, with nothing recorded, if anything about the votes has changed since `vote`
    was read, so a late answer is never overruled."""
    with conn:
        votes = get_reports(conn, vote["match_id"])
        unchanged = len(votes) == 1 and all(
            votes[0][key] == vote[key] for key in ("team_id", "winner_team_id", "reported_at"))
        if unchanged:
            _record_result(conn, tournament_id, vote["match_id"], vote["winner_team_id"], vote["reported_by"])
        return unchanged


def get_match_post(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT p.*, m.tournament_id FROM match_posts p JOIN matches m ON m.id = p.match_id
           WHERE p.match_id = ?""", (match_id,)
    ).fetchone()


def get_match_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, m.tournament_id FROM match_posts p JOIN matches m ON m.id = p.match_id"
    ).fetchall()


def claim_match_post(conn: sqlite3.Connection, match_id: int, channel_id: int, teams: tuple[int, int]) -> bool:
    """Reserve the match's one public post, which is sent next time it's brought up to
    date. False if it already has one."""
    with conn:
        return conn.execute(
            "INSERT OR IGNORE INTO match_posts (match_id, channel_id, team_a_id, team_b_id) VALUES (?, ?, ?, ?)",
            (match_id, channel_id, *teams),
        ).rowcount > 0


def set_match_post(conn: sqlite3.Connection, match_id: int, message_id: int, shown: str, pinged: str | None) -> bool:
    """Record what the post's message `message_id` now shows after an edit. False, with
    nothing changed, if the post has moved on to another message, or was forgotten."""
    with conn:
        return conn.execute("UPDATE match_posts SET shown = ?, pinged = ? WHERE match_id = ? AND message_id = ?",
                            (shown, pinged, match_id, message_id)).rowcount > 0


def claim_post_send(conn: sqlite3.Connection, match_id: int) -> str | None:
    """Claim the sending of a new message for a match's post, returning the claim to pass
    to take_over_match_post. None if something else is sending one. A claim older than a
    minute is taken over, since whatever made it must have stopped partway (the bot
    restarting mid-send)."""
    with conn:
        claim = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
        taken = conn.execute(
            """UPDATE match_posts SET sending_at = ?
               WHERE match_id = ? AND (sending_at IS NULL OR sending_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 minute'))""",
            (claim, match_id),
        ).rowcount > 0
        return claim if taken else None


def take_over_match_post(conn: sqlite3.Connection, match_id: int, claim: str, message_id: int, shown: str,
                         pinged: str | None) -> bool:
    """Make the newly sent `message_id` the post's message, ending the claim to send it.
    False, with nothing changed, if the claim no longer holds: an announcement took the
    post over, a new call claimed it afresh, or the claim was given up as stuck."""
    with conn:
        return conn.execute(
            """UPDATE match_posts SET message_id = ?, shown = ?, pinged = ?, sending_at = NULL
               WHERE match_id = ? AND sending_at = ?""", (message_id, shown, pinged, match_id, claim)
        ).rowcount > 0


def forget_match_post(conn: sqlite3.Connection, match_id: int, message_id: int | None) -> None:
    """Forget a post whose message `message_id` shows how its match ended, unless a newer
    post has taken its place."""
    with conn:
        conn.execute("DELETE FROM match_posts WHERE match_id = ? AND message_id IS ?", (match_id, message_id))


def release_post_send(conn: sqlite3.Connection, match_id: int) -> None:
    with conn:
        conn.execute("UPDATE match_posts SET sending_at = NULL WHERE match_id = ?", (match_id,))


def mark_post_unsent(conn: sqlite3.Connection, match_id: int, message_id: int) -> None:
    """Note that the post's message is gone, so it's sent again, unless a newer message
    has already taken its place."""
    with conn:
        conn.execute("UPDATE match_posts SET message_id = NULL, shown = NULL WHERE match_id = ? AND message_id = ?",
                     (match_id, message_id))


def warn_on_match_post(conn: sqlite3.Connection, match_id: int) -> None:
    """Start the time the other side has to answer a vote, from now."""
    with conn:
        conn.execute("UPDATE match_posts SET warned_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE match_id = ?",
                     (match_id,))


def delete_match_post(conn: sqlite3.Connection, match_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM match_posts WHERE match_id = ?", (match_id,))


# -- match coordination ------------------------------------------------------------

def get_match_room_category(conn: sqlite3.Connection, guild_id: int) -> int | None:
    row = conn.execute("SELECT match_room_category_id FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    return row["match_room_category_id"] if row is not None else None


def set_match_room_category(conn: sqlite3.Connection, guild_id: int, category_id: int | None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO guild_settings (guild_id, match_room_category_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET match_room_category_id = excluded.match_room_category_id""",
            (guild_id, category_id),
        )


def set_tournament_channel_if_missing(conn: sqlite3.Connection, tournament_id: int, channel_id: int) -> None:
    """Give a tournament made from a CSV, which has no sign-ups channel, somewhere for the bot to post."""
    with conn:
        conn.execute("UPDATE tournaments SET channel_id = ? WHERE id = ? AND channel_id IS NULL", (channel_id, tournament_id))


def set_match_slots(conn: sqlite3.Connection, tournament_id: int, match_slots: int | None) -> None:
    """With None, the bot stops calling the tournament's matches, so they leave the queue
    rather than keep their place for later."""
    with conn:
        conn.execute("UPDATE tournaments SET match_slots = ? WHERE id = ?", (match_slots, tournament_id))
        if match_slots is None:
            conn.execute("DELETE FROM match_queue WHERE match_id IN (SELECT id FROM matches WHERE tournament_id = ?)",
                         (tournament_id,))


def get_tournaments_to_tend(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """In-progress tournaments whose matches the bot calls, or that still have calls from
    before it stopped, which carry on until their matches are decided."""
    return conn.execute(
        """SELECT * FROM tournaments t WHERE t.status = 'in_progress' AND (t.match_slots IS NOT NULL OR EXISTS
               (SELECT 1 FROM match_calls c JOIN matches m ON m.id = c.match_id WHERE m.tournament_id = t.id))
           ORDER BY t.id"""
    ).fetchall()


def update_match_queue(conn: sqlite3.Connection, tournament_id: int, ready: list[int]) -> None:
    """Queue the tournament's matches in `ready` that aren't queued yet, in that order and
    behind every match already queued in the server, and drop its matches no longer ready."""
    with conn:
        queued = {row["match_id"] for row in conn.execute(
            """SELECT q.match_id FROM match_queue q JOIN matches m ON m.id = q.match_id
               WHERE m.tournament_id = ?""", (tournament_id,))}
        conn.executemany("DELETE FROM match_queue WHERE match_id = ?", [(m,) for m in queued - set(ready)])
        conn.executemany("INSERT OR IGNORE INTO match_queue (match_id) VALUES (?)", [(m,) for m in ready])


def get_match_queue(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """The server's queued matches in coordinated tournaments, longest waiting first, with
    their tournament_id."""
    return conn.execute(
        """SELECT q.match_id, m.tournament_id FROM match_queue q JOIN matches m ON m.id = q.match_id
           JOIN tournaments t ON t.id = m.tournament_id
           WHERE t.guild_id = ? AND t.status = 'in_progress' AND t.match_slots IS NOT NULL
           ORDER BY q.position""", (guild_id,)
    ).fetchall()


def count_server_calls(conn: sqlite3.Connection, guild_id: int) -> int:
    """Called matches across all of a server's in-progress tournaments, which share its
    rooms. A tournament that stopped being played doesn't hold rooms with its calls."""
    return conn.execute(
        """SELECT COUNT(*) FROM match_calls c JOIN matches m ON m.id = c.match_id
           JOIN tournaments t ON t.id = m.tournament_id WHERE t.guild_id = ? AND t.status = 'in_progress'""",
        (guild_id,)
    ).fetchone()[0]


def get_calls(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.* FROM match_calls c JOIN matches m ON m.id = c.match_id
           WHERE m.tournament_id = ? ORDER BY c.called_at""", (tournament_id,)
    ).fetchall()


def get_match_tournament_id(conn: sqlite3.Connection, match_id: int) -> int | None:
    row = conn.execute("SELECT tournament_id FROM matches WHERE id = ?", (match_id,)).fetchone()
    return row["tournament_id"] if row is not None else None


def get_call(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM match_calls WHERE match_id = ?", (match_id,)).fetchone()


def call_match(conn: sqlite3.Connection, match_id: int, teams: tuple[int, int]) -> bool:
    """Call a match, taking it out of the queue. False if it was already called."""
    with conn:
        conn.execute("DELETE FROM match_queue WHERE match_id = ?", (match_id,))
        return conn.execute("INSERT OR IGNORE INTO match_calls (match_id, team_a_id, team_b_id) VALUES (?, ?, ?)",
                            (match_id, *teams)).rowcount > 0


def get_ready_teams(conn: sqlite3.Connection, match_id: int) -> set[int]:
    return {r["team_id"] for r in conn.execute("SELECT team_id FROM match_ready WHERE match_id = ?", (match_id,))}


def ready_up(conn: sqlite3.Connection, match_id: int, team_id: int, user_id: int) -> bool:
    """Mark a side of a called match ready. False if it already was, or the call is gone."""
    with conn:
        if get_call(conn, match_id) is None:
            return False
        return conn.execute("INSERT OR IGNORE INTO match_ready (match_id, team_id, ready_by) VALUES (?, ?, ?)",
                            (match_id, team_id, user_id)).rowcount > 0


def start_match(conn: sqlite3.Connection, match_id: int) -> bool:
    """Mark a called match as started. False if it already was, or the call is gone."""
    with conn:
        return conn.execute(
            """UPDATE match_calls SET started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE match_id = ? AND started_at IS NULL""", (match_id,)
        ).rowcount > 0


def held_rooms(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Rooms in use by a called match in any of the server's in-progress tournaments."""
    return {r["room_id"] for r in conn.execute(
        """SELECT c.room_id FROM match_calls c JOIN matches m ON m.id = c.match_id
           JOIN tournaments t ON t.id = m.tournament_id
           WHERE t.guild_id = ? AND t.status = 'in_progress' AND c.room_id IS NOT NULL""",
        (guild_id,))}


def give_room(conn: sqlite3.Connection, guild_id: int, match_id: int, rooms: list[int]) -> int | None:
    """Give a started match the first of `rooms` no other match holds, and return it, or
    None if every room is taken."""
    with conn:
        taken = held_rooms(conn, guild_id)
        room = next((room for room in rooms if room not in taken), None)
        if room is not None:
            conn.execute("UPDATE match_calls SET room_id = ? WHERE match_id = ? AND room_id IS NULL", (room, match_id))
        return room


def mark_reminded(conn: sqlite3.Connection, match_id: int) -> None:
    with conn:
        conn.execute("UPDATE match_calls SET reminded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE match_id = ?",
                     (match_id,))


def forfeit_match(conn: sqlite3.Connection, tournament_id: int, call: sqlite3.Row) -> bool:
    """Settle a called match nobody fully readied up for: the side that readied wins by
    walkover, or with neither ready, the match is called off. False, with nothing changed,
    if the call has changed since `call` was read, such as someone readying up meanwhile."""
    with conn:
        now = get_call(conn, call["match_id"])
        ready = get_ready_teams(conn, call["match_id"])
        if now is None or now["started_at"] is not None or now["called_at"] != call["called_at"] or len(ready) == 2:
            return False
        teams = match_now(conn, tournament_id, call["match_id"]).teams
        if len(ready) == 1:
            _record_result(conn, tournament_id, call["match_id"], ready.pop(), 0, walkover=True)
        else:
            _skip_match(conn, tournament_id, call["match_id"], teams, 0)
        return True


def replace_match_post(conn: sqlite3.Connection, match_id: int) -> None:
    """Forget a match's post because a new message took its place, keeping its message
    to be deleted from Discord later."""
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO replaced_match_posts (message_id, channel_id)
               SELECT message_id, channel_id FROM match_posts WHERE match_id = ? AND message_id IS NOT NULL""",
            (match_id,),
        )
        conn.execute("DELETE FROM match_posts WHERE match_id = ?", (match_id,))


def keep_replaced_match_post(conn: sqlite3.Connection, channel_id: int, message_id: int) -> None:
    """Keep a replaced match post's message to be deleted from Discord later."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO replaced_match_posts (message_id, channel_id) VALUES (?, ?)",
                     (message_id, channel_id))


def get_replaced_match_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM replaced_match_posts").fetchall()


def forget_replaced_match_post(conn: sqlite3.Connection, message_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM replaced_match_posts WHERE message_id = ?", (message_id,))


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


# -- live posts -------------------------------------------------------------------

def set_live_post(conn: sqlite3.Connection, tournament_id: int, kind: str, channel_id: int, message_id: int,
                  shown: str) -> None:
    conn.execute(
        """INSERT INTO live_posts (tournament_id, kind, channel_id, message_id, shown) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(tournament_id, kind) DO UPDATE SET
               channel_id = excluded.channel_id, message_id = excluded.message_id, shown = excluded.shown""",
        (tournament_id, kind, channel_id, message_id, shown),
    )
    conn.commit()


def set_live_post_shown(conn: sqlite3.Connection, tournament_id: int, kind: str, message_id: int, shown: str) -> None:
    """Record what a live post now shows, unless a newer post has replaced it meanwhile."""
    conn.execute(
        "UPDATE live_posts SET shown = ? WHERE tournament_id = ? AND kind = ? AND message_id = ?",
        (shown, tournament_id, kind, message_id),
    )
    conn.commit()


def get_live_post(conn: sqlite3.Connection, tournament_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM live_posts WHERE tournament_id = ? AND kind = ?", (tournament_id, kind)
    ).fetchone()


def get_live_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM live_posts").fetchall()


def delete_live_post(conn: sqlite3.Connection, tournament_id: int, kind: str) -> None:
    conn.execute("DELETE FROM live_posts WHERE tournament_id = ? AND kind = ?", (tournament_id, kind))
    conn.commit()


# -- consent --------------------------------------------------------------------

def has_agreed(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM consents WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone() is not None


def record_agreement(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO consents (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
    conn.commit()


def opt_out(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    """Forget a member's agreement and unlink every entry of theirs in the server, past
    and current, leaving each under its name. Returns how many entries were unlinked."""
    with conn:
        conn.execute("DELETE FROM consents WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return conn.execute(
            """DELETE FROM team_members WHERE user_id = ?
               AND tournament_id IN (SELECT id FROM tournaments WHERE guild_id = ?)""",
            (user_id, guild_id),
        ).rowcount


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
