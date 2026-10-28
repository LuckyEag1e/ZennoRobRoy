"""The live bracket and leaderboard posts, and the loop that keeps them current."""

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import pytest

import cogs.tournament as tournament_cog
import db
from sim.discord_fakes import FakeAttachment
from sim.harness import Simulation

BRACKET = b"team_a,team_b\nAce,Blaze\nComet,Dash\n"


@pytest.fixture
def world(tmp_path):
    """A locked tournament with its bracket posted in the channel."""

    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        player = sim.add_user(2, "Player")
        await sim.run(organizer, "tournament create", name="Cup", csv_file=FakeAttachment(BRACKET))
        await sim.run(organizer, "tournament open", name="Cup")
        await sim.run(organizer, "tournament lock", name="Cup")
        posted = (await sim.run(player, "tournament bracket", name="Cup")).sent
        return sim, organizer, player, posted

    sim, organizer, player, posted = asyncio.run(build())
    yield sim, organizer, player, posted
    asyncio.run(sim.close())


def _cog(sim):
    return sim.cogs["TournamentCog"]


async def _refresh(sim):
    cog = _cog(sim)
    await cog.refresh_live_posts.coro(cog)


async def _record_first_result(sim, organizer):
    menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
    await sim.choose(organizer, menu, menu.view.children[0].options[0].value)
    await sim.click(organizer, menu, "Ace")


def _live_posts(sim):
    conn = db.get_connection()
    try:
        return db.get_live_posts(conn)
    finally:
        conn.close()


def _forbidden():
    response = type("FakeHTTPResponse", (), {"status": 403, "reason": "Forbidden"})()
    return discord.Forbidden(response, "Missing Permissions")


def test_a_replaced_post_is_deleted_and_forgotten(world):
    sim, organizer, player, old = world

    async def play():
        for _ in range(tournament_cog.LIVE_POST_IN_VIEW):
            await sim.chat(player, "pushing it up")
        return (await sim.run(player, "tournament bracket", name="Cup")).sent

    new = asyncio.run(play())
    assert new is not old and new.embed is not None
    assert old.deleted
    assert [live["message_id"] for live in _live_posts(sim)] == [new.id]


def test_a_post_in_a_channel_thats_gone_is_forgotten(world):
    sim, organizer, player, posted = world
    sim.bot.channels.clear()
    asyncio.run(_refresh(sim))
    assert _live_posts(sim) == []


def test_a_post_the_bot_cant_edit_any_more_is_forgotten_without_an_error(world, monkeypatch):
    sim, organizer, player, posted = world

    async def refuse(**kwargs):
        raise _forbidden()

    monkeypatch.setattr(posted, "edit", refuse)
    asyncio.run(_record_first_result(sim, organizer))
    asyncio.run(_refresh(sim))
    assert _live_posts(sim) == []
    assert not sim.issues  # a warning, not a logged error


def test_a_tournament_deleted_mid_refresh_is_skipped(world):
    sim, organizer, player, posted = world
    conn = db.get_connection()
    try:
        [live] = db.get_live_posts(conn)
        db.delete_tournament(conn, live["tournament_id"])
        assert asyncio.run(_cog(sim)._refresh(conn, live)) is True
    finally:
        conn.close()


def test_an_idle_refresh_does_no_work(world, monkeypatch):
    sim, organizer, player, posted = world
    asyncio.run(_refresh(sim))
    renders = []
    original = tournament_cog.render_live_post
    monkeypatch.setattr(tournament_cog, "render_live_post", lambda *args: renders.append(1) or original(*args))
    asyncio.run(_refresh(sim))
    assert renders == []
    asyncio.run(_record_first_result(sim, organizer))
    asyncio.run(_refresh(sim))
    assert renders  # a change gets picked up
    assert "Ace" in posted.text()


def test_a_post_retired_mid_refresh_stays_retired(world):
    """A refresh pass and a member's request that replaces the live post, run at the same
    instant across many interleavings: the old post must end up deleted."""
    sim, organizer, player, first = world
    sim.simultaneous = True
    cog = _cog(sim)

    async def round_of(seed):
        # A change each round, so the refresh pass has an edit to make while the post is replaced.
        old_name, new_name = ("Dash", "Dash II") if seed % 2 == 0 else ("Dash II", "Dash")
        await sim.run(organizer, "admin editteam", tournament="Cup", team=old_name, new_name=new_name)
        await sim.run(player, "tournament leaderboard", name="Cup")  # a second live post in the pass's list
        for _ in range(tournament_cog.LIVE_POST_IN_VIEW):
            await sim.chat(player, "pushing it up")  # so the request posts a fresh one
        before = {m.id for m in sim.channel.messages}
        await sim.together([cog.refresh_live_posts.coro(cog),
                            sim.run(player, "tournament bracket", name="Cup")], random.Random(seed))
        return before

    for seed in range(30):
        asyncio.run(round_of(seed))
        live_ids = {live["message_id"] for live in _live_posts(sim)}
        showing = [m.id for m in sim.channel.messages if m.public and not m.deleted and m.embed is not None]
        assert set(showing) == live_ids, f"seed {seed}: a replaced post is still in the channel"


def test_two_members_asking_at_once_leave_one_live_post(world):
    """Two members ask for the bracket at the same instant with it out of view: both may
    post a copy, but every copy except the live one must end up deleted."""
    sim, organizer, player, first = world
    sim.simultaneous = True
    other = sim.add_user(3, "Other player")

    async def round_of(seed):
        for _ in range(tournament_cog.LIVE_POST_IN_VIEW):
            await sim.chat(player, "pushing it up")
        await sim.together([sim.run(player, "tournament bracket", name="Cup"),
                            sim.run(other, "tournament bracket", name="Cup")], random.Random(seed))

    for seed in range(30):
        asyncio.run(round_of(seed))
        [live] = [row["message_id"] for row in _live_posts(sim) if row["kind"] == "bracket"]
        still_showing = [m.id for m in sim.channel.messages
                         if m.public and not m.deleted and m.embed is not None and m.embed.title.startswith("Bracket")]
        assert still_showing == [live], f"seed {seed}: copies left showing the bracket: {still_showing}"


def test_a_member_is_never_pointed_at_a_post_replaced_while_looking(world, monkeypatch):
    """Two members post fresh copies while a third asks at the same instant. The third may
    find a copy in view and then see it replaced while looking. The link they get must be
    to the post that's live when the bot writes it. It can still be replaced while the
    reply is on its way, which nothing can prevent."""
    sim, organizer, player, first = world
    sim.simultaneous = True
    posters = [player, sim.add_user(3, "Second player")]
    asker = sim.add_user(4, "Third player")
    linked = []  # (linked message id, live message id) as each link is written
    original_jump_url = tournament_cog._jump_url

    def jump_url(guild_id, channel_id, message_id):
        [live] = [row["message_id"] for row in _live_posts(sim) if row["kind"] == "bracket"]
        linked.append((message_id, live))
        return original_jump_url(guild_id, channel_id, message_id)

    monkeypatch.setattr(tournament_cog, "_jump_url", jump_url)

    async def round_of(seed):
        for _ in range(tournament_cog.LIVE_POST_IN_VIEW):
            await sim.chat(player, "pushing it up")
        before = len(sim.channel.messages)

        async def ask_once_a_copy_is_up():
            while not any(m.public and m.embed is not None for m in sim.channel.messages[before:]):
                await sim._turn()
            await sim.run(asker, "tournament bracket", name="Cup")

        await sim.together([sim.run(p, "tournament bracket", name="Cup") for p in posters]
                           + [ask_once_a_copy_is_up()], random.Random(seed))

    for seed in range(120):  # the race needs the right interleaving, about one round in thirteen
        asyncio.run(round_of(seed))
    assert linked  # otherwise the race was never set up
    assert all(message_id == live for message_id, live in linked), "linked a post that had been replaced"


def test_a_restart_leaves_an_unchanged_post_alone(world):
    """The bot restarting mustn't edit a post that's already current: Discord would mark it
    "(edited)" for nothing."""
    sim, organizer, player, posted = world
    asyncio.run(_refresh(sim))
    edits_before = posted.edits
    restarted = tournament_cog.TournamentCog(sim.bot)
    try:
        asyncio.run(restarted.refresh_live_posts.coro(restarted))
    finally:
        asyncio.run(restarted.cog_unload())
    assert posted.edits == edits_before


def test_a_database_from_before_fingerprints_gets_the_column(tmp_path):
    conn = db.get_connection(tmp_path / "old.db")
    db.init_db(conn)
    conn.executescript("""
        DROP TABLE live_posts;
        CREATE TABLE live_posts (tournament_id INTEGER NOT NULL, kind TEXT NOT NULL,
            channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY (tournament_id, kind));
        INSERT INTO live_posts VALUES (1, 'bracket', 2, 3);
    """)
    db.init_db(conn)
    [row] = db.get_live_posts(conn)
    assert row["message_id"] == 3 and row["shown"] is None
    conn.close()
