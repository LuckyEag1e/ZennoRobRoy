"""/rating, seeding when sign-ups close, and the admin commands for concluded tournaments,
run through the real cogs."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from sim.discord_fakes import FakeAttachment
from sim.harness import GUILD_ID, Simulation


@pytest.fixture
def world(tmp_path):
    """An organizer, two players, and a concluded CSV tournament ("Old Cup") in which
    "Ace" beat "Blaze", neither of them linked to a member yet."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        ace = sim.add_user(10, "Ace")
        blaze = sim.add_user(11, "Blaze")
        await sim.run(organizer, "tournament create", name="Old Cup", csv_file=FakeAttachment(b"team_a,team_b\nAce,Blaze\n"))
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Old Cup")
        [final] = db.get_matches(conn, tournament["id"]).values()
        db.record_result(conn, tournament["id"], final.id, final.team_a_id, organizer.id)
        db.set_tournament_status(conn, tournament["id"], "concluded")
        conn.close()
        return sim, organizer, ace, blaze, str(tournament["id"])

    sim, *rest = asyncio.run(build())
    yield (sim, *rest)
    asyncio.run(sim.close())


def _rating_reply(sim, asker, **options) -> str:
    reply = asyncio.run(sim.run(asker, "rating", **options)).sent
    assert not reply.public
    return reply.embed.description


def test_linking_an_old_team_shows_up_in_rating(world):
    sim, organizer, ace, blaze, old_cup = world
    assert "provisional" in _rating_reply(sim, ace).split("\n")[0]

    async def link():
        [picked] = await sim.autocomplete(organizer, "admin linkteam", "tournament", "old")
        teams = await sim.autocomplete(organizer, "admin linkteam", "team", "", tournament=picked)
        await sim.run(organizer, "admin linkteam", tournament=picked, team="Ace", user=ace)
        only_one_side = (await sim.run(ace, "rating")).sent.embed.description
        await sim.run(organizer, "admin linkteam", tournament=picked, team="Blaze", user=blaze)
        again = await sim.run(organizer, "admin linkteam", tournament=picked, team="Blaze", user=ace)
        return picked, sorted(teams), only_one_side, again.sent

    picked, teams, only_one_side, again = asyncio.run(link())
    assert picked == old_cup and teams == ["Ace", "Blaze"]
    assert "provisional" in only_one_side  # a game counts once both sides are linked
    assert "already on a team" in again.content
    assert _rating_reply(sim, blaze, user=ace).split("\n")[0] == "Singles: **1516**"
    assert not sim.issues


def test_a_typed_name_isnt_taken_for_a_concluded_tournament(world):
    sim, organizer, ace, _, _ = world
    reply = asyncio.run(sim.run(organizer, "admin linkteam", tournament="Old Cup", team="Ace", user=ace)).sent
    assert "Pick the tournament from the list" in reply.content


def test_removehistory_needs_the_name_retyped(world):
    sim, organizer, ace, blaze, old_cup = world

    async def remove():
        await sim.run(organizer, "admin linkteam", tournament=old_cup, team="Ace", user=ace)
        await sim.run(organizer, "admin linkteam", tournament=old_cup, team="Blaze", user=blaze)
        wrong = await sim.run(organizer, "admin removehistory", tournament=old_cup, confirm_name="Old")
        rated = (await sim.run(ace, "rating")).sent.embed.description
        right = await sim.run(organizer, "admin removehistory", tournament=old_cup, confirm_name="Old Cup")
        return wrong.sent, rated, right.sent

    wrong, rated, right = asyncio.run(remove())
    assert "didn't match" in wrong.content and "provisional" not in rated.split("\n")[0]
    assert "Deleted" in right.content
    assert "provisional" in _rating_reply(sim, ace).split("\n")[0]
    assert asyncio.run(sim.autocomplete(organizer, "admin removehistory", "tournament", "")) == []


def test_closing_seeds_by_rating_unless_overridden(world):
    sim, organizer, ace, blaze, old_cup = world

    async def two_signups():
        await sim.run(organizer, "admin linkteam", tournament=old_cup, team="Ace", user=ace)
        await sim.run(organizer, "admin linkteam", tournament=old_cup, team="Blaze", user=blaze)
        posts = []
        for name, seeding in (("Seeded", None), ("Drawn", "random")):
            await sim.run(organizer, "registration open", name=name)
            for player in (ace, blaze):
                await sim.run(player, "register", tournament=name)
            await sim.run(organizer, "registration add", tournament=name, user=None, name="Guest")
            posts.append((await sim.run(organizer, "registration close", name=name, seeding=seeding)).sent)
        return posts

    seeded, drawn = asyncio.run(two_signups())
    assert seeded.public and "seeded into the bracket by rating" in seeded.content
    assert "drawn into the bracket at random" in drawn.content
    conn = db.get_connection()
    seeds = {t["name"]: t["seed"] for t in db.get_teams(conn, db.get_tournament_by_name(conn, GUILD_ID, "Seeded")["id"]).values()}
    unseeded = [t["seed"] for t in db.get_teams(conn, db.get_tournament_by_name(conn, GUILD_ID, "Drawn")["id"]).values()]
    conn.close()
    assert seeds["Blaze"] == 3  # lost the old final, so below Ace and the unrated guest, who count as average
    assert unseeded == [None, None, None]
    assert not sim.issues
