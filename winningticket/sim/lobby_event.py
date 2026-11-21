"""A simulated 5v5 event, played through the real cogs from sign-ups to the conclude post.
Players sign up by button or command, one drops out, a guest is disqualified, some miss
check-in, captains host, finish and enter the points (sometimes tied, mismatched or left to
one captain), and organizers fix lobbies by hand."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import db
import lobby_db
import lobby_posts
from sim.actors import Organizer, Player
from sim.harness import GUILD_ID, Simulation

if TYPE_CHECKING:
    from sim.scenarios import Scenario

NAME = "Draft Night"


def _buttons(message) -> list[str]:
    if message is None or message.view is None:
        return []
    items = [getattr(item, "item", item) for item in message.view.children]
    return [item.label for item in items if hasattr(item, "label")]


@dataclass
class LobbyEventRun:
    sim: Simulation
    scenario: Scenario
    rng: random.Random
    organizer: Organizer
    players: list[Player]
    tournament_id: Optional[int] = None

    # -- reading the bot's state ---------------------------------------------------------

    def _query(self, work):
        conn = db.get_connection()
        try:
            return work(conn)
        finally:
            conn.close()

    def status(self) -> Optional[str]:
        tournament = self._query(lambda conn: db.get_tournament(conn, self.tournament_id)) if self.tournament_id else None
        self.sim.stage = tournament["status"] if tournament else "-"
        return tournament["status"] if tournament else None

    def _lobbies(self) -> list[lobby_db.Lobby]:
        return self._query(lambda conn: lobby_db.get_lobbies(conn, self.tournament_id))

    def _user_of(self) -> dict[int, int]:
        members = self._query(lambda conn: db.get_members_by_team(conn, self.tournament_id))
        return {team_id: users[0] for team_id, users in members.items() if users}

    def _player(self, user_id: int) -> Optional[Player]:
        return next((player for player in self.players if player.user.id == user_id), None)

    def _post(self, text: str):
        return next((m for m in reversed(self.sim.channel.messages)
                     if m.public and not m.deleted and text in (m.content or "")), None)

    def _event_post(self):
        return self._post(f"🎌 **{NAME}**")

    def _results_post(self, lobby: lobby_db.Lobby):
        return self._post(f"📋 **{NAME} · Lobby {lobby.number} results**")

    async def _tend(self) -> None:
        cog = self.sim.cogs["LobbiesCog"]
        await self.sim.run_background("5v5 check", lambda: cog.tend_lobbies.coro(cog))

    def _shift(self, sql: str) -> None:
        """Move a stored time back, standing in for minutes going by."""
        conn = db.get_connection()
        with conn:
            conn.execute(sql, (self.tournament_id,))
        conn.close()

    # -- the event -----------------------------------------------------------------------

    async def play(self) -> None:
        org = self.organizer.user
        await self.sim.run(org, "registration open5v5", name=NAME, starts_in_hours=30.0,
                           spread=self.rng.choice(["tier", "balanced", "random"]))
        self.tournament_id = self._query(lambda conn: db.get_tournament_by_name(conn, GUILD_ID, NAME))["id"]
        self.status()
        announcement = self._post("Sign-ups are open")
        late = self.players[-2:]
        for player in self.players[:-2]:
            await self._sign_up(player, announcement)
        for number in range(1, self.scenario.guests + 1):
            if self.rng.random() < 0.5:
                await self.sim.run(org, "registration add", tournament=NAME, user=None, name=f"Guest {number}",
                                   captain="no")
                continue
            # Left out, the captain choice is asked for; a guest can't captain, so "want" is refused.
            reply = (await self.sim.run(org, "registration add", tournament=NAME, user=None,
                                        name=f"Guest {number}")).sent
            if reply is not None and reply.view is not None:
                await self.sim.choose(org, reply, self.rng.choice(["want", "no"]))
            if reply is not None and reply.view is not None:
                await self.sim.choose(org, reply, "no")
        await self.sim.run(self.players[0].user, "registration entrants", name=NAME)
        await self._close()
        # A guest who turns up after the draw fills a team with room, or waits.
        await self.sim.run(org, "registration add", tournament=NAME, user=None, name="Late guest", captain="no")
        for player in late:
            await self._sign_up(player, announcement)  # a team with room, or the waitlist
        await self._early_dropout()
        await self._organizer_fixes_before_checkin()
        await self._check_in()
        await self._organizer_fixes_after_checkin()
        await self._play_lobbies()
        for _ in range(10):
            if self.status() != "in_progress":
                break
            await self._settle_disputes()
            self._shift("UPDATE lobby_entries SET changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', changed_at, '-11 minutes') "
                        "WHERE lobby_id IN (SELECT id FROM lobbies WHERE tournament_id = ?)")
            await self._tend()
        await self.sim.run(org, "tournament conclude", name=NAME)
        self.status()
        for player in self.rng.sample(self.players, 2):
            await self.sim.run(player.user, "rating")
        await self.sim.run(self.players[0].user, "history bracket", tournament=NAME)

    async def _sign_up(self, player: Player, announcement) -> None:
        competence = player.competence
        pref = self.rng.choices(["want", "can", "no"], weights=[2, 2, 5])[0]
        if self.rng.random() < 0.5 and announcement is not None and "Register" in _buttons(announcement):
            reply = (await self.sim.click(player.user, announcement, "Register")).sent
        else:
            reply = (await self.sim.run(player.user, "register", tournament=NAME,
                                        captain=pref if self.rng.random() < 0.5 else None)).sent
        if reply is not None and "Agree" in _buttons(reply):
            await self.sim.click(player.user, reply, "Agree" if player.agrees else "Opt Out")
        if reply is not None and reply.view is not None:
            await self.sim.choose(player.user, reply, pref)
        if reply is not None and reply.view is not None and "linked account can captain" in (reply.content or ""):
            await self.sim.choose(player.user, reply, "no")  # opted out, so they can't captain
        if self.rng.random() < competence.double_click:
            await self.sim.run(player.user, "register", tournament=NAME, captain="can")  # changes their mind

    async def _close(self) -> None:
        org = self.organizer.user
        if self.rng.random() < self.organizer.style.wrong_order:
            await self.sim.run(org, "lobby startcheckin", tournament=NAME)  # before there are lobbies
        preview = (await self.sim.run(org, "registration close", name=NAME)).sent
        if preview is None or "Create Lobbies" not in _buttons(preview):
            self.sim._problem("closing sign-ups showed no lobby preview")
            return
        menus = [item for item in preview.view.children if hasattr(item, "options")]
        if menus and len(menus[0].options) > 1 and self.rng.random() < 0.5:
            await self.sim.choose(self.organizer.user, preview, menus[0].options[0].value)
        await self.sim.click(org, preview, "Create Lobbies")
        await self.sim.click(org, preview, "Create Lobbies", late=True)  # a double-click
        self.status()
        await self._tend()

    async def _early_dropout(self) -> None:
        user_of = self._user_of()
        placed = [user_of[p] for lobby in self._lobbies() for p in lobby.players if p in user_of]
        quitter = self._player(self.rng.choice(placed))
        await self.sim.run(quitter.user, "match dropout", tournament=NAME, confirm_name=NAME)
        await self._tend()

    async def _organizer_fixes_before_checkin(self) -> None:
        """A guest is disqualified, a waiting player changes their mind, and a member pokes
        at an organizer command."""
        org = self.organizer.user
        if self.scenario.guests:
            await self.sim.run(org, "admin withdraw", tournament=NAME, entrant="Guest 1", reason="disqualified")
        user_of = self._user_of()
        waiting = [user_of[t] for t in self._query(lambda conn: lobby_db.get_waitlist(conn, self.tournament_id))
                   if t in user_of]
        if len(waiting) > 1:
            await self.sim.run(self._player(waiting[-1]).user, "unregister", tournament=NAME)
        await self.sim.run(self.rng.choice(self.players).user, "lobby startcheckin", tournament=NAME)  # refused
        await self._tend()

    async def _organizer_fixes_after_checkin(self) -> None:
        """The organizer brings a waiting player in by hand, hands one team's captaincy to
        someone else, and checks the sign-up list."""
        org = self.organizer.user
        names = self._query(lambda conn: {t: row["name"] for t, row in db.get_teams(conn, self.tournament_id).items()})
        user_of = self._user_of()
        lobbies = self._lobbies()
        waiting = self._query(lambda conn: lobby_db.get_waitlist(conn, self.tournament_id))
        if waiting:
            leaving = next(p for p in lobbies[-1].players if p not in lobbies[-1].captains)
            await self.sim.run(org, "admin lobbyswap", tournament=NAME, leaving=names[leaving], joining=names[waiting[0]])
        lobby = lobbies[0]
        side = self.rng.choice("AB")
        linked = [p for p in lobby.teams[side].players if p in user_of and p != lobby.teams[side].captain]
        if linked:
            await self.sim.run(org, "admin setcaptain", tournament=NAME, entrant=names[linked[0]])
        await self.sim.run(org, "registration entrants", name=NAME)
        await self._tend()

    async def _check_in(self) -> None:
        event_post = self._event_post()
        outsider = self.rng.choice(self.players)
        await self.sim.click(outsider.user, event_post, "Start Check-In")  # only organizers can
        if self._query(lambda conn: lobby_db.teams_without_captain(conn, self.tournament_id)):
            # Refused until every team has a captain, so the organizer gives them one first.
            await self.sim.click(self.organizer.user, event_post, "Start Check-In")
            await self._give_teams_captains()
            event_post = self._event_post()
        click = await self.sim.click(self.organizer.user, event_post, "Start Check-In")
        if click is None or click.response.modal is None:
            self.sim._problem("Start Check-In didn't open its form")
            return
        await self.sim.submit_fields(self.organizer.user, click.response.modal, ["15"])
        await self._tend()
        await self.sim.run(outsider.user, "lobby cancelcheckin", tournament=NAME)  # only organizers can
        if self.rng.random() < 0.3:
            # Started too early: the organizer calls it off and starts it again.
            await self.sim.run(self.organizer.user, "lobby cancelcheckin", tournament=NAME)
            await self._tend()
            await self.sim.run(self.organizer.user, "lobby startcheckin", tournament=NAME)
            await self._tend()
        user_of = self._user_of()
        lobbies = self._lobbies()
        waitlist = self._query(lambda conn: lobby_db.get_waitlist(conn, self.tournament_id))
        # A captain and a couple of others miss check-in; everyone waiting checks in.
        captains = [p for lobby in lobbies for p in lobby.captains]
        missing = set(self.rng.sample(captains, 1))
        others = [p for lobby in lobbies for p in lobby.players if p not in captains]
        missing |= set(self.rng.sample(others, min(2, len(others))))
        names = self._query(lambda conn: {t: row["name"] for t, row in db.get_teams(conn, self.tournament_id).items()})
        if others and self.rng.random() < 0.5:
            # A player says in chat they're here but can't press the button: an organizer checks them in.
            helped = self.rng.choice(others)
            await self.sim.run(self.organizer.user, "admin checkin", tournament=NAME, entrant=names[helped])
            missing.discard(helped)
        for lobby in lobbies:
            post = self._event_post()
            for player in lobby.players:
                if player in user_of and player not in missing:
                    await self.sim.click(self._player(user_of[player]).user, post, "Check In")
        for team_id in waitlist:
            if team_id in user_of:
                await self.sim.run(self._player(user_of[team_id]).user, "checkin", tournament=NAME)
        self._shift("UPDATE tournaments SET checkin_ends_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 second') "
                    "WHERE id = ?")
        await self._tend()
        await self._give_teams_captains()

    async def _give_teams_captains(self) -> None:
        """Check-in can't start or finish while a team has no captain, so the organizer gives
        one to any team left without: moving a spare captain over, or making a linked player
        captain."""
        org = self.organizer.user
        for _ in range(5):
            missing = self._query(lambda conn: lobby_db.teams_without_captain(conn, self.tournament_id))
            if not missing:
                return
            lobby, side = missing[0]
            other = "B" if side == "A" else "A"
            signups = {s.team_id: s for s in self._query(lambda conn: lobby_db.get_signups(conn, self.tournament_id))}
            spare = [p for p in lobby.teams[other].players
                     if signups[p].can_captain and p != lobby.teams[other].captain]
            linked = [p for p in lobby.teams[side].players if signups[p].user_id is not None]
            if spare:
                await self.sim.run(org, "admin lobbymove", tournament=NAME, entrant=signups[spare[0]].name)
            elif linked:
                await self.sim.run(org, "admin setcaptain", tournament=NAME, entrant=signups[linked[0]].name)
            else:
                # Nobody there can captain: move the other team's linked player over and make them captain.
                mover = next(p for p in lobby.teams[other].players
                             if signups[p].user_id is not None and p != lobby.teams[other].captain)
                await self.sim.run(org, "admin lobbymove", tournament=NAME, entrant=signups[mover].name)
                await self.sim.run(org, "admin setcaptain", tournament=NAME, entrant=signups[mover].name)
            await self._tend()
        self.sim._problem("a team still has no captain, so check-in can't finish")

    async def _play_lobbies(self) -> None:
        user_of = self._user_of()
        for lobby in self._lobbies():
            post = self._event_post()
            captains = [self._player(user_of[c]) for c in lobby.captains if c in user_of]
            if len(captains) < 2:
                continue  # an organizer settles a lobby without two captains
            host, other = self.rng.sample(captains, 2)
            players = [self._player(user_of[p]) for p in lobby.players if p in user_of]
            outsider = next((p for p in self.players if p.user.id not in {pl.user.id for pl in players}), None)
            if outsider is not None:
                await self.sim.click(outsider.user, post, "Get Room Code")  # refused
            await self.sim.click(self.rng.choice(players).user, post, "Get Room Code")  # not in yet
            if self.rng.random() < 0.3:
                await self.sim.click(host.user, post, lobby_posts.FINISH)  # refused: no room yet
            if self.rng.random() < 0.3:
                # The host's room is set up by an organizer, who gives the code by command.
                await self.sim.run(self.organizer.user, "lobby roomcode", tournament=NAME, code=f"ORG{lobby.number}",
                                   lobby=lobby.number)
            else:
                form = await self.sim.click(host.user, post, "Submit Room Code")
                if form is None or form.response.modal is None:
                    self.sim._problem(f"lobby {lobby.number}'s Submit Room Code didn't open its form")
                    continue
                await self.sim.submit_fields(host.user, form.response.modal, [f"ROOM{lobby.number}"])
                # The host dismissed the reply and asks again for who to pass the code to.
                await self.sim.run(host.user, "lobby code", tournament=NAME)
            post = self._event_post() or post
            for player in players:
                await self.sim.click(player.user, post, "Get Room Code")
            await self._finish(host, other, lobby)
            await self._enter_results(host, other, lobby)

    def _points(self, lobby: lobby_db.Lobby, tied: bool = False) -> dict[int, str]:
        if tied:
            return {p: "5" for p in lobby.players}
        return {p: str(self.rng.randint(0, 20)) for p in lobby.players}

    async def _finish(self, host: Player, other: Player, lobby: lobby_db.Lobby) -> None:
        """The races are over: the host presses Finish, then the other captain does, or
        sometimes an organizer finishes the lobby for them."""
        await self.sim.click(host.user, self._event_post(), lobby_posts.FINISH)
        await self.sim.click(host.user, self._event_post(), lobby_posts.FINISH)  # already pressed
        if self.rng.random() < 0.2:
            await self.sim.run(self.organizer.user, "lobby finish", tournament=NAME, lobby=lobby.number)
        else:
            await self.sim.click(other.user, self._event_post(), lobby_posts.FINISH)
        await self._tend()

    def _labels(self, lobby: lobby_db.Lobby) -> dict[str, str]:
        names = self._query(lambda conn: {t: row["name"] for t, row in db.get_teams(conn, self.tournament_id).items()})
        return {side: lobby_posts.points_button_label(names[lobby.teams[side].captain]) for side in lobby_db.SIDES}

    def _winning_points(self, lobby: lobby_db.Lobby) -> dict[int, str]:
        points = self._points(lobby)
        if lobby_db.winning_side(lobby, {p: int(v) for p, v in points.items()}) is None:
            points[lobby.players[0]] = str(int(points[lobby.players[0]]) + 1)
        return points

    async def _enter_results(self, host: Player, other: Player, lobby: lobby_db.Lobby) -> None:
        """Each captain enters everyone's points on the results post: they match, or one is
        wrong and corrects theirs, or they're left for an organizer, or only one captain
        enters any and they're accepted when the other's time runs out."""
        post = self._results_post(lobby)
        if post is None:
            self.sim._problem(f"lobby {lobby.number} has no results post after both captains finished")
            return
        labels = self._labels(lobby)
        sides = {player.user.id: side for side in lobby_db.SIDES for player in (host, other)
                 if self._user_of().get(lobby.teams[side].captain) == player.user.id}
        await self.sim.click(host.user, post, labels["B" if sides.get(host.user.id) == "A" else "A"])  # not theirs
        points = self._winning_points(lobby)
        if self.rng.random() < 0.3:
            await self._enter_points(host, labels[sides[host.user.id]], lobby, self._points(lobby, tied=True))
        await self._enter_points(host, labels[sides[host.user.id]], lobby, points)
        outcome = self.rng.choice(["match", "corrected", "organizer", "alone"])
        if outcome == "alone":
            return
        if outcome == "match":
            await self._enter_points(other, labels[sides[other.user.id]], lobby, points)
            return
        wrong = dict(points)
        wrong[lobby.players[0]] = str(int(wrong[lobby.players[0]]) + 50)
        if lobby_db.winning_side(lobby, {p: int(v) for p, v in wrong.items()}) is None:
            wrong[lobby.players[1]] = str(int(wrong[lobby.players[1]]) + 1)
        await self._enter_points(other, labels[sides[other.user.id]], lobby, wrong)
        if outcome == "corrected":
            await self._enter_points(other, labels[sides[other.user.id]], lobby, points)

    async def _enter_points(self, captain: Player, label: str, lobby: lobby_db.Lobby, points: dict[int, str],
                            organizer: bool = False) -> None:
        await self._tend()
        user = self.organizer.user if organizer else captain.user
        click = await self.sim.click(user, self._results_post(lobby), label)
        if click is None or click.response.modal is None:
            return
        await self._submit_points(user, click.response.modal, lobby, points)

    async def _submit_points(self, user, modal, lobby: lobby_db.Lobby, points: dict[int, str]) -> None:
        first = await self.sim.submit_fields(user, modal, [points[p] for p in lobby.teams["A"].players])
        between = first.sent if first is not None else None
        if between is None or not _buttons(between):
            return
        second = await self.sim.click(user, between, _buttons(between)[0])
        await self.sim.submit_fields(user, second.response.modal, [points[p] for p in lobby.teams["B"].players])

    async def _settle_disputes(self) -> None:
        """The organizer settles lobbies whose captains' points don't match with Finalize
        Scores, and records any lobby still without a result by command."""
        for lobby in self._lobbies():
            if self._query(lambda conn: lobby_db.get_result(conn, lobby.id)) is not None:
                continue
            post = self._results_post(lobby)
            points = self._winning_points(lobby)
            if post is not None and lobby_posts.FINALIZE in _buttons(post):
                await self._enter_points(self.organizer, lobby_posts.FINALIZE, lobby, points, organizer=True)
                continue
            if self.rng.random() < 0.5:
                continue  # the captains' time to enter theirs hasn't run out yet
            reply = await self.sim.run(self.organizer.user, "lobby report", tournament=NAME, lobby=lobby.number)
            if reply.response.modal is not None:
                await self._submit_points(self.organizer.user, reply.response.modal, lobby, points)

    def check_final_state(self) -> None:
        self.sim._begin(None, "final checks")
        try:
            tournament = self._query(lambda conn: db.get_tournament(conn, self.tournament_id))
            if tournament["status"] != "concluded":
                self.sim._problem(f"the 5v5 event ended at status {tournament['status']}, not concluded")
            for lobby in self._lobbies():
                if self._query(lambda conn: lobby_db.get_result(conn, lobby.id)) is None:
                    self.sim._problem(f"lobby {lobby.number} has no result")
        finally:
            self.sim._after_step()
