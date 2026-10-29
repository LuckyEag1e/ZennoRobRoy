"""/history for concluded tournaments, and keeping their names out of reuse, run through the real cogs."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import resolvable_matches, resolve_slot
from sim.harness import GUILD_ID, Simulation


@pytest.fixture
def world(tmp_path):
    """An organizer, a player, and "Cup": three entrants seeded by rating (slide), played
    out with the first-listed side winning, and concluded."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        player = sim.add_user(10, "Ace")
        await sim.run(organizer, "registration open", name="Cup", seeding="slide")
        await sim.run(player, "register", tournament="Cup")
        for guest in ("Blaze", "Comet"):
            await sim.run(organizer, "registration add", tournament="Cup", user=None, name=guest)
        await sim.run(organizer, "registration close", name="Cup")
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Cup")
        db.set_tournament_status(conn, tournament["id"], "in_progress")
        while True:
            matches_by_id, feeders, _ = db.load_bracket(conn, tournament["id"])
            results = db.get_results(conn, tournament["id"])
            ready = resolvable_matches(matches_by_id, feeders, results.get, set(results))
            if not ready:
                break
            team_a, _ = resolve_slot(ready[0], matches_by_id, results.get, feeders)
            db.record_result(conn, tournament["id"], ready[0].id, team_a, organizer.id)
        conn.close()
        await sim.run(organizer, "tournament conclude", name="Cup")
        return sim, organizer, player

    sim, *rest = asyncio.run(build())
    yield (sim, *rest)
    asyncio.run(sim.close())


def test_history_replies_privately_with_bracket_seeding_and_leaderboard(world):
    sim, _, player = world

    async def look_back():
        return [(await sim.run(player, f"history {kind}", tournament="Cup")).sent
                for kind in ("bracket", "seeding", "leaderboard")]

    bracket, seeding, leaderboard = asyncio.run(look_back())
    assert not any(reply.public for reply in (bracket, seeding, leaderboard))
    assert bracket.embed.title == "Bracket: Cup" and "concluded" in bracket.embed.description
    assert seeding.embed.description.splitlines()[0] == (
        "Entrants were seeded into the bracket by rating, with the top seeds playing mid-ranked seeds first.")
    assert len(seeding.embed.description.splitlines()) == 4  # the description, then three seeds
    assert leaderboard.embed.title == "Leaderboard: Cup"
    assert not sim.issues


def test_history_list_sums_up_each_concluded_tournament(world):
    sim, _, player = world
    reply = asyncio.run(sim.run(player, "history list")).sent
    assert not reply.public
    [line] = reply.embed.description.splitlines()
    name, details = line.split(": ", 1)
    champion, entrants, format_name, play, finished = details.split(" · ")
    assert name == "Cup" and champion.startswith("🏆 ")
    assert (entrants, format_name, play) == ("3 entrants", "Single elimination", "Singles")
    assert len(finished) == 10 and finished[4] == "-"  # the day the final was recorded


def test_history_list_says_when_nothing_has_concluded(tmp_path):
    async def ask():
        sim = await Simulation.create(tmp_path / "empty.db")
        try:
            return (await sim.run(sim.add_user(10, "Ace"), "history list")).sent
        finally:
            await sim.close()

    assert asyncio.run(ask()).embed.description == "No tournaments have concluded yet."


def test_only_history_offers_a_concluded_tournament(world):
    sim, organizer, player = world

    async def ask():
        offered_normally = await sim.autocomplete(player, "tournament bracket", "name", "")
        offered_by_history = await sim.autocomplete(player, "history bracket", "tournament", "")
        redirected = (await sim.run(player, "tournament bracket", name="Cup")).sent
        await sim.run(organizer, "registration open", name="Live")
        not_yet = (await sim.run(player, "history bracket", tournament="Live")).sent
        return offered_normally, offered_by_history, redirected, not_yet

    offered_normally, offered_by_history, redirected, not_yet = asyncio.run(ask())
    assert "Cup" not in offered_normally and offered_by_history == ["Cup"]
    assert not redirected.public and "`/history`" in redirected.content
    assert "hasn't concluded yet" in not_yet.content


def test_a_concluded_name_cant_be_used_again(world):
    sim, organizer, _ = world
    reply = asyncio.run(sim.run(organizer, "registration open", name="Cup")).sent
    assert "names aren't reused" in reply.content
    conn = db.get_connection()
    assert db.get_tournament_by_name(conn, GUILD_ID, "Cup") is None
    conn.close()
