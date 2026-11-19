"""5v5 events through the real cogs: sign-up buttons with the captain question, the close
preview, check-in, room codes and the captains' report."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import lobby_db
from sim.harness import GUILD_ID, Simulation


def _public(sim):
    return [m for m in sim.channel.messages if m.public and not m.deleted]


def _post_with(sim, text):
    return next(m for m in reversed(_public(sim)) if text in (m.content or ""))


def _buttons(message):
    """The labels of a message's buttons, including the bot's lasting (dynamic) ones."""
    if message.view is None:
        return []
    return [getattr(item, "item", item).label for item in message.view.children]


def _loop(sim):
    cog = sim.cogs["LobbiesCog"]
    asyncio.run(sim.run_background("lobby check", lambda: cog.tend_lobbies.coro(cog)))


async def _sign_up_by_button(sim, user, announcement, pref, agree=True):
    reply = (await sim.click(user, announcement, "Register")).sent
    if agree is not None and "Agree" in [getattr(item, "label", None) for item in reply.view.children]:
        await sim.click(user, reply, "Agree" if agree else "Opt Out")
    await sim.choose(user, reply, pref)
    return reply


def _event(tmp_path, players=12, captains=2):
    async def build():
        sim = await Simulation.create(tmp_path / "test.db")
        organizer = sim.add_user(1, "Organizer", organizer=True)
        users = [sim.add_user(100 + n, f"Player{n}") for n in range(1, players + 1)]
        await sim.run(organizer, "registration open5v5", name="Duel", starts_in_hours=24.0)
        announcement = _post_with(sim, "Sign-ups are open")
        for n, user in enumerate(users, start=1):
            await _sign_up_by_button(sim, user, announcement, "want" if n <= captains else "no")
        return sim, organizer, users, announcement
    return asyncio.run(build())


def test_register_button_asks_consent_then_captain_choice(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=1, captains=1)
    try:
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        signups = lobby_db.get_signups(conn, tournament["id"])
        conn.close()
        assert [(s.user_id, s.captain_pref) for s in signups] == [(101, "want")]
        assert "1** signed up" in announcement.content
    finally:
        asyncio.run(sim.close())


def test_register_command_without_captain_choice_asks_for_it(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=1, captains=0)
    try:
        newcomer = sim.add_user(500, "Late")
        conn = db.get_connection()
        db.record_agreement(conn, GUILD_ID, 500)
        conn.close()
        reply = asyncio.run(sim.run(newcomer, "register", tournament="Duel")).sent
        assert "Will you captain?" in reply.content
        asyncio.run(sim.choose(newcomer, reply, "can"))
        assert "Can captain if needed" in reply.content
    finally:
        asyncio.run(sim.close())


def test_whole_event_from_close_to_result(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        assert "5v5, 2 waitlisted" in preview.content
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        assert "Created the lobbies" in preview.content
        event_post = _post_with(sim, "🎌")
        waiting = [u.id for u in users[10:]]
        assert len([m for m in _public(sim) if "🎌" in (m.content or "")]) == 1
        assert "Lobby 1" in event_post.content and "Waitlist, in order" in event_post.content
        assert len(event_post.allowed_mentions.users) == 12
        assert "Register" in _buttons(announcement)  # late sign-ups join the waitlist

        # Only organizers can start check-in.
        refused = asyncio.run(sim.click(users[0], event_post, "Start Check-In")).sent
        assert "Only organizers" in refused.content
        click = asyncio.run(sim.click(organizer, event_post, "Start Check-In"))
        asyncio.run(sim.submit_fields(organizer, click.response.modal, ["15"]))
        _loop(sim)
        lobby_post = _post_with(sim, "Check-in is open")
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        lobby = lobby_db.get_lobbies(conn, tournament["id"])[0]
        user_of = {team_id: members[0] for team_id, members in db.get_members_by_team(conn, tournament["id"]).items()}
        conn.close()
        by_id = {u.id: u for u in users}
        no_show = next(p for p in lobby.players if p not in lobby.captains)
        for player in lobby.players:
            if player != no_show:
                asyncio.run(sim.click(by_id[user_of[player]], lobby_post, "Check In"))
        asyncio.run(sim.click(by_id[waiting[0]], _post_with(sim, "🎌"), "Check In"))

        # Check-in runs out: the no-show is replaced by the waiter who checked in.
        conn = db.get_connection()
        conn.execute("UPDATE tournaments SET checkin_ends_at = ? WHERE id = ?",
                     (db.to_db_time(datetime.now(timezone.utc) - timedelta(seconds=1)), tournament["id"]))
        conn.commit()
        conn.close()
        _loop(sim)
        ready = _post_with(sim, "drafter.uma.guide/host")
        conn = db.get_connection()
        lobby = lobby_db.get_lobby(conn, lobby.id)
        conn.close()
        assert user_of[no_show] not in [user_of[p] for p in lobby.players]
        assert by_id[waiting[0]].id in [user_of[p] for p in lobby.players]
        assert "Register" not in _buttons(announcement)

        captain_a, captain_b = (by_id[user_of[lobby.teams[side].captain]] for side in "AB")
        outsider = by_id[waiting[1]]
        assert "aren't playing" in asyncio.run(sim.click(outsider, ready, "Get Room Code")).sent.content
        form = asyncio.run(sim.click(captain_a, ready, "Submit Room Code")).response.modal
        asyncio.run(sim.submit_fields(captain_a, form, ["ABC123"]))
        player = by_id[user_of[next(p for p in lobby.players if p not in lobby.captains)]]
        assert "ABC123" in asyncio.run(sim.click(player, _post_with(sim, "hosted by"), "Get Room Code")).sent.content

        # Captain A reports, in two forms, and captain B confirms.
        post = _post_with(sim, "hosted by")
        first = asyncio.run(sim.click(captain_a, post, "Report Result")).response.modal
        between = asyncio.run(sim.submit_fields(captain_a, first, ["9"] * 5)).sent
        second = asyncio.run(sim.click(captain_a, between, _buttons(between)[0])).response.modal
        sent = asyncio.run(sim.submit_fields(captain_a, second, ["2"] * 5)).sent
        assert "Sent to the other captain" in sent.content
        report_post = _post_with(sim, "on these points")
        assert [u.id for u in report_post.allowed_mentions.users] == [captain_b.id]
        assert "Only the other" in asyncio.run(sim.click(captain_a, report_post, "Confirm")).sent.content
        asyncio.run(sim.click(captain_b, report_post, "Confirm"))
        _loop(sim)
        assert "wins" in _post_with(sim, "Every lobby has finished").content

        conn = db.get_connection()
        assert db.get_tournament(conn, tournament["id"])["status"] == "completed"
        conn.close()
        asyncio.run(sim.run(organizer, "tournament conclude", name="Duel"))
        assert "How each lobby went" in _post_with(sim, "How each lobby went").content
        assert not [issue for issue in sim.issues]
    finally:
        asyncio.run(sim.close())


def test_bracket_commands_refuse_a_5v5_event(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=10, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        reply = asyncio.run(sim.run(users[0], "tournament bracket", name="Duel")).sent
        assert "5v5 event" in reply.content
    finally:
        asyncio.run(sim.close())


def test_signup_reply_and_post_show_the_captain_option(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=1, captains=1)
    try:
        newcomer = sim.add_user(500, "Late")
        conn = db.get_connection()
        db.record_agreement(conn, GUILD_ID, 500)
        conn.close()
        reply = asyncio.run(sim.run(newcomer, "register", tournament="Duel", captain="no")).sent
        assert "captain:<your choice>" in reply.content and "entered so far" not in reply.content
        assert "captain:<your choice>" in announcement.content
    finally:
        asyncio.run(sim.close())


def test_closing_a_5v5_event_refuses_seeding(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=10, captains=2)
    try:
        reply = asyncio.run(sim.run(organizer, "registration close", name="Duel", seeding="slide")).sent
        assert "aren't seeded" in reply.content
    finally:
        asyncio.run(sim.close())


def test_one_post_for_every_lobby_and_an_organizer_picks_the_lobby(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=20, captains=4)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        posts = [m for m in _public(sim) if "🎌" in (m.content or "")]
        assert len(posts) == 1 and "Lobby 1" in posts[0].content and "Lobby 2" in posts[0].content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        conn.execute("UPDATE tournaments SET checkin_ends_at = ?, checkin_finished = 1 WHERE id = ?",
                     (db.to_db_time(datetime.now(timezone.utc)), tournament["id"]))
        conn.commit()
        second = lobby_db.get_lobbies(conn, tournament["id"])[1]
        conn.close()
        _loop(sim)
        post = _post_with(sim, "🎌")
        # An organizer who isn't playing picks which lobby a button is for.
        picker = asyncio.run(sim.click(organizer, post, "Get Room Code")).sent
        assert "Which lobby?" in picker.content
        asyncio.run(sim.choose(organizer, picker, str(second.id)))
        assert "isn't in yet" in sim.channel.messages[-1].content
    finally:
        asyncio.run(sim.close())


def test_adding_a_name_only_captain_says_they_cant(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=1, captains=1)
    try:
        reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Dummy1",
                                    captain="want")).sent
        assert "can't captain" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        prefs = {s.name: s.captain_pref for s in lobby_db.get_signups(conn, tournament["id"])}
        conn.close()
        assert prefs["Dummy1"] == "no"
    finally:
        asyncio.run(sim.close())
