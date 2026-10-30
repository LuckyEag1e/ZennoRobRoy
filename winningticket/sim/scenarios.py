"""Scenarios and the run that plays one from sign-ups to conclusion.

Real match results come from each entrant's hidden skill, so a run knows the true
champion and every true result. It checks the bot's final state against them.
"""

from __future__ import annotations

import random
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord

import db
from bracket import bracket_order, final_match, is_bye, resolve_all_slots, resolve_slot
from labels import team_label
from ratings import BASE_RATING, compute_ratings, entrant_rating
from seeding import seeded_pairs
from sim.actors import (
    CAREFUL,
    CASUAL,
    CHAOTIC,
    DILIGENT,
    SLOPPY,
    SPAMMER,
    Competence,
    Organizer,
    OrganizerStyle,
    Player,
    guessed_winner,
)
from cogs.tournament import LIVE_POST_IN_VIEW, render_live_post
from sim.discord_fakes import FakeAttachment
from sim.harness import GUILD_ID, Simulation

CONFIRM_BRACKET = "Confirm & Save Bracket"
# A spammer's burst: about as many commands as one person can fire before Discord slows them.
SPAM_BURST = 10
# The most new public messages one member should be able to cause in a burst; more fills the channel.
NOISE_LIMIT = 2
START_OVER = "Start Over"
# How many players agree to link their data when they first sign up.
AGREES_TO_LINK = 0.8

# One per tournament in a scenario, since a concluded tournament's name can't be reused.
TOURNAMENT_NAMES = ["Spring Cup", "Summer Cup", "Autumn Cup", "Winter Cup"]

NAMES = [
    "Ace", "Blaze", "Comet", "Dash", "Echo", "Flint", "Gale", "Haze", "Iris", "Jinx", "Kite", "Lumen", "Mist",
    "Nova", "Onyx", "Pike", "Quill", "Rook", "Sage", "Tide", "Umber", "Vale", "Wren", "Xeno", "Yarrow", "Zephyr",
]


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    players: tuple[tuple[Competence, int], ...]
    organizer: OrganizerStyle
    guests: int = 0               # entrants the organizer adds by name, with no Discord account
    entry: str = "signups"         # "signups", or "csv" for a bracket uploaded with /tournament create
    turns_per_stage: int = 3
    duplicate_names: bool = False  # two players share a display name
    crowded: bool = False          # players act at the same time, so their steps interleave
    simultaneous: bool = False     # and the bot handles their interactions at the same instant
    co_organizer: bool = False     # a second organizer records results alongside the first
    tournaments: int = 1           # played one after another by the same people, so later ones are seeded by rating
    format: str = "single_elim"    # the only format the bot supports so far


SCENARIOS = {
    scenario.name: scenario for scenario in [
        Scenario("smooth", "8 diligent players and a careful organizer: nothing should go wrong.",
                 ((DILIGENT, 8),), CAREFUL),
        Scenario("typical", "A realistic mix acting at the same time, with a couple of chaotic players.",
                 ((DILIGENT, 4), (CASUAL, 6), (CHAOTIC, 2)), CAREFUL, guests=1, duplicate_names=True, crowded=True, simultaneous=True),
        Scenario("chaos", "Mostly chaotic players at once, and two sloppy organizers recording results together.",
                 ((DILIGENT, 2), (CASUAL, 3), (CHAOTIC, 7)), SLOPPY, guests=2, duplicate_names=True,
                 crowded=True, simultaneous=True, co_organizer=True),
        Scenario("large", "40 players and 3 guests at once, for a 64-line bracket and long menus.",
                 ((DILIGENT, 10), (CASUAL, 25), (CHAOTIC, 5)), CAREFUL, guests=3, turns_per_stage=2, crowded=True, simultaneous=True),
        Scenario("spam", "Ordinary players at once, plus one member firing public-posting commands as fast as they can.",
                 ((CASUAL, 6), (SPAMMER, 1)), CAREFUL, guests=1, turns_per_stage=2, crowded=True, simultaneous=True),
        Scenario("csv", "8 named teams uploaded as a CSV, with two organizers recording results together.",
                 ((DILIGENT, 3), (CASUAL, 3), (CHAOTIC, 2)), SLOPPY, guests=8, entry="csv", co_organizer=True),
        Scenario("season", "Two tournaments in a row: a guest joins Discord in between, and the second is seeded by rating.",
                 ((DILIGENT, 4), (CASUAL, 3), (CHAOTIC, 1)), SLOPPY, guests=2, tournaments=2),
    ]
}


@dataclass
class TournamentRun:
    sim: Simulation
    scenario: Scenario
    rng: random.Random
    organizer: Organizer
    players: list[Player]
    name: str = "Spring Cup"
    tournament_id: Optional[int] = None
    co_organizer: Optional[Organizer] = None
    guest_skill: dict[str, float] = field(default_factory=dict)
    # The real winner of each match, by match id. Games are played by whoever really won
    # their way there, whatever the bot's bracket shows while a mistake is in it.
    real_winners: dict[int, int] = field(default_factory=dict)
    banned: Optional[Player] = None
    # Players who opted out when signing up, so they're entered under their name alone.
    name_only: set[int] = field(default_factory=set)
    # A guest from the last tournament who has since joined Discord, and the entrant name
    # the organizer links to them once they've agreed to it by signing up.
    newcomer: Optional[tuple[Player, str, str]] = None

    # -- reading the bot's state --------------------------------------------------------

    def _tournament(self, conn):
        if self.tournament_id is not None:
            return db.get_tournament(conn, self.tournament_id)
        return db.get_tournament_by_name(conn, GUILD_ID, self.name)

    def status(self) -> Optional[str]:
        conn = db.get_connection()
        try:
            tournament = self._tournament(conn)
            status = tournament["status"] if tournament else None
        finally:
            conn.close()
        self.sim.stage = status or "-"
        return status

    def is_registered(self, player: Player) -> bool:
        conn = db.get_connection()
        try:
            tournament = self._tournament(conn)
            if tournament is None:
                return False
            if player.user.id in self.name_only:
                entrant = db.get_team_by_name(conn, tournament["id"], player.user.display_name)
                return entrant is not None and not db.get_members_by_team(conn, tournament["id"])[entrant["id"]]
            return db.get_entrant_team_id(conn, tournament["id"], player.user.id) is not None
        finally:
            conn.close()

    def entrant_count(self) -> int:
        conn = db.get_connection()
        try:
            tournament = self._tournament(conn)
            return db.entrant_count(conn, tournament["id"]) if tournament else 0
        finally:
            conn.close()

    def has_predicted(self, player: Player) -> bool:
        conn = db.get_connection()
        try:
            return bool(db.get_user_predictions(conn, self.tournament_id, player.user.id))
        finally:
            conn.close()

    def _bracket(self):
        conn = db.get_connection()
        try:
            matches_by_id, feeders, teams_by_id = db.load_bracket(conn, self.tournament_id)
            return matches_by_id, feeders, teams_by_id, db.get_results(conn, self.tournament_id)
        finally:
            conn.close()

    def skill_of(self, team_id: int) -> float:
        conn = db.get_connection()
        try:
            team = db.get_teams(conn, self.tournament_id)[team_id]
            member = conn.execute("SELECT user_id FROM team_members WHERE team_id = ?", (team_id,)).fetchone()
        finally:
            conn.close()
        if member is None:
            return self.guest_skill[team["name"]]
        return next(p.skill for p in self.players if p.user.id == member["user_id"])

    async def tournament_name(self, player: Player, command: str, option: str = "tournament") -> str:
        """What a player puts in the tournament option: usually the autocomplete choice,
        sometimes their own typo."""
        await self.sim.autocomplete(player.user, command, option, self.name[:2])
        if self.rng.random() < player.competence.typo:
            return self.rng.choice([self.name.lower(), self.name[:-1], self.name + "s", self.name.replace(" ", "")])
        return self.name

    # -- things players do --------------------------------------------------------------

    async def register(self, player: Player, tournament: Optional[str] = None) -> None:
        """/register, answering the consent notice the first time. Someone who has agreed
        must never be asked again."""
        conn = db.get_connection()
        try:
            agreed_before = db.has_agreed(conn, GUILD_ID, player.user.id)
        finally:
            conn.close()
        reply = await self.sim.run(player.user, "register", tournament=tournament or self.name)
        message = reply.sent
        buttons = [item.label for item in message.view.children] if message is not None and message.view is not None else []
        if "Agree" not in buttons:
            return
        if agreed_before:
            self.sim._problem(f"{player.user.display_name} was asked for consent again after agreeing")
        await self.sim.click(player.user, message, "Agree" if player.agrees else "Opt Out")
        if not player.agrees and (message.content or "").startswith("✅"):
            self.name_only.add(player.user.id)
            self.guest_skill.setdefault(player.user.display_name, player.skill)

    async def predict(self, player: Player) -> None:
        reply = await self.sim.run(player.user, "predict", tournament=await self.tournament_name(player, "predict"))
        await self._fill_bracket(player, reply.sent)

    async def resume_bracket(self, player: Player) -> None:
        """Pick up a bracket they walked away from, which may be after predictions closed."""
        message, player.unfinished_bracket = player.unfinished_bracket, None
        await self.sim.narrate(f"{player.user.display_name} comes back to the bracket they left")
        await self._fill_bracket(player, message)

    async def _fill_bracket(self, player: Player, message) -> None:
        c, user = player.competence, player.user
        restarted = False
        _, _, teams_by_id, _ = self._bracket()
        team_by_label = {team_label(teams_by_id, team_id)[:80]: team_id for team_id in teams_by_id}
        while message is not None and message.view is not None:
            labels = [item.label for item in message.view.children if isinstance(item, discord.ui.Button)]
            if CONFIRM_BRACKET in labels:
                if not restarted and self.rng.random() < c.double_click:
                    restarted = True
                    clicked = await self.sim.click(user, message, START_OVER)
                    message = clicked.sent if clicked else None
                    continue
                await self.sim.click(user, message, CONFIRM_BRACKET)
                return
            if self.rng.random() < c.abandon / 4:
                await self.sim.narrate(f"{user.display_name} walks away from their bracket")
                player.unfinished_bracket = message
                return
            team_a, team_b = (team_by_label[label] for label in labels[:2])
            label = team_label(teams_by_id, guessed_winner(player, self, team_a, team_b, self.rng))[:80]
            clicked = await self.sim.click(user, message, label)
            if clicked is None:
                self.sim._problem(f"/predict showed no [{label}] button to click")
                return
            if self.rng.random() < c.double_click:
                await self.sim.click(user, message, label, late=True)
            message = clicked.sent

    async def match_stats(self, player: Player) -> None:
        reply = await self.sim.run(player.user, "match stats",
                                   tournament=await self.tournament_name(player, "match stats"))
        message = reply.sent
        menu = _menu(message)
        if menu is not None:
            await self.sim.choose(player.user, message, self.rng.choice(menu.options).value)

    async def look_back(self, player: Player) -> None:
        """Look at a concluded tournament with /history, which only the asker should see."""
        command = f"history {self.rng.choice(['list', 'bracket', 'seeding', 'leaderboard'])}"
        if command == "history list":
            reply = await self.sim.run(player.user, command)
            listed = reply.sent.embed.description if reply.sent is not None and reply.sent.embed is not None else None
            if listed is not None and self.name not in listed:  # a banned player gets no list at all
                self.sim._problem(f"/history list doesn't include {self.name}")
        else:
            reply = await self.sim.run(player.user, command, tournament=await self.tournament_name(player, command))
        if reply.sent is not None and reply.sent.public:
            self.sim._problem(f"/{command} posted in the channel instead of replying privately")

    async def check_history_autocomplete(self) -> None:
        """Once concluded, the tournament is offered by /history and by no other command."""
        org = self.organizer.user
        if self.name in await self.sim.autocomplete(org, "tournament bracket", "name", ""):
            self.sim._problem(f"/tournament bracket still offers {self.name} after it concluded")
        if self.name not in await self.sim.autocomplete(org, "history bracket", "tournament", ""):
            self.sim._problem(f"/history bracket doesn't offer {self.name} after it concluded")

    async def reuse_a_concluded_name(self, old_name: str) -> None:
        """An organizer tries to name the next tournament after the last one, which the bot
        must refuse without creating anything."""
        reply = await self.sim.run(self.organizer.user, "registration open", name=old_name)
        conn = db.get_connection()
        try:
            created = db.get_tournament_by_name(conn, GUILD_ID, old_name)
        finally:
            conn.close()
        if created is not None or reply.sent is None or "aren't reused" not in (reply.sent.content or ""):
            self.sim._problem(f"/registration open reused the concluded name {old_name}")

    async def look_up(self, player: Player, kind: str) -> None:
        """Ask for the bracket or leaderboard. If the bot points at its live post instead of
        posting one, check the post it points at exists and really is in view."""
        command = f"tournament {kind}"
        name = await self.tournament_name(player, command, "name")
        reply = await self.sim.run(player.user, command, name=name)
        pointer = re.search(r"kept up to date: \S+/(\d+)$", (reply.sent.content or "") if reply.sent else "")
        if pointer is None:
            return
        message_id = int(pointer.group(1))
        # Messages that arrive while the bot is answering can push the post up a little, and
        # someone else's request can replace it with a newer one. Either is fine: the post
        # has to be in view when they asked or when the reply reached them. It mustn't have
        # been deleted before the bot started answering, though being replaced while the
        # answer is on its way is something no bot can prevent.
        messages = self.sim.channel.messages
        reply_index = messages.index(reply.sent) if reply.sent in messages else len(messages)

        def showing(window: list) -> list[int]:
            visible = [m.id for m in window[::-1] if m.public and m.id not in reply.deleted_before_answer]
            return visible[:LIVE_POST_IN_VIEW]

        in_view = set(showing(messages[:reply.messages_before])) | set(showing(messages[:reply_index]))
        if message_id not in in_view:
            self.sim._problem(f"the bot pointed {player.user.display_name} at a {kind} post that isn't in view")

    async def refresh_live_posts(self) -> None:
        """Run the live-post refresh as the bot would."""
        cog = self.sim.cogs["TournamentCog"]
        await self.sim.run_background("live posts refresh", lambda: cog.refresh_live_posts.coro(cog))

    def check_live_posts(self) -> None:
        """Every live post should show its tournament as it is now. Run right after a refresh,
        while nothing else is changing."""
        conn = db.get_connection()
        try:
            for live in db.get_live_posts(conn):
                tournament = db.get_tournament(conn, live["tournament_id"])
                message = self.sim.channel.get_partial_message(live["message_id"])
                if message.embed is None or message.embed.to_dict() != render_live_post(conn, tournament, live["kind"]).to_dict():
                    self.sim._problem(f"the live {live['kind']} post is out of date after a refresh")
        finally:
            conn.close()

    async def spam_burst(self, player: Player) -> None:
        """Fire public-posting commands as fast as possible, then check how much of the
        channel it took over."""
        before = self.sim.public_posts[player.user.id]
        for _ in range(SPAM_BURST):
            if self.status() == "registration":
                if self.is_registered(player):
                    await self.sim.run(player.user, "unregister", tournament=self.name)
                else:
                    await self.register(player)
            else:
                await self.look_up(player, self.rng.choice(["bracket", "leaderboard"]))
        caused = self.sim.public_posts[player.user.id] - before
        if caused > NOISE_LIMIT:
            self.sim._problem(f"{player.user.display_name} put {caused} public posts in the channel "
                              f"with a burst of {SPAM_BURST} commands")

    async def send_feedback(self, player: Player) -> None:
        opened = await self.sim.run(player.user, "feedback")
        if opened.response.modal is None:
            return
        roll = self.rng.random()
        if roll < 0.15:
            await self.sim.narrate(f"{player.user.display_name} closes the form without sending")
        elif roll < 0.3:
            await self.sim.submit_form(player.user, opened.response.modal, "   ")
        elif roll < 0.45:
            # Opens a second form before sending the first, then sends both.
            second = await self.sim.run(player.user, "feedback")
            await self.sim.submit_form(player.user, opened.response.modal, "More rounds please")
            if second.response.modal is not None:
                await self.sim.submit_form(player.user, second.response.modal, "Also, a shorter bracket post")
        else:
            await self.sim.submit_form(player.user, opened.response.modal, f"Feedback from a {player.competence.name} player")

    # -- what the organizer does -------------------------------------------------------

    async def play(self) -> None:
        org, style = self.organizer.user, self.organizer.style
        if self.rng.random() < style.wrong_order:
            await self.sim.run(org, "tournament open", name=self.name)  # before the tournament exists
        if self.scenario.entry == "csv":
            await self._create_from_csv()
        else:
            await self._run_signups()
        await self._housekeeping()

        if self.rng.random() < style.wrong_order:
            await self.sim.run(org, "tournament lock", name=self.name)  # before predictions opened
        await self.sim.run(org, "tournament open", name=self.name)
        await self._player_turns()
        await self._housekeeping()
        await self.sim.run(org, "tournament lock", name=self.name)
        await self._play_matches()
        # A last look over every result before concluding. Fixing an early one can drop the
        # final's result, and then there are games left to record.
        await self._review_results(self.organizer)
        while self.status() == "in_progress":
            await self._play_matches()
            await self._review_results(self.organizer)
        await self._housekeeping()
        await self.sim.run(org, "tournament conclude", name=self.name)
        await self.refresh_live_posts()
        self.check_live_posts()
        self.status()
        await self.check_history_autocomplete()
        for player in self.players:
            if self.rng.random() < player.competence.curiosity:
                # Some still try the usual command, which should point them to /history.
                if self.rng.random() < 0.5:
                    await self.look_up(player, "leaderboard")
                else:
                    await self.look_back(player)

    async def _run_signups(self) -> None:
        org, style = self.organizer.user, self.organizer.style
        # Leaving seeding out tries the default.
        seeding = self.rng.choice([{}, {"seeding": "slide"}, {"seeding": "random"}])
        await self.sim.run(org, "registration open", name=self.name,
                           closes_in_hours=1.0 if style.uses_deadline else None, **seeding)
        self._remember_tournament_id()
        if self.rng.random() < style.wrong_order:
            await self.sim.run(org, "registration close", name=self.name)  # far too early
        await self._player_turns()
        for number in range(1, self.scenario.guests + 1):
            guest = f"Guest {number}"
            self.guest_skill[guest] = self.rng.gauss(1500, 200)
            await self.sim.run(org, "registration add", tournament=self.name, user=None, name=guest)
        if self.scenario.guests:
            # Second thoughts about a guest: withdraw them, add them back, then fix their name.
            await self.sim.run(org, "registration remove", tournament=self.name, entrant="Guest 1")
            await self.sim.run(org, "registration add", tournament=self.name, user=None, name="Guest 1")
            await self.sim.run(org, "admin editteam", tournament=self.name, team="Guest 1", new_name="Guest One")
            self.guest_skill["Guest One"] = self.guest_skill["Guest 1"]
        await self._player_turns()
        await self.separate_shared_names()
        await self.link_newcomer()
        await self._close_signups()
        self.check_seeds()

    async def _create_from_csv(self) -> None:
        names = [f"Team {chr(ord('A') + n)}" for n in range(self.scenario.guests)]
        for name in names:
            self.guest_skill[name] = self.rng.gauss(1500, 200)
        rows = ["team_a,team_b"] + [f"{a},{b}" for a, b in zip(names[::2], names[1::2])]
        await self.sim.run(self.organizer.user, "tournament create", name=self.name,
                           csv_file=FakeAttachment("\n".join(rows).encode()))
        self._remember_tournament_id()

    def _remember_tournament_id(self) -> None:
        conn = db.get_connection()
        self.tournament_id = db.get_tournament_by_name(conn, GUILD_ID, self.name)["id"]
        conn.close()
        self.status()

    async def _housekeeping(self) -> None:
        """What an organizer does between stages: read and clear feedback, and deal with
        whoever keeps poking at admin commands."""
        # Stages are hours apart on a real server, so every per-member wait has long run out.
        self.sim.cogs["FeedbackCog"].cooldown.clear()
        org = self.organizer.user
        reply = await self.sim.run(org, "admin feedback")
        newest = reply.sent.embed.fields if reply.sent is not None and reply.sent.embed is not None else []
        if newest:
            await self.sim.run(org, "admin deletefeedback", number=int(newest[0].name.split()[0].lstrip("#")))
        if self.banned is not None:
            await self.sim.run(org, "admin unban", user=self.banned.user)
            self.banned = None
        else:
            troublemakers = [p for p in self.players if p.competence.pokes_admin >= CHAOTIC.pokes_admin]
            if troublemakers and self.rng.random() < 0.5:
                await self._ban_midway(self.rng.choice(troublemakers))
                await self.sim.run(org, "admin ban", user=org, reason=None)  # can't ban an organizer
        await self.sim.run(org, "admin bans")

    async def _ban_midway(self, player: Player) -> None:
        """Ban a player who has a feedback form and a stats menu open, then have them try
        both. A ban should stop what's already open, not only new commands."""
        form = (await self.sim.run(player.user, "feedback")).response.modal
        stats = (await self.sim.run(player.user, "match stats", tournament=self.name)).sent
        self.banned = player
        await self.sim.run(self.organizer.user, "admin ban", user=player.user, reason="kept poking admin commands")
        if form is not None:
            self._expect_refusal("feedback form", await self.sim.submit_form(player.user, form, "Sent after being banned"))
        menu = _menu(stats)
        if menu is not None:
            self._expect_refusal("stats menu", await self.sim.choose(player.user, stats, menu.options[0].value))

    def _expect_refusal(self, what: str, reply) -> None:
        refused = reply is not None and reply.sent is not None and "blocked you" in (reply.sent.content or "")
        if not refused:
            self.sim._problem(f"a banned member could still use a {what} opened before the ban")

    async def _player_turns(self) -> None:
        for _ in range(self.scenario.turns_per_stage):
            # The bot's refresh loop runs alongside everyone, then a quiet refresh is checked.
            await self._all_at_once([player.take_turn(self) for player in self.rng.sample(self.players, len(self.players))]
                                    + [self.refresh_live_posts()])
            await self.refresh_live_posts()
            self.check_live_posts()

    async def _all_at_once(self, flows: list) -> None:
        """Run flows together if the scenario is crowded, otherwise one after another."""
        if self.scenario.crowded:
            await self.sim.together(flows, self.rng)
        else:
            for flow in flows:
                await flow

    async def _close_signups(self) -> None:
        org = self.organizer.user
        if self.entrant_count() < 2:
            await self.sim.run(org, "registration add", tournament=self.name, user=None, name="Stand-in")
            self.guest_skill["Stand-in"] = 1500
        if self.organizer.style.uses_deadline:
            conn = db.get_connection()
            conn.execute("UPDATE tournaments SET registration_closes_at = ? WHERE id = ?",
                         ("2000-01-01T00:00:00.000Z", self.tournament_id))
            conn.commit()
            conn.close()
            cog = self.sim.cogs["RegistrationCog"]
            # Latecomers try to sign up just as the deadline passes.
            latecomers = [p for p in self.players if not self.is_registered(p)][:3]
            await self._all_at_once(
                [self.sim.run_background("sign-up deadline passes", lambda: cog.close_due_registrations.coro(cog))]
                + [self.register(p) for p in latecomers]
            )
        if self.status() == "registration":
            await self.sim.run(org, "registration close", name=self.name)

    async def _play_matches(self) -> None:
        organizers = [self.organizer] + ([self.co_organizer] if self.co_organizer else [])
        while self.status() == "in_progress":
            found_a_match = []
            onlookers = [p.take_turn(self) for p in self.rng.sample(self.players, min(2, len(self.players)))]
            await self._all_at_once([self._organizer_round(o, found_a_match) for o in organizers] + onlookers
                                    + [self.refresh_live_posts()])
            await self.refresh_live_posts()
            self.check_live_posts()
            if not found_a_match and self.status() == "in_progress":
                self.sim._problem("no organizer could record any result, so the tournament can't finish")
                return

    async def _organizer_round(self, organizer: Organizer, found_a_match: list) -> None:
        """Correct any result this organizer got wrong earlier, then record one more."""
        await self._review_results(organizer)
        if await self._record_one_result(organizer):
            found_a_match.append(organizer)

    async def _record_one_result(self, organizer: Organizer) -> bool:
        """Record the real result of a ready match. False if the menu had none to offer.
        With two organizers, the other may record the same match first; the bot should
        then refuse this one."""
        org, style = organizer.user, organizer.style
        reply = await self.sim.run(org, "match result", tournament=self.name)
        message = reply.sent
        menu = _menu(message)
        if menu is None:
            return False
        match_id = int(self.rng.choice(menu.options).value)
        await self.sim.choose(org, message, str(match_id))
        buttons = [item.label for item in message.view.children] if message.view is not None else []
        if len(buttons) != 2:
            return True  # the matchup was refused or gone by the time they picked it
        matches_by_id, feeders, teams_by_id, results = self._bracket()
        label_to_team = {team_label(teams_by_id, team)[:80]: team for team in teams_by_id}
        team_a, team_b = (label_to_team[label] for label in buttons)
        winner = self._real_winner(match_id, matches_by_id, feeders)
        if winner not in (team_a, team_b):
            # An earlier result is wrong, so the bracket shows a game that wasn't played. A
            # careful organizer leaves it until that's fixed; a sloppy one records it anyway,
            # and the bot should drop or keep that result correctly once the earlier one is fixed.
            if self.rng.random() < style.wrong_result:
                await self.sim.click(org, message, self.rng.choice(buttons))
            return True
        loser = team_b if winner == team_a else team_a
        if self.rng.random() < style.wrong_result:
            await self.sim.click(org, message, team_label(teams_by_id, loser)[:80])
            # Half the time they notice at once; otherwise the mistake sits in the bracket
            # until their next review, while others may record games from it.
            if self.rng.random() < 0.5:
                await self._fix_result(organizer, match_id, winner, teams_by_id)
        else:
            await self.sim.click(org, message, team_label(teams_by_id, winner)[:80])
        if self.rng.random() < style.wrong_result / 2:
            await self._clear_result(organizer, match_id)
        return True

    async def _review_results(self, organizer: Organizer) -> None:
        """Look over the recorded results and correct any that don't match what was played,
        as an organizer would after noticing a mistake. Earliest first, since fixing one can
        bring the real winner of a later match back into it."""
        matches_by_id, feeders, teams_by_id, results = self._bracket()
        for match in sorted(matches_by_id.values(), key=bracket_order):
            if match.id not in results:
                continue
            real = self._real_winner(match.id, matches_by_id, feeders)
            if results[match.id] != real and real in resolve_slot(match, matches_by_id, results.get, feeders):
                await self._fix_result(organizer, match.id, real, teams_by_id)
                results = self._bracket()[3]  # the fix may have dropped later results

    async def _clear_result(self, organizer: Organizer, match_id: int) -> None:
        """Clears a result they just recorded, as if unsure; it gets recorded again later."""
        org = organizer.user
        reply = await self.sim.run(org, "admin clearresult", tournament=self.name)
        if await self.sim.choose(org, reply.sent, str(match_id)) is not None:
            await self.sim.click(org, reply.sent, "Confirm")

    async def _fix_result(self, organizer: Organizer, match_id: int, winner: int, teams_by_id) -> None:
        org = organizer.user
        reply = await self.sim.run(org, "admin setresult", tournament=self.name)
        message = reply.sent
        if await self.sim.choose(org, message, str(match_id)) is None:
            return  # the match isn't on offer any more, after someone else's change
        await self.sim.click(org, message, team_label(teams_by_id, winner)[:80])
        await self.sim.click(org, message, "Confirm")

    def _real_winner(self, match_id: int, matches_by_id, feeders) -> int:
        """Who really wins this match, decided by skill the first time anyone asks."""
        if match_id not in self.real_winners:
            team_a, team_b = self._real_matchup(match_id, matches_by_id, feeders)
            a_wins = 1 / (1 + 10 ** ((self.skill_of(team_b) - self.skill_of(team_a)) / 400))
            self.real_winners[match_id] = team_a if self.rng.random() < a_wins else team_b
        return self.real_winners[match_id]

    def _real_matchup(self, match_id: int, matches_by_id, feeders) -> tuple[int, int]:
        """The two entrants who really reached this match."""
        return resolve_slot(matches_by_id[match_id], matches_by_id,
                            lambda feeder_id: self._real_winner(feeder_id, matches_by_id, feeders), feeders)

    # -- the final comparison ------------------------------------------------------------

    def check_seeds(self) -> None:
        """A tournament seeded by rating must list its entrants best first, counting
        unrated ones as the starting rating, and pair them in round 1 as its seeding says."""
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament["status"] == "registration" or tournament["seeding_mode"] == "random":
                return
            ratings = compute_ratings(db.load_match_history(conn, GUILD_ID))
            members_by_team = db.get_members_by_team(conn, self.tournament_id)
            seeds = {team_id: team["seed"] for team_id, team in db.get_teams(conn, self.tournament_id).items()}
            matches_by_id = db.get_matches(conn, self.tournament_id)
        finally:
            conn.close()
        if sorted(seeds.values(), key=str) != sorted(range(1, len(seeds) + 1), key=str):
            self.sim._problem(f"a tournament seeded by rating has seeds {sorted(seeds.values(), key=str)}")
            return
        by_seed = [entrant_rating(members_by_team[team_id], ratings) for team_id in sorted(seeds, key=seeds.get)]
        by_seed = [BASE_RATING if rating is None else rating for rating in by_seed]
        if by_seed != sorted(by_seed, reverse=True):
            self.sim._problem(f"seeds don't follow the entrants' ratings: {[round(rating) for rating in by_seed]}")
        first_round = [(m.team_a_id, m.team_b_id) for m in sorted(matches_by_id.values(), key=lambda m: m.slot_index)
                       if m.round == 1]
        expected = seeded_pairs(sorted(seeds, key=seeds.get), slide=tournament["seeding_mode"] == "slide")
        if first_round != expected:
            self.sim._problem(f"round 1 isn't paired for {tournament['seeding_mode']} seeding")

    async def separate_shared_names(self) -> None:
        """A player who opted out shares a display name with one who agreed afterwards, so
        that one took over the opted-out player's entry. The organizer unlinks it, which
        must give it back under its name alone, and enters the other player under another name."""
        org = self.organizer.user
        for player in self.players:
            if player.user.id not in self.name_only:
                continue
            name = player.user.display_name
            conn = db.get_connection()
            try:
                taker = next((e["user_id"] for e in db.get_entrants(conn, self.tournament_id)
                              if e["name"] == name and e["user_id"] is not None), None)
            finally:
                conn.close()
            if taker is None:
                continue
            [picked, *_] = await self.sim.autocomplete(org, "admin unlinkplayer", "tournament", self.name[:2])
            await self.sim.autocomplete(org, "admin unlinkplayer", "entrant", name[:2], tournament=picked)
            await self.sim.run(org, "admin unlinkplayer", tournament=picked, entrant=name)
            taker_user = next(p.user for p in self.players if p.user.id == taker)
            await self.sim.run(org, "registration add", tournament=self.name, user=taker_user, name=f"{name} too")
            conn = db.get_connection()
            try:
                entrants = db.get_entrants(conn, self.tournament_id)
            finally:
                conn.close()
            if (name, None) not in [(e["name"], e["user_id"]) for e in entrants]:
                self.sim._problem(f"/admin unlinkplayer didn't leave {name} under its name alone")
            if not any(e["user_id"] == taker and e["name"] != name for e in entrants):
                self.sim._problem(f"{name}'s taker couldn't be entered under another name after /admin unlinkplayer")

    async def welcome_a_guest(self) -> Optional[tuple[Player, str, str]]:
        """After a tournament: a guest who played without a Discord account joins. The
        organizer can't link their old entry to them until they agree to it, which they do
        by signing up for the next tournament. Returns them, their old entrant name and the
        tournament it's in."""
        org, style = self.organizer.user, self.organizer.style
        [picked, *_] = await self.sim.autocomplete(org, "admin linkplayer", "tournament", self.name[:2])
        entrants = await self.sim.autocomplete(org, "admin linkplayer", "entrant", "Gu", tournament=picked)
        guest = next((entrant for entrant in entrants if entrant in self.guest_skill), None)
        if guest is None:
            return None
        newcomer = Player(self.sim.add_user(900 + len(self.players), f"Newcomer {len(self.players)}"),
                          CASUAL, self.guest_skill[guest], agrees=True)
        if self.rng.random() < style.wrong_order:
            # Tries to delete the old tournament by mistake, which a concluded one is safe from.
            reply = await self.sim.run(org, "admin deletetournament", tournament=picked, confirm_name=picked)
            if reply.sent is None or "has concluded" not in (reply.sent.content or ""):
                self.sim._problem("/admin deletetournament didn't refuse a concluded tournament")
        reply = await self.sim.run(org, "admin linkplayer", tournament=picked, entrant=guest, user=newcomer.user)
        if reply.sent is None or "hasn't agreed" not in (reply.sent.content or ""):
            self.sim._problem("/admin linkplayer linked a member who hadn't agreed to it")
        return newcomer, guest, picked

    async def link_newcomer(self) -> None:
        """Once last tournament's guest has signed up and agreed, link their old entry so
        it counts for their rating before this bracket is seeded."""
        if self.newcomer is None:
            return
        newcomer, guest, old_tournament = self.newcomer
        if not self.is_registered(newcomer):
            await self.register(newcomer)
        reply = await self.sim.run(self.organizer.user, "admin linkplayer", tournament=old_tournament, entrant=guest,
                                   user=newcomer.user)
        if reply.sent is None or "Linked" not in (reply.sent.content or ""):
            self.sim._problem(f"/admin linkplayer couldn't link {guest} after they agreed")
        await self.sim.run(newcomer.user, "rating")

    async def change_of_heart(self) -> None:
        """Between tournaments, a player who agreed opts out, which must unlink all of their
        entries. Some first get the confirmation wrong, which must change nothing."""
        conn = db.get_connection()
        try:
            agreed = [p for p in self.players if db.has_agreed(conn, GUILD_ID, p.user.id)]
        finally:
            conn.close()
        if not agreed:
            return
        player = self.rng.choice(agreed)
        if self.rng.random() < player.competence.typo:
            await self.sim.run(player.user, "optout", confirm="optout")
        await self.sim.run(player.user, "optout", confirm="opt out")
        conn = db.get_connection()
        try:
            still_linked = conn.execute("SELECT COUNT(*) FROM team_members WHERE user_id = ?", (player.user.id,)).fetchone()[0]
            still_agreed = db.has_agreed(conn, GUILD_ID, player.user.id)
        finally:
            conn.close()
        if still_linked or still_agreed:
            self.sim._problem(f"/optout left {player.user.display_name} linked to {still_linked} entries")
        player.agrees = False

    def check_final_state(self) -> None:
        self.sim._begin(None, "final checks")
        try:
            self._compare_final_state()
        finally:
            self.sim._after_step()

    def _compare_final_state(self) -> None:
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament["status"] != "concluded":
                self.sim._problem(f"the tournament ended at status {tournament['status']}, not concluded")
                return
            matches_by_id, feeders, _ = db.load_bracket(conn, self.tournament_id)
            results = db.get_results(conn, self.tournament_id)
            slots = resolve_all_slots(matches_by_id, results.get, feeders)
            for match_id, winner in results.items():
                if set(slots[match_id]) != set(self._real_matchup(match_id, matches_by_id, feeders)):
                    self.sim._problem(f"match {match_id} shows a matchup that wasn't played")
                elif winner != self._real_winner(match_id, matches_by_id, feeders):
                    self.sim._problem(f"match {match_id}'s stored winner isn't the one who really won")
            podium, _, scores = db.load_final_results(conn, self.tournament_id)
            final = final_match(matches_by_id)
            if podium.champion != self._real_winner(final.id, matches_by_id, feeders):
                self.sim._problem("the stored champion isn't the one who won the final")
            predictions = db.get_all_predictions(conn, self.tournament_id)
        finally:
            conn.close()
        for user_id, picks in predictions.items():
            for match in sorted(matches_by_id.values(), key=bracket_order):
                if is_bye(match):
                    continue
                if match.id not in picks:
                    self.sim._problem(f"user {user_id}'s saved bracket has no pick for match {match.id}")
                elif picks[match.id] not in resolve_slot(match, matches_by_id, picks.get, feeders):
                    self.sim._problem(f"user {user_id} picked a team that isn't in match {match.id} of their own bracket")
        expected = {
            user_id: sum(2 ** (matches_by_id[m].played_round - 1) for m, team in picks.items() if results.get(m) == team)
            for user_id, picks in predictions.items()
        }
        actual = {score.user_id: score.points for score in scores}
        if actual != expected:
            self.sim._problem(f"leaderboard points {actual} don't match the picks and results {expected}")


def _menu(message) -> Optional[discord.ui.Select]:
    if message is None or message.view is None:
        return None
    return next((item for item in message.view.children if isinstance(item, discord.ui.Select)), None)


async def run_scenario(scenario: Scenario, seed: int) -> Simulation:
    """Play one tournament from start to finish and return the simulation, with its issues and transcript."""
    rng = random.Random(seed)
    # Closing sign-ups draws the bracket with the random module, so it's seeded too, and put
    # back afterwards so nothing else sees the change.
    saved_random_state = random.getstate()
    random.seed(seed)
    try:
        return await _play_scenario(scenario, rng)
    finally:
        random.setstate(saved_random_state)


async def _play_scenario(scenario: Scenario, rng: random.Random) -> Simulation:
    with tempfile.TemporaryDirectory() as directory:
        sim = await Simulation.create(Path(directory) / "sim.db")
        try:
            await _play(sim, scenario, rng)
        finally:
            await sim.close()
    return sim


async def _play(sim: Simulation, scenario: Scenario, rng: random.Random) -> None:
    sim.simultaneous = scenario.simultaneous
    organizer = Organizer(sim.add_user(1, "Organizer", organizer=True), scenario.organizer)
    players = []
    names = iter(NAMES * 3)
    for competence, count in scenario.players:
        for _ in range(count):
            user_id = 100 + len(players)
            name = next(names) if user_id < 100 + len(NAMES) else f"Player {user_id}"
            players.append(Player(sim.add_user(user_id, name), competence, rng.gauss(1500, 200),
                                  agrees=rng.random() < AGREES_TO_LINK))
    if scenario.duplicate_names and len(players) > 1:
        players[1].user.display_name = players[0].user.display_name
        # If the one who opts out signs up first, the other takes over their entry by agreeing.
        players[0].agrees, players[1].agrees = False, True
    co_organizer = Organizer(sim.add_user(2, "Co-organizer", organizer=True), scenario.organizer) if scenario.co_organizer else None
    newcomer = None
    for number in range(scenario.tournaments):
        run = TournamentRun(sim, scenario, rng, organizer, players, name=TOURNAMENT_NAMES[number],
                            co_organizer=co_organizer, newcomer=newcomer)
        if number and rng.random() < scenario.organizer.wrong_order:
            await run.reuse_a_concluded_name(TOURNAMENT_NAMES[number - 1])
        await run.play()
        run.check_final_state()
        if number + 1 < scenario.tournaments:
            newcomer = await run.welcome_a_guest()
            if newcomer is not None:
                players.append(newcomer[0])
            await run.change_of_heart()
            for player in players:
                player.unfinished_bracket = None  # that tournament's /predict steps are gone
