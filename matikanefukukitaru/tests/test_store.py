import sqlite3
from datetime import date

import pytest

from store import AlreadyDecided, BannerExists, Store

GUILD = 1
TODAY = date(2026, 11, 27)


@pytest.fixture
def store():
    return Store(":memory:")


def make_banner(store, name="Summer Maruzensky", guild=GUILD, ends_on=date(2026, 12, 10)):
    return store.create_banner(guild, name, featured="Maruzensky (Summer)", featured_rate=0.75,
                               ends_on=ends_on, created_by=99)


def test_a_new_banner_is_open_and_found_by_name_in_any_case(store):
    banner = make_banner(store)
    found = store.find_open_banner(GUILD, "summer MARUZENSKY", TODAY)
    assert found == banner
    assert found.featured == "Maruzensky (Summer)"
    assert found.featured_rate == 0.75
    assert found.ends_on == date(2026, 12, 10)


def test_banner_names_are_unique_per_server_ignoring_case(store):
    make_banner(store)
    with pytest.raises(BannerExists):
        make_banner(store, name="SUMMER maruzensky")
    make_banner(store, guild=GUILD + 1)  # another server can reuse it


def test_a_banner_is_open_through_its_last_day_and_closed_after(store):
    make_banner(store, ends_on=TODAY)
    assert store.find_open_banner(GUILD, "Summer Maruzensky", TODAY) is not None
    assert store.find_open_banner(GUILD, "Summer Maruzensky", date(2026, 11, 28)) is None
    assert store.open_banners(GUILD, date(2026, 11, 28)) == []


def test_open_banners_only_lists_this_servers(store):
    mine = make_banner(store)
    make_banner(store, name="Other", guild=GUILD + 1)
    assert store.open_banners(GUILD, TODAY) == [mine]


def test_joining_twice_only_counts_once(store):
    banner = make_banner(store)
    assert store.join(banner.id, 5) is True
    assert store.join(banner.id, 5) is False
    assert store.has_joined(banner.id, 5)
    assert not store.has_joined(banner.id, 6)


def test_only_approved_pulls_count_towards_a_users_totals(store):
    banner = make_banner(store)
    first = store.record_pull(banner.id, 5, pulls=10, featured=1, off_banner=2)
    second = store.record_pull(banner.id, 5, pulls=10, featured=0, off_banner=1)
    third = store.record_pull(banner.id, 5, pulls=50, featured=2, off_banner=4)
    assert store.get_pull(first).status == "pending"
    assert store.get_pull(first).off_banner == 2
    assert store.totals(banner.id, 5) == (0, 0, 0)

    store.decide(first, approver_id=7, approved=True)
    store.decide(second, approver_id=7, approved=True)
    store.decide(third, approver_id=7, approved=False)
    assert store.totals(banner.id, 5) == (20, 1, 3)
    assert store.get_pull(third).status == "rejected"
    assert store.get_pull(first).decided_by == 7


def test_a_pull_can_only_be_decided_once(store):
    banner = make_banner(store)
    pull = store.record_pull(banner.id, 5, pulls=10, featured=1, off_banner=0)
    store.decide(pull, approver_id=7, approved=False)
    with pytest.raises(AlreadyDecided):
        store.decide(pull, approver_id=8, approved=True)
    assert store.get_pull(pull).status == "rejected"


def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "fuku.db"
    first = Store(path)
    banner = make_banner(first)
    first.join(banner.id, 5)
    pull = first.record_pull(banner.id, 5, pulls=10, featured=1, off_banner=2)
    first.decide(pull, approver_id=7, approved=True)
    first.close()

    again = Store(path)
    assert again.find_open_banner(GUILD, "Summer Maruzensky", TODAY) == banner
    assert again.has_joined(banner.id, 5)
    assert again.totals(banner.id, 5) == (10, 1, 2)


def test_a_database_from_before_off_banner_ssrs_were_counted_still_opens(tmp_path):
    # The first version of Fuku made the pulls table without an off_banner column.
    path = tmp_path / "fuku.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE banners (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
            featured TEXT NOT NULL, featured_rate REAL NOT NULL, ends_on TEXT NOT NULL, created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE (guild_id, name));
        CREATE TABLE pulls (id INTEGER PRIMARY KEY, banner_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            pulls INTEGER NOT NULL, featured INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            reported_at TEXT NOT NULL, decided_by INTEGER, decided_at TEXT);
        INSERT INTO banners VALUES (1, 1, 'Old', 'Someone', 0.75, '2026-12-10', 99, '2026-11-27');
        INSERT INTO pulls VALUES (1, 1, 5, 10, 1, 'approved', '2026-11-27', 7, '2026-11-27');
    """)
    old.close()

    store = Store(path)
    assert store.totals(1, 5) == (10, 1, 0)  # older reports didn't say, so they count as none
    new = store.record_pull(1, 5, pulls=10, featured=0, off_banner=3)
    store.decide(new, approver_id=7, approved=True)
    assert store.totals(1, 5) == (20, 1, 3)
