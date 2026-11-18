"""/match dropout, /admin withdraw, reinstate, skipmatch, setresult and clearresult, run through the real cogs."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from bracket_changes import reinstate_problem
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
    assert posts[0].startswith(f"🚫 <@{organizer.id}> disqualified **Dune** from **Cup**.")
    assert posts[1] == f"↩️ <@{organizer.id}> brought **Dune** back into **Cup**."
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
    assert posts == [f"📣 Round 1: **Ace** vs **Blaze** was called off by <@{organizer.id}> with no winner.\n"
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


def test_setting_and_clearing_results_say_which_organizer_did_it(world):
    sim, organizer, ace, tournament_id, teams = world
    semi_1, _, _ = _match_ids(tournament_id)

    async def set_result(winner, overwrite):
        menu = (await sim.run(organizer, "admin setresult", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(semi_1))
        await sim.click(organizer, menu, winner)
        if overwrite:
            await sim.click(organizer, menu, "Confirm")

    async def clear_result():
        menu = (await sim.run(organizer, "admin clearresult", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(semi_1))
        await sim.click(organizer, menu, "Confirm")

    async def changes():
        before = len(sim.channel.messages)
        await set_result("Ace", overwrite=False)
        await set_result("Blaze", overwrite=True)
        await clear_result()
        return _public_since(sim, before)

    by = f"<@{organizer.id}>"
    assert asyncio.run(changes()) == [
        f"📣 Round 1: **Ace** defeats **Blaze** (recorded by {by}). **Ace** advances to Round 2 to face TBD.",
        f"✏️ {by} changed the Round 1 result: **Blaze** defeats **Ace**. **Blaze** advances to Round 2 to face TBD.",
        f"✏️ {by} cleared the result of Round 1: **Ace** vs **Blaze**, so it's waiting to be played.",
    ]
    assert _outcomes(tournament_id).decided == set()
    assert not sim.issues


def test_a_result_later_results_depend_on_is_cleared_last_first(world):
    sim, organizer, ace, tournament_id, teams = world
    semi_1, semi_2, final = _match_ids(tournament_id)
    conn = db.get_connection()
    db.record_result(conn, tournament_id, semi_1, teams["Ace"], organizer.id)
    db.record_result(conn, tournament_id, semi_2, teams["Cyan"], organizer.id)
    db.record_result(conn, tournament_id, final, teams["Cyan"], organizer.id)
    conn.close()

    async def try_clear(match_id):
        menu = (await sim.run(organizer, "admin clearresult", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(match_id))
        reply = menu.content
        await sim.click(organizer, menu, "Confirm")
        return reply

    async def try_skip(match_id):
        menu = (await sim.run(organizer, "admin skipmatch", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(match_id))
        return menu.content

    refusal = "Later results depend on this one, so clear them first with `/admin clearresult`, latest first: R2: Ace vs Cyan."
    assert asyncio.run(try_clear(semi_1)) == refusal
    assert asyncio.run(try_skip(semi_1)) == refusal
    assert _outcomes(tournament_id).winners[final] == teams["Cyan"]

    asyncio.run(try_clear(final))
    asyncio.run(try_clear(semi_1))
    assert _outcomes(tournament_id).winners == {semi_2: teams["Cyan"]}
    assert not sim.issues



def test_reinstating_waits_for_later_results_to_be_cleared(world):
    """In this small bracket the only later match is the final, whose result finishes the
    tournament, so the check is called directly rather than through /admin reinstate."""
    sim, organizer, ace, tournament_id, teams = world
    semi_1, semi_2, final = _match_ids(tournament_id)
    asyncio.run(sim.run(organizer, "admin withdraw", tournament="Cup", entrant="Blaze", reason="withdrawn"))
    conn = db.get_connection()
    try:
        tournament = db.get_tournament(conn, tournament_id)
        assert reinstate_problem(conn, tournament, teams["Blaze"]) is None
        db.record_result(conn, tournament_id, semi_2, teams["Cyan"], organizer.id)
        db.record_result(conn, tournament_id, final, teams["Ace"], organizer.id)
        assert reinstate_problem(conn, tournament, teams["Blaze"]).startswith("Later results depend on this one")
        assert reinstate_problem(conn, tournament, teams["Cyan"]) is None
    finally:
        conn.close()


def test_withdraw_and_reinstate_only_offer_entrants_they_apply_to(world):
    sim, organizer, ace, tournament_id, teams = world

    async def go():
        await sim.run(organizer, "admin withdraw", tournament="Cup", entrant="Dune", reason="withdrawn")
        return (await sim.autocomplete(organizer, "admin withdraw", "entrant", "", tournament="Cup"),
                await sim.autocomplete(organizer, "admin reinstate", "entrant", "", tournament="Cup"))

    playing, withdrawn = asyncio.run(go())
    assert sorted(playing) == ["Ace", "Blaze", "Cyan"]
    assert withdrawn == ["Dune"]
    assert not sim.issues


def test_a_withdrawal_that_completes_the_tournament_can_be_reinstated(world):
    sim, organizer, ace, tournament_id, teams = world

    async def withdraw_all_but_ace_then_reinstate():
        for entrant in ("Blaze", "Cyan", "Dune"):
            await sim.run(organizer, "admin withdraw", tournament="Cup", entrant=entrant, reason="withdrawn")
        completed = _status(tournament_id)
        reply = await sim.run(organizer, "admin reinstate", tournament="Cup", entrant="Dune")
        return completed, reply.sent

    completed, reply = asyncio.run(withdraw_all_but_ace_then_reinstate())
    assert completed == "completed"
    assert reply.content.startswith("Reinstated **Dune**")
    assert _status(tournament_id) == "in_progress"
    assert not sim.issues


def _status(tournament_id):
    conn = db.get_connection()
    try:
        return db.get_tournament(conn, tournament_id)["status"]
    finally:
        conn.close()
