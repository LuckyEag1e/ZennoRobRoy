"""SQLite storage for banners, who joined them, and the pulls they reported. No Discord code here."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

DB_PATH = Path(__file__).parent / "data" / "fuku.db"

# A reported pull waits for an approver, who either approves it (it counts) or rejects it.
PULL_STATUSES = ("pending", "approved", "rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS banners (
    id            INTEGER PRIMARY KEY,
    guild_id      INTEGER NOT NULL,
    name          TEXT    NOT NULL COLLATE NOCASE,
    featured      TEXT    NOT NULL,
    featured_rate REAL    NOT NULL,  -- percent, e.g. 0.75 means 0.75%
    ends_on       TEXT    NOT NULL,  -- YYYY-MM-DD; open through the end of that day (UTC)
    created_by    INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS entrants (
    banner_id INTEGER NOT NULL REFERENCES banners (id),
    user_id   INTEGER NOT NULL,
    joined_at TEXT    NOT NULL,
    PRIMARY KEY (banner_id, user_id)
);
CREATE TABLE IF NOT EXISTS pulls (
    id          INTEGER PRIMARY KEY,
    banner_id   INTEGER NOT NULL REFERENCES banners (id),
    user_id     INTEGER NOT NULL,
    pulls       INTEGER NOT NULL CHECK (pulls > 0),
    featured    INTEGER NOT NULL CHECK (featured BETWEEN 0 AND pulls),
    -- SSRs that weren't the featured one. Every pull is one card, so featured + off_banner <= pulls.
    off_banner  INTEGER NOT NULL DEFAULT 0 CHECK (off_banner >= 0 AND featured + off_banner <= pulls),
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reported_at TEXT    NOT NULL,
    decided_by  INTEGER,
    decided_at  TEXT
);
"""


class BannerExists(Exception):
    """This server already has a banner with that name."""


class AlreadyDecided(Exception):
    """The pull was already approved or rejected."""


@dataclass(frozen=True)
class Banner:
    id: int
    guild_id: int
    name: str
    featured: str
    featured_rate: float
    ends_on: date
    created_by: int


@dataclass(frozen=True)
class Pull:
    id: int
    banner_id: int
    user_id: int
    pulls: int
    featured: int
    off_banner: int
    status: str
    decided_by: Optional[int]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _banner(row: sqlite3.Row) -> Banner:
    return Banner(row["id"], row["guild_id"], row["name"], row["featured"], row["featured_rate"],
                  date.fromisoformat(row["ends_on"]), row["created_by"])


class Store:
    def __init__(self, path: Union[str, Path] = DB_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._upgrade()

    def _upgrade(self) -> None:
        """Brings a database made by an older version of Fuku up to the current schema."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(pulls)")}
        if "off_banner" not in columns:
            # Reports from before this column didn't say, so they count as none.
            with self._conn:
                self._conn.execute("ALTER TABLE pulls ADD COLUMN off_banner INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._conn.close()

    # Banners

    def create_banner(self, guild_id: int, name: str, *, featured: str, featured_rate: float, ends_on: date,
                      created_by: int) -> Banner:
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO banners (guild_id, name, featured, featured_rate, ends_on, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, name, featured, featured_rate, ends_on.isoformat(), created_by, _now()))
        except sqlite3.IntegrityError as error:
            raise BannerExists(name) from error
        return Banner(cursor.lastrowid, guild_id, name, featured, featured_rate, ends_on, created_by)

    def open_banners(self, guild_id: int, today: date) -> list[Banner]:
        rows = self._conn.execute("SELECT * FROM banners WHERE guild_id = ? AND ends_on >= ? ORDER BY ends_on, id",
                                  (guild_id, today.isoformat()))
        return [_banner(row) for row in rows]

    def find_open_banner(self, guild_id: int, name: str, today: date) -> Optional[Banner]:
        row = self._conn.execute("SELECT * FROM banners WHERE guild_id = ? AND name = ? AND ends_on >= ?",
                                 (guild_id, name, today.isoformat())).fetchone()
        return _banner(row) if row else None

    def get_banner(self, banner_id: int) -> Optional[Banner]:
        row = self._conn.execute("SELECT * FROM banners WHERE id = ?", (banner_id,)).fetchone()
        return _banner(row) if row else None

    # Entrants

    def join(self, banner_id: int, user_id: int) -> bool:
        """Adds the user to the banner. False if they had already joined."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO entrants (banner_id, user_id, joined_at) VALUES (?, ?, ?)",
                (banner_id, user_id, _now()))
        return cursor.rowcount == 1

    def has_joined(self, banner_id: int, user_id: int) -> bool:
        return self._conn.execute("SELECT 1 FROM entrants WHERE banner_id = ? AND user_id = ?",
                                  (banner_id, user_id)).fetchone() is not None

    # Pulls

    def record_pull(self, banner_id: int, user_id: int, *, pulls: int, featured: int, off_banner: int) -> int:
        """Saves a reported batch of pulls as pending and returns its id."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO pulls (banner_id, user_id, pulls, featured, off_banner, reported_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (banner_id, user_id, pulls, featured, off_banner, _now()))
        return cursor.lastrowid

    def get_pull(self, pull_id: int) -> Optional[Pull]:
        row = self._conn.execute("SELECT * FROM pulls WHERE id = ?", (pull_id,)).fetchone()
        if row is None:
            return None
        return Pull(row["id"], row["banner_id"], row["user_id"], row["pulls"], row["featured"], row["off_banner"],
                    row["status"],
                    row["decided_by"])

    def decide(self, pull_id: int, *, approver_id: int, approved: bool) -> Pull:
        # Only a pending pull changes, so two approvers clicking at once can't both decide it.
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE pulls SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                ("approved" if approved else "rejected", approver_id, _now(), pull_id))
        if cursor.rowcount == 0:
            raise AlreadyDecided(pull_id)
        return self.get_pull(pull_id)

    def totals(self, banner_id: int, user_id: int) -> tuple[int, int, int]:
        """(pulls, featured, off_banner) the user has had approved on the banner."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pulls), 0), COALESCE(SUM(featured), 0), COALESCE(SUM(off_banner), 0) FROM pulls"
            " WHERE banner_id = ? AND user_id = ? AND status = 'approved'", (banner_id, user_id)).fetchone()
        return row[0], row[1], row[2]
