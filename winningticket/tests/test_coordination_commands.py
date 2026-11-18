"""Match coordination: the bot calling matches into voice rooms, Ready Up, reminders and
forfeits, run through the real cogs."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from bracket import bracket_order
from sim.discord_fakes import FakeAttachment, FakeCategory, FakeVoiceChannel
from sim.harness import GUILD_ID, Simulation

CSV = b"team_a,team_b\nAce,Blaze\nCyan,Dune\n"


def _build(tmp_path, rooms=1, slots=1, linked=("Ace", "Blaze", "Cyan", "Dune")):
    """A CSV tournament "Cup" (Ace vs Blaze, then Cyan vs Dune) with `slots` matches called
    at once into `rooms` rooms, locked so the bot starts calling. Entrants in `linked` have members."""
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        players = {name: sim.add_user(user_id, name) for user_id, name in zip(range(10, 14), ("Ace", "Blaze", "Cyan", "Dune"))}
        category = FakeCategory("Match Rooms", rooms)
        sim.bot.channels[category.id] = category
        await sim.run(organizer, "admin matchrooms", category=category)
        await sim.run(organizer, "tournament create", name="Cup", csv_file=FakeAttachment(CSV), match_slots=slots)
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Cup")
        teams = {row["name"]: team_id for team_id, row in db.get_teams(conn, tournament["id"]).items()}
        for name in linked:
            db.record_agreement(conn, GUILD_ID, players[name].id)
            db.link_team_member(conn, tournament["id"], teams[name], players[name].id)
        matches = [m.id for m in sorted(db.get_matches(conn, tournament["id"]).values(), key=bracket_order)]
        conn.close()
        await sim.run(organizer, "tournament open", name="Cup")
        await sim.run(organizer, "tournament lock", name="Cup")
        sim.channel.messages.clear()  # only what the tests do is looked at
        return sim, organizer, players, tournament["id"], teams, matches, category
    return asyncio.run(build())


@pytest.fixture
def world(tmp_path):
    built = _build(tmp_path)
    yield built
    asyncio.run(built[0].close())


def _loop(sim):
    cog = sim.cogs["ResultsCog"]
    asyncio.run(sim.run_background("match check", lambda: cog.tend_matches.coro(cog)))


def _posts(sim):
    return [m for m in sim.channel.messages if m.public and not m.deleted]


def _click(sim, user, message, label):
    async def go():
        interaction = await sim.click(user, message, label)
        return interaction.sent if interaction is not None else None
    return asyncio.run(go())


def _age_call(match_id, minutes):
    then = db.to_db_time(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    conn = db.get_connection()
    with conn:
        conn.execute("UPDATE match_calls SET called_at = ? WHERE match_id = ?", (then, match_id))
        conn.execute("UPDATE match_calls SET reminded_at = ? WHERE match_id = ? AND reminded_at IS NOT NULL",
                     (then, match_id))
    conn.close()


def _outcomes(tournament_id):
    conn = db.get_connection()
    try:
        return db.get_outcomes(conn, tournament_id)
    finally:
        conn.close()


def test_a_called_match_starts_once_both_sides_ready_up(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)  # one place, so only the first match is called
    assert call.content.startswith("🎮 Round 1: **Ace** vs **Blaze** is up! <@10> and <@11>, press **Ready Up**")
    assert call.content.endswith("Ready: Ace ⏳ · Blaze ⏳")

    reply = _click(sim, players["Ace"], call, "Ready Up")
    assert reply.content == "You're ready. Waiting for your opponent." and not reply.public
    assert call.content.endswith("Ready: Ace ✅ · Blaze ⏳")
    assert _click(sim, players["Cyan"], call, "Ready Up").content == "This isn't your match."

    _click(sim, players["Blaze"], call, "Ready Up")
    [start] = _posts(sim)  # a fresh post, so both are notified, and the call is gone
    assert call.deleted
    room = category.voice_channels[0].id
    assert start.content == (f"▶️ Round 1: **Ace** vs **Blaze**. <@10> and <@11>, head to <#{room}> and stream your "
                             f"match there. When it's over, press **Report Result** below, or use `/match report`.")
    assert [item.item.label for item in start.view.children] == ["Report Result"]
    assert not sim.issues


def test_players_can_report_from_the_started_match_post(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    _click(sim, players["Ace"], call, "Ready Up")
    _click(sim, players["Blaze"], call, "Ready Up")
    [start] = _posts(sim)
    assert _click(sim, players["Cyan"], start, "Report Result").content == "This isn't your match."

    async def report(user):
        interaction = await sim.click(user, start, "Report Result")
        assert interaction.sent.content == "Round 1: **Ace** vs **Blaze**. Who won?" and not interaction.sent.public
        await sim.click(user, interaction.sent, "Ace")
    asyncio.run(report(players["Ace"]))
    [waiting] = _posts(sim)
    assert waiting is start and [item.item.label for item in start.view.children] == ["Report Result"]
    asyncio.run(report(players["Blaze"]))
    [result] = _posts(sim)
    assert result.content.startswith("📣 Round 1: **Ace** defeats **Blaze**.") and result.view is None
    assert _outcomes(tid).winners[matches[0]] == teams["Ace"]
    assert not sim.issues


def test_reporting_waits_for_the_match_to_start_and_frees_the_place(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    early = asyncio.run(sim.run(players["Ace"], "match report", tournament="Cup")).sent
    assert "hasn't started yet" in early.content
    _click(sim, players["Ace"], call, "Ready Up")
    _click(sim, players["Blaze"], call, "Ready Up")

    async def report(user):
        reply = (await sim.run(user, "match report", tournament="Cup")).sent
        await sim.click(user, reply, "Ace")
    asyncio.run(report(players["Ace"]))
    asyncio.run(report(players["Blaze"]))
    [result] = _posts(sim)
    assert result.content.startswith("📣 Round 1: **Ace** defeats **Blaze**.")

    _loop(sim)
    result, next_call = _posts(sim)
    assert next_call.content.startswith("🎮 Round 1: **Cyan** vs **Dune** is up!")
    assert not sim.issues


def test_a_side_that_never_readies_is_reminded_then_forfeits(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    _click(sim, players["Ace"], call, "Ready Up")
    _age_call(matches[0], 6)
    _loop(sim)
    [reminder] = _posts(sim)
    assert call.deleted
    assert reminder.content.startswith("🎮 Round 1: **Ace** vs **Blaze** is up! <@11>, you still need to press **Ready Up**")

    _age_call(matches[0], 6)
    _loop(sim)
    outcomes = _outcomes(tid)
    assert outcomes.winners[matches[0]] == teams["Ace"]
    assert matches[0] in outcomes.walkovers  # a forfeit scores no picks and doesn't change ratings
    assert reminder.content.startswith("Round 1: **Ace** wins by walkover.")
    assert reminder.view is None
    assert not sim.issues


def test_a_match_nobody_readies_for_is_called_off(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    _age_call(matches[0], 6)
    _loop(sim)
    _age_call(matches[0], 6)
    _loop(sim)
    assert _outcomes(tid).skipped == {matches[0]}


def test_a_match_with_an_unlinked_entrant_starts_at_once_and_needs_an_organizer(tmp_path):
    sim, organizer, players, tid, teams, matches, category = _build(tmp_path, linked=("Ace",))
    try:
        _loop(sim)
        [start] = _posts(sim)
        assert start.content.endswith("An organizer needs to watch it there and record the result.")
        assert "<@10> and **Blaze**, head to" in start.content
        assert not sim.issues
    finally:
        asyncio.run(sim.close())


def test_a_ready_match_waits_for_a_room(tmp_path):
    sim, organizer, players, tid, teams, matches, category = _build(tmp_path, rooms=2, slots=2)
    try:
        _loop(sim)
        first_call, second_call = _posts(sim)
        category.voice_channels.pop()  # a room is taken away while both matches are called
        for user, call in ((players["Ace"], first_call), (players["Blaze"], first_call),
                           (players["Cyan"], second_call), (players["Dune"], second_call)):
            _click(sim, user, call, "Ready Up")
        # The waiting call is edited, and the started match is posted afresh to notify its players.
        waiting, playing = _posts(sim)
        assert waiting.content == "🎮 Round 1: **Cyan** vs **Dune** is ready. Waiting for a room to come free."
        assert playing.content.startswith("▶️ Round 1: **Ace** vs **Blaze**.")

        conn = db.get_connection()
        db.record_result(conn, tid, matches[0], teams["Ace"], organizer.id)
        conn.close()
        _loop(sim)
        assert any(post.content.startswith("▶️ Round 1: **Cyan** vs **Dune**.") for post in _posts(sim))
        assert not sim.issues
    finally:
        asyncio.run(sim.close())


def test_more_places_than_rooms_is_refused(tmp_path):
    sim, organizer, players, tid, teams, matches, category = _build(tmp_path)
    try:
        reply = asyncio.run(sim.run(organizer, "admin matchslots", tournament="Cup", match_slots=3)).sent
        assert reply.content == "There's only 1 match room, so only 1 match can be called at once."
    finally:
        asyncio.run(sim.close())


def _admin(sim, organizer, command, **options):
    return asyncio.run(sim.run(organizer, command, tournament="Cup", **options)).sent


def _calls(tournament_id):
    conn = db.get_connection()
    try:
        return [call["match_id"] for call in db.get_calls(conn, tournament_id)]
    finally:
        conn.close()


def _set_result(sim, organizer, match_id, winner, confirm=False):
    """/admin setresult on `match_id`, picking `winner`, and Confirm when it overwrites."""
    async def go():
        menu = (await sim.run(organizer, "admin setresult", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(match_id))
        await sim.click(organizer, menu, winner)
        if confirm:
            await sim.click(organizer, menu, "Confirm")
    asyncio.run(go())


def _forfeit_blaze(sim, players, matches):
    """Ace readies up for the first match and Blaze never does, so Ace wins by forfeit."""
    _loop(sim)
    [call] = _posts(sim)
    _click(sim, players["Ace"], call, "Ready Up")
    _age_call(matches[0], 6)
    _loop(sim)
    _age_call(matches[0], 6)
    _loop(sim)


def test_withdrawing_a_called_entrant_frees_the_place_for_the_next_match(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    _admin(sim, organizer, "admin withdraw", entrant="Blaze", reason="withdrawn")
    _loop(sim)
    assert call.content.startswith("Round 1: **Ace** wins by walkover.") and call.view is None
    assert _calls(tid) == [matches[1]]
    assert _posts(sim)[-1].content.startswith("🎮 Round 1: **Cyan** vs **Dune** is up!")
    assert not sim.issues


def test_an_organizer_result_during_a_call_ends_it_on_the_call(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)

    async def record():
        menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(matches[0]))
        await sim.click(organizer, menu, "Blaze")
    asyncio.run(record())
    assert call.content.startswith(f"📣 Round 1: **Blaze** defeats **Ace** (recorded by <@{organizer.id}>).")
    assert call.view is None
    _loop(sim)
    assert _calls(tid) == [matches[1]]
    assert not sim.issues


def test_changing_an_earlier_result_calls_the_final_off_and_calls_it_again(world):
    sim, organizer, players, tid, teams, matches, category = world
    conn = db.get_connection()
    db.record_result(conn, tid, matches[0], teams["Ace"], organizer.id)
    db.record_result(conn, tid, matches[1], teams["Cyan"], organizer.id)
    conn.close()
    _loop(sim)
    [final_call] = _posts(sim)
    assert final_call.content.startswith("🎮 Round 2: **Ace** vs **Cyan** is up!")

    _set_result(sim, organizer, matches[0], "Blaze", confirm=True)
    _loop(sim)
    assert final_call.content == "Round 2: **Ace** vs **Cyan** is off, because an earlier result changed."
    assert final_call.view is None
    _loop(sim)  # the new call gets a post of its own once the old one has ended
    assert _posts(sim)[-1].content.startswith("🎮 Round 2: **Blaze** vs **Cyan** is up!")
    assert _calls(tid) == [matches[2]]
    assert not sim.issues


def test_turning_calls_off_lets_a_called_match_finish_but_calls_no_more(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    reply = _admin(sim, organizer, "admin matchslots")
    assert reply.content == "The bot no longer calls **Cup**'s matches. Matches it already called carry on."
    _click(sim, players["Ace"], call, "Ready Up")
    _click(sim, players["Blaze"], call, "Ready Up")
    [start] = _posts(sim)
    assert start.content.startswith("▶️ Round 1: **Ace** vs **Blaze**.")

    conn = db.get_connection()
    db.record_result(conn, tid, matches[0], teams["Ace"], organizer.id)
    conn.close()
    _loop(sim)
    assert _calls(tid) == []
    assert not any("is up!" in post.content for post in _posts(sim))
    assert not sim.issues


def test_an_organizer_can_overwrite_a_forfeit(world):
    sim, organizer, players, tid, teams, matches, category = world
    _forfeit_blaze(sim, players, matches)
    assert matches[0] in _outcomes(tid).forfeits
    _set_result(sim, organizer, matches[0], "Blaze", confirm=True)
    outcomes = _outcomes(tid)
    assert outcomes.played[matches[0]] == teams["Blaze"] and matches[0] not in outcomes.forfeits
    assert not sim.issues


def test_an_organizer_can_clear_a_forfeit_and_the_match_is_called_again_in_turn(world):
    sim, organizer, players, tid, teams, matches, category = world
    _forfeit_blaze(sim, players, matches)

    async def clear():
        menu = (await sim.run(organizer, "admin clearresult", tournament="Cup")).sent
        await sim.choose(organizer, menu, str(matches[0]))
        await sim.click(organizer, menu, "Confirm")
    asyncio.run(clear())
    assert matches[0] not in _outcomes(tid).decided
    _loop(sim)
    assert _calls(tid) == [matches[1]]  # the one place went to the next match meanwhile

    conn = db.get_connection()
    db.record_result(conn, tid, matches[1], teams["Cyan"], organizer.id)
    conn.close()
    _loop(sim)
    assert _calls(tid) == [matches[0]]
    assert not sim.issues


def test_a_room_added_to_the_category_is_used_straight_away(tmp_path):
    sim, organizer, players, tid, teams, matches, category = _build(tmp_path, rooms=2, slots=2)
    try:
        _loop(sim)
        first_call, second_call = _posts(sim)
        category.voice_channels.pop()
        for user, call in ((players["Ace"], first_call), (players["Blaze"], first_call),
                           (players["Cyan"], second_call), (players["Dune"], second_call)):
            _click(sim, user, call, "Ready Up")
        new_room = FakeVoiceChannel(999, "Room 3")
        category.voice_channels.append(new_room)
        _loop(sim)
        assert any(post.content.startswith("▶️ Round 1: **Cyan** vs **Dune**.") and f"<#{new_room.id}>" in post.content
                   for post in _posts(sim))
        assert not sim.issues
    finally:
        asyncio.run(sim.close())


def test_the_bot_readies_up_for_its_own_entry_when_playing_for_testing(world, monkeypatch):
    sim, organizer, players, tid, teams, matches, category = world
    monkeypatch.setenv("BOT_PLAYS_FOR_TESTING", "1")
    conn = db.get_connection()
    conn.execute("DELETE FROM team_members WHERE team_id = ?", (teams["Blaze"],))
    conn.commit()
    db.link_team_member(conn, tid, teams["Blaze"], sim.bot.user.id)
    conn.close()
    _loop(sim)  # calls the match and posts the call
    [call] = _posts(sim)
    assert "Blaze ⏳" in call.content
    _loop(sim)  # the bot readies up once its call is out
    assert "Blaze ✅" in call.content
    _click(sim, players["Ace"], call, "Ready Up")
    [start] = _posts(sim)
    assert start.content.startswith("▶️ Round 1: **Ace** vs **Blaze**.")
    assert not sim.issues


def test_the_bot_never_readies_up_unless_playing_for_testing(world, monkeypatch):
    sim, organizer, players, tid, teams, matches, category = world
    monkeypatch.delenv("BOT_PLAYS_FOR_TESTING", raising=False)
    conn = db.get_connection()
    db.link_team_member(conn, tid, teams["Blaze"], sim.bot.user.id)
    conn.close()
    _loop(sim)
    _loop(sim)
    [call] = _posts(sim)
    assert "Blaze ⏳" in call.content


def test_a_vote_on_a_started_match_counts_down_on_its_post(world):
    sim, organizer, players, tid, teams, matches, category = world
    _loop(sim)
    [call] = _posts(sim)
    _click(sim, players["Ace"], call, "Ready Up")
    _click(sim, players["Blaze"], call, "Ready Up")
    [start] = _posts(sim)

    async def concede():
        interaction = await sim.click(players["Ace"], start, "Report Result")
        await sim.click(players["Ace"], interaction.sent, "Blaze")
    asyncio.run(concede())
    assert start.content.startswith("⏳ Round 1: **Ace** reports that **Blaze** won. Waiting for **Blaze** to report, "
                                    "or it's recorded <t:")
    assert [item.item.label for item in start.view.children] == ["Report Result"]
    assert not sim.issues
