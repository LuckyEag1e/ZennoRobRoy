"""Asking members before their entries, results and rating are tied to their account, run
through the real cogs."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from helpers import sign_up
from sim.harness import GUILD_ID, Simulation


@pytest.fixture
def world(tmp_path):
    """An organizer, a member who hasn't been asked yet, and two tournaments taking sign-ups."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        member = sim.add_user(10, "Ace")
        for name in ("Cup", "Shield"):
            await sim.run(organizer, "registration open", name=name)
        return sim, organizer, member

    sim, *rest = asyncio.run(build())
    yield (sim, *rest)
    asyncio.run(sim.close())


def _entry(name: str, tournament: str = "Cup"):
    """(entrant name, linked user id or None) for the entrant called `name`, or None."""
    conn = db.get_connection()
    try:
        tournament_row = db.get_tournament_by_name(conn, GUILD_ID, tournament)
        rows = [e for e in db.get_entrants(conn, tournament_row["id"]) if e["name"] == name]
        return (rows[0]["name"], rows[0]["user_id"]) if rows else None
    finally:
        conn.close()


def _agreed(user_id: int) -> bool:
    conn = db.get_connection()
    try:
        return db.has_agreed(conn, GUILD_ID, user_id)
    finally:
        conn.close()


def test_first_sign_up_asks_privately_before_entering_anyone(world):
    sim, _, member = world
    notice = asyncio.run(sim.run(member, "register", tournament="Cup")).sent
    assert not notice.public
    assert [item.label for item in notice.view.children] == ["Agree", "Opt Out"]
    assert "/optout" in notice.content
    assert _entry("Ace") is None  # nothing is saved until they choose


def test_agreeing_links_the_entry_and_is_remembered(world):
    sim, _, member = world
    reply = asyncio.run(sign_up(sim, member, "Cup", agree=True))
    assert "signed up" in reply.content and "/optout" in reply.content
    assert _entry("Ace") == ("Ace", member.id) and _agreed(member.id)
    second = asyncio.run(sim.run(member, "register", tournament="Shield")).sent
    assert second.view is None and "signed up" in second.content  # not asked again
    assert _entry("Ace", "Shield") == ("Ace", member.id)


def test_opting_out_enters_by_name_only_and_isnt_remembered(world):
    sim, _, member = world
    reply = asyncio.run(sign_up(sim, member, "Cup", agree=False))
    assert _entry("Ace") == ("Ace", None) and not _agreed(member.id)
    assert "/predict" in reply.content and "history" in reply.content  # picks are still tied to them
    again = asyncio.run(sim.run(member, "register", tournament="Shield")).sent
    assert [item.label for item in again.view.children] == ["Agree", "Opt Out"]
    twice = asyncio.run(sign_up(sim, member, "Cup", agree=False))
    assert "already signed up" in twice.content


def test_opting_out_unlinks_entries_from_before_anyone_was_asked(world):
    sim, _, member = world
    conn = db.get_connection()
    old = db.get_tournament_by_name(conn, GUILD_ID, "Shield")["id"]
    db.register_entrant(conn, old, "Ace", member.id)  # linked before consent existed
    conn.close()
    asyncio.run(sign_up(sim, member, "Cup", agree=False))
    assert _entry("Ace", "Shield") == ("Ace", None)


def test_optout_needs_the_confirmation_typed_then_unlinks_everything(world):
    sim, _, member = world

    async def agree_then_leave():
        await sign_up(sim, member, "Cup", agree=True)
        await sim.run(member, "register", tournament="Shield")
        wrong = (await sim.run(member, "optout", confirm="yes")).sent
        linked_after_wrong = _entry("Ace") == ("Ace", member.id)
        right = (await sim.run(member, "optout", confirm="Opt out")).sent
        return wrong, linked_after_wrong, right

    wrong, linked_after_wrong, right = asyncio.run(agree_then_leave())
    assert "Nothing changed" in wrong.content and linked_after_wrong
    assert not right.public and "2 of your entries" in right.content
    assert _entry("Ace") == ("Ace", None) and _entry("Ace", "Shield") == ("Ace", None)
    assert not _agreed(member.id)


def test_an_organizer_can_only_link_a_member_who_agreed(world):
    sim, organizer, member = world
    reply = asyncio.run(sim.run(organizer, "registration add", tournament="Cup", user=member)).sent
    assert "by name only" in reply.content and _entry("Ace") == ("Ace", None)
    asyncio.run(sign_up(sim, member, "Shield", agree=True))
    asyncio.run(sim.run(organizer, "registration add", tournament="Cup", user=member, name="Ace again"))
    assert _entry("Ace again") == ("Ace again", member.id)


def test_agreeing_takes_over_an_entry_an_organizer_added_by_name(world):
    sim, organizer, member = world
    asyncio.run(sim.run(organizer, "registration add", tournament="Cup", user=member))
    asyncio.run(sign_up(sim, member, "Cup", agree=True))
    assert _entry("Ace") == ("Ace", member.id) and _entry("Ace (2)") is None


def test_an_organizer_cant_add_a_member_by_name_twice(world):
    sim, organizer, member = world
    asyncio.run(sim.run(organizer, "registration add", tournament="Cup", user=member))
    again = asyncio.run(sim.run(organizer, "registration add", tournament="Cup", user=member)).sent
    assert "already has an entrant named 'Ace'" in again.content and _entry("Ace (2)") is None


def test_an_organizer_can_separate_a_member_who_took_over_an_entry(world):
    sim, organizer, member = world

    async def take_over_then_separate():
        await sim.run(organizer, "registration add", tournament="Cup", user=None, name="Ace")
        await sign_up(sim, member, "Cup", agree=True)
        took_over = _entry("Ace")
        reply = (await sim.run(organizer, "admin unlinkplayer", tournament="Cup", entrant="Ace")).sent
        again = (await sim.run(member, "register", tournament="Cup")).sent
        return took_over, reply, again

    took_over, reply, again = asyncio.run(take_over_then_separate())
    assert took_over == ("Ace", member.id)
    assert not reply.public and "Ace (2)" in reply.content
    assert _entry("Ace") == ("Ace", None) and _entry("Ace (2)") == ("Ace (2)", member.id)
    assert _agreed(member.id) and "already signed up" in again.content
    assert not sim.issues


def test_a_notice_answered_after_sign_ups_close_enters_no_one(world):
    sim, organizer, member = world

    async def answer_late():
        notice = (await sim.run(member, "register", tournament="Cup")).sent
        await sim.run(organizer, "registration add", tournament="Cup", user=None, name="Guest A")
        await sim.run(organizer, "registration add", tournament="Cup", user=None, name="Guest B")
        await sim.run(organizer, "registration close", name="Cup")
        await sim.click(member, notice, "Agree")
        return notice

    notice = asyncio.run(answer_late())
    assert "closed" in notice.content and not _agreed(member.id)
    assert not sim.issues


def test_a_banned_member_can_still_opt_out(world):
    sim, organizer, member = world

    async def ban_then_opt_out():
        await sign_up(sim, member, "Cup", agree=True)
        await sim.run(organizer, "admin ban", user=member)
        blocked = (await sim.run(member, "register", tournament="Shield")).sent
        return blocked, (await sim.run(member, "optout", confirm="opt out")).sent

    blocked, opted_out = asyncio.run(ban_then_opt_out())
    assert "blocked" in blocked.content
    assert "no longer linked" in opted_out.content and _entry("Ace") == ("Ace", None)
