"""Menus opened before someone else changed the bracket mustn't save outdated picks."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import pytest

import db
from sim.discord_fakes import FakeAttachment
from sim.harness import Simulation

BRACKET = b"team_a,team_b\nAce,Blaze\nComet,Dash\n"


@pytest.fixture
def world(tmp_path):
    """A locked 4-team tournament and two organizers, run through the real cogs."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        first = sim.add_user(1, "First organizer", organizer=True)
        second = sim.add_user(2, "Second organizer", organizer=True)
        await sim.run(first, "tournament create", name="Cup", csv_file=FakeAttachment(BRACKET))
        await sim.run(first, "tournament open", name="Cup")
        await sim.run(first, "tournament lock", name="Cup")
        return sim, first, second

    sim, first, second = asyncio.run(build())
    yield sim, first, second
    asyncio.run(sim.close())


def _results(sim) -> dict[str, str]:
    """Stored results as {match id: the winner's team name}."""
    conn = db.get_connection()
    try:
        teams = {team_id: row["name"] for team_id, row in db.get_teams(conn, 1).items()}
        return {match_id: teams[winner] for match_id, winner in db.get_results(conn, 1).items()}
    finally:
        conn.close()


def _first_option(message) -> str:
    return message.view.children[0].options[0].value


async def _record(sim, organizer, match_value: str, winner: str):
    menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
    await sim.choose(organizer, menu, match_value)
    return await sim.click(organizer, menu, winner)


def test_a_second_result_menu_cant_overwrite_the_first(world):
    sim, first, second = world

    async def play():
        menu_a = (await sim.run(first, "match result", tournament="Cup")).sent
        menu_b = (await sim.run(second, "match result", tournament="Cup")).sent
        match = _first_option(menu_a)
        await sim.choose(first, menu_a, match)
        await sim.click(first, menu_a, "Ace")
        posts_before = len(sim.public_posts_since(0))
        await sim.choose(second, menu_b, match)
        refused = await sim.click(second, menu_b, "Blaze")
        return refused, posts_before

    refused, posts_before = asyncio.run(play())
    assert "already recorded" in refused.message.content
    assert list(_results(sim).values()) == ["Ace"]
    assert len(sim.public_posts_since(0)) == posts_before  # no second, contradictory announcement
    assert not sim.issues


def test_setresult_asks_before_overwriting_a_result_recorded_after_its_menu_opened(world):
    sim, first, second = world

    async def play():
        admin_menu = (await sim.run(second, "admin setresult", tournament="Cup")).sent
        match = _first_option(admin_menu)
        await _record(sim, first, match, "Ace")
        await sim.choose(second, admin_menu, match)
        return await sim.click(second, admin_menu, "Blaze")

    asked = asyncio.run(play())
    assert "Overwrite" in asked.message.content
    assert list(_results(sim).values()) == ["Ace"]


def test_a_changed_matchup_is_refused(world):
    sim, first, second = world

    async def play():
        semi_1 = _first_option((await sim.run(first, "match result", tournament="Cup")).sent)
        await _record(sim, first, semi_1, "Ace")
        semi_2 = _first_option((await sim.run(first, "match result", tournament="Cup")).sent)
        await _record(sim, first, semi_2, "Comet")
        final_menu = (await sim.run(second, "match result", tournament="Cup")).sent  # shows Ace vs Comet
        # Meanwhile the first organizer corrects semi-final 1: Blaze won, not Ace.
        admin_menu = (await sim.run(first, "admin setresult", tournament="Cup")).sent
        await sim.choose(first, admin_menu, semi_1)
        await sim.click(first, admin_menu, "Blaze")
        await sim.click(first, admin_menu, "Confirm")
        await sim.choose(second, final_menu, _first_option(final_menu))
        return await sim.click(second, final_menu, "Ace")

    refused = asyncio.run(play())
    assert "matchup has changed" in refused.message.content
    assert sorted(_results(sim).values()) == ["Blaze", "Comet"]  # the final wasn't recorded


def test_a_deleted_tournament_is_refused_without_crashing(world):
    sim, first, second = world

    async def play():
        menu = (await sim.run(second, "match result", tournament="Cup")).sent
        await sim.choose(second, menu, _first_option(menu))
        await sim.run(first, "admin deletetournament", tournament="Cup", confirm_name="Cup")
        return await sim.click(second, menu, "Ace")

    refused = asyncio.run(play())
    assert "no longer exists" in refused.message.content
    assert not sim.issues


def test_clearing_a_result_twice_says_it_was_already_cleared(world):
    sim, first, second = world

    async def play():
        match = _first_option((await sim.run(first, "match result", tournament="Cup")).sent)
        await _record(sim, first, match, "Ace")
        menus = [(await sim.run(organizer, "admin clearresult", tournament="Cup")).sent for organizer in (first, second)]
        for organizer, menu in zip((first, second), menus):
            await sim.choose(organizer, menu, match)
        await sim.click(first, menus[0], "Confirm")
        return await sim.click(second, menus[1], "Confirm")

    second_clear = asyncio.run(play())
    assert "already cleared" in second_clear.message.content
    assert _results(sim) == {}


def test_clearing_a_result_that_changed_since_the_menu_opened_is_refused(world):
    sim, first, second = world

    async def play():
        match = _first_option((await sim.run(first, "match result", tournament="Cup")).sent)
        await _record(sim, first, match, "Ace")
        clear_menu = (await sim.run(second, "admin clearresult", tournament="Cup")).sent  # shows Ace as the winner
        await sim.choose(second, clear_menu, match)
        # Meanwhile the first organizer corrects it: Blaze won.
        fix_menu = (await sim.run(first, "admin setresult", tournament="Cup")).sent
        await sim.choose(first, fix_menu, match)
        await sim.click(first, fix_menu, "Blaze")
        await sim.click(first, fix_menu, "Confirm")
        return await sim.click(second, clear_menu, "Confirm")

    refused = asyncio.run(play())
    assert "has changed" in refused.message.content
    assert list(_results(sim).values()) == ["Blaze"]


def test_a_concluded_tournament_cant_be_changed_from_an_old_menu(world):
    sim, first, second = world

    async def play():
        for winner in ("Ace", "Comet", "Ace"):  # both semi-finals, then the final
            await _record(sim, first, _first_option((await sim.run(first, "match result", tournament="Cup")).sent), winner)
        old_menu = (await sim.run(second, "admin setresult", tournament="Cup")).sent
        await sim.run(first, "tournament conclude", name="Cup")
        await sim.choose(second, old_menu, _first_option(old_menu))
        return await sim.click(second, old_menu, "Blaze")

    refused = asyncio.run(play())
    assert "has concluded" in refused.message.content
    conn = db.get_connection()
    try:
        assert db.get_tournament(conn, 1)["status"] == "concluded"
    finally:
        conn.close()


def test_setting_the_winner_a_match_already_has_changes_nothing(world):
    sim, first, second = world

    async def play():
        match = _first_option((await sim.run(first, "match result", tournament="Cup")).sent)
        await _record(sim, first, match, "Ace")
        menu = (await sim.run(second, "admin setresult", tournament="Cup")).sent
        await sim.choose(second, menu, match)
        return await sim.click(second, menu, "Ace")

    reply = asyncio.run(play())
    assert "already won this match" in reply.message.content
    assert reply.message.view is None  # no "Overwrite?" confirmation
    assert list(_results(sim).values()) == ["Ace"]


def test_a_recorded_result_is_only_announced_publicly(world):
    sim, first, _ = world

    async def play():
        menu = (await sim.run(first, "match result", tournament="Cup")).sent
        await sim.choose(first, menu, _first_option(menu))
        await sim.click(first, menu, "Ace")
        return menu

    menu = asyncio.run(play())
    assert menu.deleted
    assert "Ace** defeats **Blaze" in sim.public_posts_since(0)[-1].content
    assert not sim.issues


def test_a_result_that_cant_be_announced_is_reported_privately(world, monkeypatch):
    sim, first, _ = world

    async def refuse_to_post(*args, **kwargs):
        raise discord.Forbidden(type("Response", (), {"status": 403, "reason": "Forbidden"})(), "Missing Access")

    async def play():
        menu = (await sim.run(first, "match result", tournament="Cup")).sent
        await sim.choose(first, menu, _first_option(menu))
        monkeypatch.setattr(sim.channel, "send", refuse_to_post)
        await sim.click(first, menu, "Ace")
        return menu

    menu = asyncio.run(play())
    assert not menu.deleted
    assert "couldn't be announced" in menu.content and "Ace** advances" in menu.content
    assert list(_results(sim).values()) == ["Ace"]
