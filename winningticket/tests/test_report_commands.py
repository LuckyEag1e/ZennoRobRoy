"""Players reporting results with /match report, and the loop that warns about and gives
unanswered votes, run through the real cogs."""

import asyncio
import re
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import pytest

import db
from bracket import bracket_order
from sim.discord_fakes import FakeAttachment
from sim.harness import GUILD_ID, Simulation


@pytest.fixture
def world(tmp_path):
    """A locked CSV tournament "Cup": Ace vs Blaze, then Cyan vs Dune. Ace, Blaze and Cyan
    are linked to members; Dune has no account."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        players = {name: sim.add_user(user_id, name) for user_id, name in ((10, "Ace"), (11, "Blaze"), (12, "Cyan"))}
        await sim.run(organizer, "tournament create", name="Cup",
                      csv_file=FakeAttachment(b"team_a,team_b\nAce,Blaze\nCyan,Dune\n"))
        await sim.run(organizer, "tournament open", name="Cup")
        await sim.run(organizer, "tournament lock", name="Cup")
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Cup")
        teams = {row["name"]: team_id for team_id, row in db.get_teams(conn, tournament["id"]).items()}
        for name, user in players.items():
            db.record_agreement(conn, GUILD_ID, user.id)
            db.link_team_member(conn, tournament["id"], teams[name], user.id)
        first_match = min(db.get_matches(conn, tournament["id"]).values(), key=bracket_order).id
        conn.close()
        sim.channel.messages.clear()  # only what the tests do is looked at
        return sim, organizer, players, tournament["id"], teams, first_match

    sim, *rest = asyncio.run(build())
    yield (sim, *rest)
    asyncio.run(sim.close())


def _vote(sim, user, label):
    """/match report, then click `label`. Returns the private reply."""
    async def go():
        reply = (await sim.run(user, "match report", tournament="Cup")).sent
        await sim.click(user, reply, label)
        return reply
    return asyncio.run(go())


def _run_loop(sim):
    cog = sim.cogs["ResultsCog"]
    asyncio.run(sim.run_background("match check", lambda: cog.tend_matches.coro(cog)))


def _age(match_id, minutes):
    """Move a match's votes, and the time its post last changed, `minutes` into the past."""
    then = db.to_db_time(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    conn = db.get_connection()
    with conn:
        conn.execute("UPDATE result_reports SET reported_at = ? WHERE match_id = ?", (then, match_id))
        conn.execute("UPDATE match_posts SET warned_at = ? WHERE match_id = ? AND warned_at IS NOT NULL", (then, match_id))
    conn.close()


def _outcomes(tournament_id):
    conn = db.get_connection()
    try:
        return db.get_outcomes(conn, tournament_id)
    finally:
        conn.close()


def _public(sim, since=0):
    return [m for m in sim.channel.messages[since:] if m.public and not m.deleted]


def test_matching_votes_are_recorded_and_announced_once(world):
    sim, organizer, players, tid, teams, match_id = world
    before = len(sim.channel.messages)
    first = _vote(sim, players["Ace"], "Ace")
    assert "win is yours" in first.content and not first.public
    assert _public(sim, before) == []
    second = _vote(sim, players["Blaze"], "Ace")
    assert "Both sides agree" in second.content
    assert [m.content for m in _public(sim, before)] == [
        "📣 Round 1: **Ace** defeats **Blaze**. **Ace** advances to Round 2 to face TBD."]
    assert _outcomes(tid).played == {match_id: teams["Ace"]}
    assert not sim.issues


def test_an_unanswered_claim_is_warned_about_then_given(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 4)
    _run_loop(sim)
    assert _public(sim) == []  # not yet

    _age(match_id, 6)
    before = len(sim.channel.messages)
    _run_loop(sim)
    [warning] = _public(sim, before)
    assert warning.content.startswith("⏳ Round 1: **Ace** reports winning. <@11>, if that's wrong, use `/match report` by <t:")

    _age(match_id, 6)
    _run_loop(sim)
    assert warning.content == ("⏰ Round 1: **Blaze** didn't report in time, so **Ace** gets the win. "
                               "**Ace** advances to Round 2 to face TBD.")
    assert len(_public(sim, before)) == 1
    assert _outcomes(tid).played == {match_id: teams["Ace"]}
    conn = db.get_connection()
    assert db.get_match_posts(conn) == []
    conn.close()
    assert not sim.issues


def test_answering_a_warning_turns_it_into_the_result(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 6)
    _run_loop(sim)
    [warning] = _public(sim)
    before = len(sim.channel.messages)
    _vote(sim, players["Blaze"], "Ace")
    assert warning.content.startswith("📣 Round 1: **Ace** defeats **Blaze**.")
    assert _public(sim, before) == []


def test_a_dispute_is_posted_once_and_settled_in_place(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    reply = _vote(sim, players["Blaze"], "Blaze")
    assert "organizer will record" in reply.content
    [dispute] = _public(sim)
    assert dispute.content == "⚠️ Round 1: **Ace** and **Blaze** each report winning. An organizer will record the result."
    _age(match_id, 30)
    _run_loop(sim)
    assert _outcomes(tid).decided == set()  # a dispute never times out

    async def settle():
        menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(match_id))
        await sim.click(organizer, menu, "Blaze")
    before = len(sim.channel.messages)
    asyncio.run(settle())
    assert dispute.content.startswith(f"📣 Round 1: **Blaze** defeats **Ace** (recorded by <@{organizer.id}>).")
    assert _public(sim, before) == []
    assert not sim.issues


def test_taking_back_a_warned_vote_withdraws_the_warning(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 6)
    _run_loop(sim)
    [warning] = _public(sim)
    reply = _vote(sim, players["Ace"], "Take Back Vote")
    assert reply.content == "Your vote is taken back."
    assert warning.content == "Round 1: the report on **Ace** vs **Blaze** no longer stands."
    _age(match_id, 30)
    _run_loop(sim)
    assert _outcomes(tid).decided == set()


def test_a_concession_gets_no_warning_and_is_recorded_after_the_wait(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Blaze")
    _age(match_id, 6)
    _run_loop(sim)
    assert _public(sim) == []
    _age(match_id, 11)
    _run_loop(sim)
    assert [m.content for m in _public(sim)] == [
        "📣 Round 1: **Blaze** defeats **Ace**. **Blaze** advances to Round 2 to face TBD."]
    assert _outcomes(tid).played == {match_id: teams["Blaze"]}


def test_each_side_naming_the_other_calls_the_match_off(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Blaze")
    reply = _vote(sim, players["Blaze"], "Ace")
    assert "called off" in reply.content
    assert _public(sim)[0].content == "📣 Round 1: **Ace** vs **Blaze** was called off with no winner."
    assert _outcomes(tid).skipped == {match_id}


def test_no_vote_against_an_opponent_with_no_account(world):
    sim, organizer, players, tid, teams, match_id = world
    reply = asyncio.run(sim.run(players["Cyan"], "match report", tournament="Cup")).sent
    assert reply.content == "**Dune** has no Discord account behind them, so an organizer records this match."
    assert reply.view is None


def test_someone_with_no_match_cant_report(world):
    sim, organizer, *_ = world
    reply = asyncio.run(sim.run(organizer, "match report", tournament="Cup")).sent
    assert "aren't an entrant" in reply.content


def test_an_organizer_changing_an_earlier_result_drops_later_votes(world):
    sim, organizer, players, tid, teams, match_id = world
    conn = db.get_connection()
    matches = sorted(db.get_matches(conn, tid).values(), key=bracket_order)
    semi_2, final = matches[1].id, matches[2].id
    db.record_result(conn, tid, match_id, teams["Ace"], 1)
    db.record_result(conn, tid, semi_2, teams["Cyan"], 1)
    conn.close()
    _vote(sim, players["Ace"], "Ace")
    conn = db.get_connection()
    assert len(db.get_reports(conn, final)) == 1
    db.record_result(conn, tid, match_id, teams["Blaze"], 1)
    assert db.get_reports(conn, final) == []
    conn.close()


def test_a_post_whose_sending_never_finished_is_sent_on_the_next_check(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    conn = db.get_connection()
    db.claim_match_post(conn, match_id, sim.channel.id, (teams["Ace"], teams["Blaze"]))
    conn.close()
    _run_loop(sim)
    [waiting] = _public(sim)
    assert waiting.content == "⏳ Round 1: **Ace** reports winning. Waiting for **Blaze** to report."
    _age(match_id, 6)
    _run_loop(sim)
    [warning] = _public(sim)  # the warning takes over from it, to notify Blaze
    assert warning.content.startswith("⏳ Round 1: **Ace** reports winning. <@11>")
    assert waiting.deleted


def test_a_deleted_warning_is_forgotten_and_the_win_announced_anew(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 6)
    _run_loop(sim)
    [warning] = _public(sim)
    sim.channel.messages.remove(warning)  # someone deleted it
    _age(match_id, 6)
    _run_loop(sim)
    [announcement] = _public(sim)
    assert announcement.content.startswith("⏰ Round 1: **Blaze** didn't report in time")
    conn = db.get_connection()
    assert db.get_match_posts(conn) == []
    conn.close()
    assert not sim.issues


def _warned(sim, players, match_id):
    """Ace claims the win and the loop warns about it. Returns the warning."""
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 6)
    _run_loop(sim)
    [warning] = _public(sim)
    return warning


def _match_posts():
    conn = db.get_connection()
    try:
        return db.get_match_posts(conn)
    finally:
        conn.close()


def test_a_warning_in_a_channel_out_of_reach_is_updated_once_its_back(world):
    sim, organizer, players, tid, teams, match_id = world
    warning = _warned(sim, players, match_id)
    channel = sim.bot.channels.pop(sim.channel.id)
    _age(match_id, 6)
    _run_loop(sim)
    assert _outcomes(tid).played == {match_id: teams["Ace"]}
    assert len(_match_posts()) == 1  # kept, to finish once the channel is back

    sim.bot.channels[channel.id] = channel
    _run_loop(sim)
    assert warning.content.startswith("📣 Round 1: **Ace** defeats **Blaze**.")
    assert _public(sim) == [warning]
    assert _match_posts() == []


def test_a_warning_discord_wont_edit_doesnt_lose_the_organizers_result(world):
    sim, organizer, players, tid, teams, match_id = world
    warning = _warned(sim, players, match_id)
    response = type("FakeHTTPResponse", (), {"status": 403, "reason": "Forbidden"})()

    async def refuse(**kwargs):
        raise discord.Forbidden(response, "Missing Access")
    warning.edit = refuse

    async def record():
        menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(match_id))
        await sim.click(organizer, menu, "Blaze")
    before = len(sim.channel.messages)
    asyncio.run(record())
    [announcement] = _public(sim, before)
    assert announcement.content.startswith(f"📣 Round 1: **Blaze** defeats **Ace** (recorded by <@{organizer.id}>).")
    assert _outcomes(tid).played == {match_id: teams["Blaze"]}
    assert _match_posts() == []

    warning.delete = refuse
    _run_loop(sim)
    assert len(_replaced_posts()) == 1  # kept to try again

    del warning.delete  # Discord lets the bot in again
    _run_loop(sim)
    assert warning.deleted  # so only the announcement is left
    assert _replaced_posts() == []


def _replaced_posts():
    conn = db.get_connection()
    try:
        return db.get_replaced_match_posts(conn)
    finally:
        conn.close()


def test_calling_off_a_warned_match_announces_its_walkovers_on_the_warning(world):
    sim, organizer, players, tid, teams, match_id = world

    async def record_cyan():
        menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(_second_match(tid)))
        await sim.click(organizer, menu, "Cyan")
    asyncio.run(record_cyan())
    sim.channel.messages.clear()

    warning = _warned(sim, players, match_id)
    _vote(sim, players["Ace"], "Blaze")
    _vote(sim, players["Blaze"], "Ace")
    assert warning.content == ("📣 Round 1: **Ace** vs **Blaze** was called off with no winner.\n"
                               "Round 2: **Cyan** wins by walkover. 🏆 **Cyan** is the champion of Cup!")
    assert _public(sim) == [warning]


def _second_match(tournament_id):
    conn = db.get_connection()
    try:
        return sorted(db.get_matches(conn, tournament_id).values(), key=bracket_order)[1].id
    finally:
        conn.close()


def _due_in_minutes(reply):
    """How far off the countdown in a vote reply is, in whole minutes."""
    [stamp] = re.findall(r"<t:(\d+):R>", reply.content)
    return round((int(stamp) - datetime.now(timezone.utc).timestamp()) / 60)


def test_the_vote_reply_counts_down_to_when_the_win_is_really_given(world):
    sim, organizer, players, tid, teams, match_id = world
    first = _vote(sim, players["Ace"], "Ace")
    assert "the win is yours <t:" in first.content and _due_in_minutes(first) == 10
    _age(match_id, 6)
    _run_loop(sim)
    changed = _vote(sim, players["Ace"], "Blaze")  # the warning is up, so its clock restarts
    assert "or <t:" in changed.content and _due_in_minutes(changed) == 5


def test_repeating_a_vote_doesnt_restart_the_other_sides_time(world):
    sim, organizer, players, tid, teams, match_id = world
    _vote(sim, players["Ace"], "Ace")
    _age(match_id, 4)
    again = _vote(sim, players["Ace"], "Ace")
    assert _due_in_minutes(again) == 6
    _age(match_id, 6)  # the first vote, unmoved by the repeat, is now past its 5 minutes
    _run_loop(sim)
    [warning] = _public(sim)
    assert warning.content.startswith("⏳ Round 1: **Ace** reports winning.")


def test_conceding_after_a_warning_rewords_it_without_asking_the_winner(world):
    sim, organizer, players, tid, teams, match_id = world
    warning = _warned(sim, players, match_id)
    _vote(sim, players["Ace"], "Blaze")
    assert warning.content.startswith("⏳ Round 1: **Ace** reports that **Blaze** won. It's recorded <t:")
    assert warning.content.endswith(":R> unless **Blaze** reports otherwise.")
    assert _public(sim) == [warning]
    _age(match_id, 6)
    _run_loop(sim)
    assert warning.content.startswith("📣 Round 1: **Blaze** defeats **Ace**.")
    assert _outcomes(tid).played == {match_id: teams["Blaze"]}
