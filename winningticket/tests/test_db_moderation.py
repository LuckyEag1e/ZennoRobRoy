import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from labels import FEEDBACK_PAGE_SIZE, feedback_page


@pytest.fixture
def conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def test_bans_are_per_server_and_can_be_lifted(conn):
    db.ban_user(conn, 1, 42, 7, "spam")
    assert db.is_banned(conn, 1, 42)
    assert not db.is_banned(conn, 2, 42)
    assert db.unban_user(conn, 1, 42)
    assert not db.is_banned(conn, 1, 42)
    assert not db.unban_user(conn, 1, 42)


def test_banning_again_updates_the_reason(conn):
    db.ban_user(conn, 1, 42, 7, "spam")
    db.ban_user(conn, 1, 42, 8, None)
    [ban] = db.get_bans(conn, 1)
    assert (ban["reason"], ban["banned_by"]) == (None, 8)


def test_feedback_keeps_no_sender_and_only_the_date(conn):
    db.add_feedback(conn, 1, "More rounds please")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(feedback)")}
    assert columns == {"id", "guild_id", "message", "created_on"}
    [entry] = db.get_feedback(conn, 1)
    assert len(entry["created_on"]) == len("2026-01-01")


def test_feedback_is_newest_first_and_deleted_per_server(conn):
    for message in ("first", "second"):
        db.add_feedback(conn, 1, message)
    db.add_feedback(conn, 2, "elsewhere")
    first, second = reversed(db.get_feedback(conn, 1))
    assert [e["message"] for e in db.get_feedback(conn, 1)] == ["second", "first"]
    assert not db.delete_feedback(conn, 2, first["id"])
    assert db.delete_feedback(conn, 1, first["id"])
    assert [e["message"] for e in db.get_feedback(conn, 1)] == ["second"]


def test_feedback_pages():
    entries = list(range(FEEDBACK_PAGE_SIZE * 2 + 1))
    assert feedback_page(entries, 1) == (entries[:FEEDBACK_PAGE_SIZE], 1, 3)
    assert feedback_page(entries, 3) == (entries[-1:], 3, 3)
    assert feedback_page(entries, 99)[1] == 3
    assert feedback_page([], 1) == ([], 1, 1)
