"""/match dropout, /admin withdraw, /admin reinstate and /admin skipmatch, run through the real cogs."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from sim.discord_fakes import FakeAttachment
from sim.harness import GUILD_ID, Simulation


@pytest.fixture
def world(tmp_path):
    """A locked CSV tournament "Cup" (Ace vs Blaze, Cyan vs Dune), with a member linked to Ace."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        ace = sim.add_user(10, "Ace")
        await sim.run(organizer, "tournament create", name="Cup",
                      csv_file=FakeAttachment(b"team_a,team_b\nAce,Blaze\nCyan,Dune\n"))
        await sim.run(organizer, "tournament open", name="Cup")
        await sim.run(organizer, "tournament lock", name="Cup")
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Cup")
        teams = {row["name"]: team_id for team_id, row in db.get_teams(conn, tournament["id"]).items()}
        db.record_agreement(conn, GUILD_ID, ace.id)
        db.link_team_member(conn, tournament["id"], teams["Ace"], ace.id)
        conn.close()
        return sim, organizer, ace, tournament["id"], teams

    sim, *rest = asyncio.run(build())
    yield (sim, *rest)
    asyncio.run(sim.close())


def _outcomes(tournament_id):
    conn = db.get_connection()
    try:
        return db.get_outcomes(conn, tournament_id)
    finally:
        conn.close()


def _public_since(sim, index):
    return [m.content for m in sim.public_posts_since(index)]


def test_a_player_drops_out_after_retyping_the_name(world):
    sim, organizer, ace, tournament_id, teams = world

    async def drop():
        wrong = await sim.run(ace, "match dropout", tournament="Cup", confirm_name="cup")
        before = len(sim.channel.messages)
        right = await sim.run(ace, "match dropout", tournament="Cup", confirm_name="Cup")
        again = await sim.run(ace, "match dropout", tournament="Cup", confirm_name="Cup")
        return wrong.sent, right.sent, again.sent, _public_since(sim, before)

    wrong, right, again, posts = asyncio.run(drop())
    assert "still in" in wrong.content and not wrong.public
    assert not right.public
    assert posts == ["🚪 **Ace** dropped out of **Cup**.\n"
                     "Round 1: **Blaze** wins by walkover. **Blaze** advances to Round 2 to face TBD."]
    assert "already left" in again.content
    assert _outcomes(tournament_id).walkovers and not sim.issues


def test_someone_who_isnt_an_entrant_cant_drop_out(world):
    sim, organizer, *_ = world
    reply = asyncio.run(sim.run(organizer, "match dropout", tournament="Cup", confirm_name="Cup")).sent
    assert "aren't an entrant" in reply.content


def test_an_organizer_disqualifies_and_then_reinstates(world):
    sim, organizer, ace, tournament_id, teams = world

    async def withdraw_and_back():
        before = len(sim.channel.messages)
        await sim.run(organizer, "admin withdraw", tournament="Cup", entrant="Dune", reason="disqualified")
        out = _outcomes(tournament_id)
        await sim.run(organizer, "admin reinstate", tournament="Cup", entrant="Dune")
        return out, _public_since(sim, before)

    while_out, posts = asyncio.run(withdraw_and_back())
    assert while_out.winners == {_match_ids(tournament_id)[1]: teams["Cyan"]}
    assert posts[0].startswith("🚫 An organizer disqualified **Dune** from **Cup**.")
    assert posts[1] == "↩️ **Dune** is back in **Cup**."
    assert _outcomes(tournament_id).winners == {}
    assert not sim.issues


def test_a_member_cant_use_the_organizer_withdrawal(world):
    sim, organizer, ace, tournament_id, _ = world
    asyncio.run(sim.run(ace, "admin withdraw", tournament="Cup", entrant="Blaze", reason="withdrawn"))
    assert _outcomes(tournament_id).winners == {}


def test_skipping_a_match_gives_the_next_opponent_a_walkover(world):
    sim, organizer, ace, tournament_id, teams = world
    semi_1, semi_2, final = _match_ids(tournament_id)
    conn = db.get_connection()
    db.record_result(conn, tournament_id, semi_2, teams["Cyan"], organizer.id)
    conn.close()

    async def skip():
        menu = (await sim.run(organizer, "admin skipmatch", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(semi_1))
        before = len(sim.channel.messages)
        await sim.click(organizer, menu, "Confirm")
        return _public_since(sim, before)

    posts = asyncio.run(skip())
    assert posts == ["📣 Round 1: **Ace** vs **Blaze** was called off with no winner.\n"
                     "Round 2: **Cyan** wins by walkover. 🏆 **Cyan** is the champion of Cup!"]
    outcomes = _outcomes(tournament_id)
    assert outcomes.skipped == {semi_1}
    assert outcomes.walkovers == {final}
    assert not sim.issues


def _match_ids(tournament_id):
    conn = db.get_connection()
    try:
        return [m.id for m in sorted(db.get_matches(conn, tournament_id).values(), key=bracket_order)]
    finally:
        conn.close()
