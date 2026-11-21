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
        assert "Register" in _buttons(announcement)  # late sign-ups are still taken

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


def test_adding_a_name_only_captain_is_refused(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=1, captains=1)
    try:
        reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Dummy1",
                                    captain="want")).sent
        assert "Only players with a linked account can captain" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        names = [s.name for s in lobby_db.get_signups(conn, tournament["id"])]
        conn.close()
        assert "Dummy1" not in names
    finally:
        asyncio.run(sim.close())


def test_opting_out_then_choosing_to_captain_is_refused_and_asked_again(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=0)
    try:
        newcomer = sim.add_user(500, "Late")
        reply = asyncio.run(_sign_up_by_button(sim, newcomer, announcement, "want", agree=False))
        assert "Only players with a linked account can captain" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        assert lobby_db.get_signups(conn, tournament["id"]) == []
        asyncio.run(sim.choose(newcomer, reply, "no"))
        assert [s.captain_pref for s in lobby_db.get_signups(conn, tournament["id"])] == ["no"]
        conn.close()
    finally:
        asyncio.run(sim.close())


def test_the_commands_captain_choice_is_used_after_the_consent_notice(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=0)
    try:
        newcomer = sim.add_user(500, "Late")
        reply = asyncio.run(sim.run(newcomer, "register", tournament="Duel", captain="can")).sent
        asyncio.run(sim.click(newcomer, reply, "Agree"))
        assert "Can captain if needed" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        assert [s.captain_pref for s in lobby_db.get_signups(conn, tournament["id"])] == ["can"]
        conn.close()
    finally:
        asyncio.run(sim.close())


def test_adding_without_a_captain_choice_asks_for_it(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=0)
    try:
        reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Dummy1")).sent
        assert "Will **Dummy1** captain?" in reply.content
        asyncio.run(sim.choose(organizer, reply, "can"))
        assert "Only players with a linked account can captain" in reply.content
        asyncio.run(sim.choose(organizer, reply, "no"))
        assert "Signed up Dummy1" in reply.content and "Not captain" in reply.content
    finally:
        asyncio.run(sim.close())


def test_an_organizer_can_add_to_the_waitlist_after_the_draw(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=10, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Late1",
                                    captain="no")).sent
        assert "#1 on the waitlist" in reply.content
        _loop(sim)
        assert "Late1" in _post_with(sim, "🎌").content
    finally:
        asyncio.run(sim.close())


def test_registration_commands_work_until_check_in_ends_then_point_to_dropouts(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=11, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        lobby = lobby_db.get_lobbies(conn, tournament["id"])[0]
        waiting = lobby_db.get_waitlist(conn, tournament["id"])
        leaving = next(p for p in lobby.players if p not in lobby.captains)
        name = next(s.name for s in lobby_db.get_signups(conn, tournament["id"]) if s.team_id == leaving)
        conn.close()
        # After the draw, removing a player fills their spot in place from the waitlist.
        reply = asyncio.run(sim.run(organizer, "registration remove", tournament="Duel", entrant=name)).sent
        assert "takes the spot in lobby 1" in reply.content
        conn = db.get_connection()
        assert lobby_db.get_lobby(conn, lobby.id).players == [waiting[0] if p == leaving else p for p in lobby.players]
        conn.execute("UPDATE tournaments SET checkin_ends_at = ?, checkin_finished = 1 WHERE id = ?",
                     (db.to_db_time(datetime.now(timezone.utc)), tournament["id"]))
        conn.commit()
        conn.close()
        # Once check-in is over, they're refused with what to use instead.
        reply = asyncio.run(sim.run(organizer, "registration remove", tournament="Duel", entrant="Player2")).sent
        assert "/admin withdraw" in reply.content
        reply = asyncio.run(sim.run(users[0], "unregister", tournament="Duel")).sent
        assert "/match dropout" in reply.content
    finally:
        asyncio.run(sim.close())


def test_someone_who_left_after_the_draw_can_sign_up_again(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Late1", captain="no"))
        asyncio.run(sim.run(organizer, "registration remove", tournament="Duel", entrant="Late1"))
        reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Late1",
                                    captain="no")).sent
        assert "Brought Late1 back" in reply.content and "in lobby 1" in reply.content
        # A member who dropped out signs themselves up again the same way.
        player = users[-1]
        asyncio.run(sim.run(player, "unregister", tournament="Duel"))
        reply = asyncio.run(sim.run(player, "register", tournament="Duel", captain="no")).sent
        assert "You're in lobby 1" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        team_id = db.get_entrant_team_id(conn, tournament["id"], player.id)
        assert not db.get_teams(conn, tournament["id"])[team_id]["withdrawn"]
        conn.close()
    finally:
        asyncio.run(sim.close())


def _drawn_without_a_captain(tmp_path, captains=2):
    """A drawn 4v3 lobby whose first captain has left, so their team has no captain."""
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=captains)
    preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
    asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
    conn = db.get_connection()
    tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
    lobby = lobby_db.get_lobbies(conn, tournament["id"])[0]
    user_of = {team_id: members[0] for team_id, members in db.get_members_by_team(conn, tournament["id"]).items()}
    conn.close()
    leaving = next(u for u in users if u.id == user_of[lobby.captains[0]])
    asyncio.run(sim.run(leaving, "unregister", tournament="Duel"))
    _loop(sim)
    return sim, organizer, users, tournament["id"]


def _lobby(tournament_id):
    conn = db.get_connection()
    try:
        return lobby_db.get_lobbies(conn, tournament_id)[0]
    finally:
        conn.close()


def _newcomer(sim, user_id, name):
    user = sim.add_user(user_id, name)
    conn = db.get_connection()
    db.record_agreement(conn, GUILD_ID, user_id)
    conn.close()
    return user


def test_a_team_without_a_captain_holds_its_spot_for_one(tmp_path):
    sim, organizer, users, tournament_id = _drawn_without_a_captain(tmp_path)
    try:
        assert "needs a captain: its empty spot is held" in _post_with(sim, "🎌").content
        # Someone who can't captain doesn't take the held spot.
        asyncio.run(sim.run(_newcomer(sim, 500, "NoCaptain"), "register", tournament="Duel", captain="no"))
        lobby = _lobby(tournament_id)
        assert len(lobby.captains) == 1
        asyncio.run(sim.run(_newcomer(sim, 501, "NewCaptain"), "register", tournament="Duel", captain="want"))
        conn = db.get_connection()
        assert db.get_entrant_team_id(conn, tournament_id, 501) in _lobby(tournament_id).captains
        conn.close()
        _loop(sim)
        assert "needs a captain" not in _post_with(sim, "🎌").content
    finally:
        asyncio.run(sim.close())


def test_check_in_cant_start_until_every_team_has_a_captain(tmp_path):
    sim, organizer, users, tournament_id = _drawn_without_a_captain(tmp_path)
    try:
        reply = asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5)).sent
        assert "has no captain" in reply.content and "Check-in can't start" in reply.content
        click = asyncio.run(sim.click(organizer, _post_with(sim, "🎌"), "Start Check-In"))
        assert click.response.modal is None and "Check-in can't start" in click.sent.content
        asyncio.run(sim.run(_newcomer(sim, 501, "NewCaptain"), "register", tournament="Duel", captain="want"))
        reply = asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5)).sent
        assert "is open for 5 minutes" in reply.content
    finally:
        asyncio.run(sim.close())


def test_check_in_waits_when_a_captain_leaves_during_it(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5))
        tournament_id = _tournament_id()
        lobby = _lobby(tournament_id)
        conn = db.get_connection()
        user_of = {team_id: members[0] for team_id, members in db.get_members_by_team(conn, tournament_id).items()}
        conn.close()
        leaving = next(u for u in users if u.id == user_of[lobby.captains[0]])
        asyncio.run(sim.run(leaving, "unregister", tournament="Duel"))
        conn = db.get_connection()
        conn.execute("UPDATE tournaments SET checkin_ends_at = ? WHERE id = ?",
                     (db.to_db_time(datetime.now(timezone.utc) - timedelta(seconds=1)), tournament_id))
        conn.commit()
        conn.close()
        _loop(sim)
        assert "open until every team has a captain" in _post_with(sim, "🎌").content
        asyncio.run(sim.run(_newcomer(sim, 501, "NewCaptain"), "register", tournament="Duel", captain="want"))
        _loop(sim)
        conn = db.get_connection()
        assert db.get_tournament(conn, tournament_id)["checkin_finished"]
        conn.close()
    finally:
        asyncio.run(sim.close())


def test_an_organizer_is_told_to_move_a_spare_captain_over(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        conn = db.get_connection()
        tournament = db.get_tournament_by_name(conn, GUILD_ID, "Duel")
        lobby = lobby_db.get_lobbies(conn, tournament["id"])[0]
        keeping = lobby.side_of(lobby.captains[1])
        # A second player who can captain on the team that keeps its captain.
        spare = next(p for p in lobby.teams[keeping].players if p != lobby.teams[keeping].captain)
        lobby_db.set_captain_pref(conn, tournament["id"], spare, "can")
        user_of = {team_id: members[0] for team_id, members in db.get_members_by_team(conn, tournament["id"]).items()}
        conn.close()
        leaving = next(u for u in users if u.id == user_of[lobby.captains[0]])
        asyncio.run(sim.run(leaving, "unregister", tournament="Duel"))
        reply = asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5)).sent
        assert "/admin lobbymove" in reply.content
        mover = reply.content.split("entrant:")[1].split("`")[0]
        reply = asyncio.run(sim.run(organizer, "admin lobbymove", tournament="Duel", entrant=mover)).sent
        assert "moved to" in reply.content
        lobby = _lobby(tournament["id"])
        assert len(lobby.captains) == 2 and spare in lobby.captains
    finally:
        asyncio.run(sim.close())


def test_the_sign_up_count_leaves_out_players_who_left_after_the_draw(tmp_path):
    sim, organizer, users, tournament_id = _drawn_without_a_captain(tmp_path)
    try:
        assert "**6** signed up so far" in _post_with(sim, "📝").content
    finally:
        asyncio.run(sim.close())


def test_starting_up_brings_an_old_sign_ups_post_up_to_date(tmp_path):
    sim, organizer, users, tournament_id = _drawn_without_a_captain(tmp_path)
    try:
        post = _post_with(sim, "📝")
        post.content = "📝 an older version's wording: **7** signed up so far."
        cog = sim.cogs["LobbiesCog"]
        asyncio.run(sim.run_background("startup", lambda: cog._wait_until_ready()))
        assert "**6** signed up so far" in _post_with(sim, "📝").content
    finally:
        asyncio.run(sim.close())


def test_waiting_while_a_team_without_a_captain_has_room_says_why(tmp_path):
    sim, organizer, users, tournament_id = _drawn_without_a_captain(tmp_path)
    try:
        for n in range(1, 4):
            reply = asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name=f"Late{n}",
                                        captain="no")).sent
            if "waitlist" in reply.content:
                break
        assert "#1 on the waitlist" in reply.content
        assert "room, but only for a player who can captain, since it has no captain" in reply.content
    finally:
        asyncio.run(sim.close())



def _tournament_id():
    conn = db.get_connection()
    try:
        return db.get_tournament_by_name(conn, GUILD_ID, "Duel")["id"]
    finally:
        conn.close()


def test_an_organizer_can_call_off_check_in_and_check_someone_in(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        reply = asyncio.run(sim.run(organizer, "admin checkin", tournament="Duel", entrant="Player3")).sent
        assert "isn't open" in reply.content
        asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5))
        asyncio.run(sim.run(users[3], "lobby cancelcheckin", tournament="Duel"))  # not an organizer
        conn = db.get_connection()
        assert lobby_db.checkin_running(db.get_tournament(conn, _tournament_id()))
        conn.close()
        reply = asyncio.run(sim.run(organizer, "admin checkin", tournament="Duel", entrant="Player3")).sent
        assert "Checked in Player3 (lobby 1)" in reply.content
        reply = asyncio.run(sim.run(organizer, "admin checkin", tournament="Duel", entrant="Player3")).sent
        assert "already checked in" in reply.content
        reply = asyncio.run(sim.run(organizer, "lobby cancelcheckin", tournament="Duel")).sent
        assert "called off" in reply.content
        conn = db.get_connection()
        tournament = db.get_tournament(conn, _tournament_id())
        assert tournament["checkin_ends_at"] is None
        assert not any(s.checked_in for s in lobby_db.get_signups(conn, tournament["id"]) if s.user_id is not None)
        conn.close()
        _loop(sim)
        assert "Start Check-In" in _buttons(_post_with(sim, "🎌"))
        reply = asyncio.run(sim.run(organizer, "lobby cancelcheckin", tournament="Duel")).sent
        assert "isn't open" in reply.content
    finally:
        asyncio.run(sim.close())


def test_checking_in_twice_says_so(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=7, captains=2)
    try:
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5))
        assert "✅ Checked in for lobby 1" in asyncio.run(sim.click(users[0], _post_with(sim, "🎌"), "Check In")).sent.content
        again = asyncio.run(sim.click(users[0], _post_with(sim, "🎌"), "Check In")).sent.content
        assert "You're already checked in for lobby 1" in again
    finally:
        asyncio.run(sim.close())


def test_check_in_marks_only_players_who_can_check_in(tmp_path):
    sim, organizer, users, announcement = _event(tmp_path, players=6, captains=2)
    try:
        asyncio.run(sim.run(organizer, "registration add", tournament="Duel", name="Dummy1", captain="no"))
        preview = asyncio.run(sim.run(organizer, "registration close", name="Duel")).sent
        asyncio.run(sim.click(organizer, preview, "Create Lobbies"))
        asyncio.run(sim.run(organizer, "lobby startcheckin", tournament="Duel", minutes=5))
        post = _post_with(sim, "🎌").content
        assert "**Dummy1**" in post and "**Dummy1** ✅" not in post and "**Dummy1** ⏳" not in post
        assert f"<@{users[0].id}> (captain) ⏳" in post
        asyncio.run(sim.click(users[0], _post_with(sim, "🎌"), "Check In"))
        _loop(sim)
        assert f"<@{users[0].id}> (captain) ✅" in _post_with(sim, "🎌").content
    finally:
        asyncio.run(sim.close())
