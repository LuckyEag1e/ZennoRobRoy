"""Scenarios and the run that plays one from sign-ups to conclusion.

Real match results come from each entrant's hidden skill, so a run knows the true
champion and every true result. It checks the bot's final state against them.
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord

import db
from bracket import bracket_order, final_match, is_bye, resolve_all_slots, resolve_slot
from labels import team_label
from sim.actors import (
    CAREFUL,
    CASUAL,
    CHAOTIC,
    DILIGENT,
    SLOPPY,
    Competence,
    Organizer,
    OrganizerStyle,
    Player,
    entrant_count,
    guessed_winner,
)
from sim.discord_fakes import FakeAttachment
from sim.harness import GUILD_ID, Simulation

CONFIRM_BRACKET = "Confirm & Save Bracket"
START_OVER = "Start Over"
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
    format: str = "single_elim"    # the only format so far; Phase 5 adds more


SCENARIOS = {
    scenario.name: scenario for scenario in [
        Scenario("smooth", "8 diligent players and a careful organizer: nothing should go wrong.",
                 ((DILIGENT, 8),), CAREFUL),
        Scenario("typical", "A realistic mix with a couple of chaotic players and one guest entrant.",
                 ((DILIGENT, 4), (CASUAL, 6), (CHAOTIC, 2)), CAREFUL, guests=1, duplicate_names=True),
        Scenario("chaos", "Mostly chaotic players and a sloppy organizer who uses a deadline.",
                 ((DILIGENT, 2), (CASUAL, 3), (CHAOTIC, 7)), SLOPPY, guests=2, duplicate_names=True),
        Scenario("large", "40 players and 3 guests, for a 64-line bracket and long menus.",
                 ((DILIGENT, 10), (CASUAL, 25), (CHAOTIC, 5)), CAREFUL, guests=3, turns_per_stage=2),
        Scenario("csv", "A bracket of 8 named teams uploaded as a CSV, predicted by a mix of players.",
                 ((DILIGENT, 3), (CASUAL, 3), (CHAOTIC, 2)), SLOPPY, guests=8, entry="csv"),
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
    guest_skill: dict[str, float] = field(default_factory=dict)
    truth: dict[int, int] = field(default_factory=dict)  # match id -> the real winner's team id
    banned: Optional[Player] = None

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
            return tournament is not None and db.get_entrant_team_id(conn, tournament["id"], player.user.id) is not None
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

    async def predict(self, player: Player) -> None:
        reply = await self.sim.run(player.user, "predict", tournament=await self.tournament_name(player, "predict"))
        await self._fill_bracket(player, reply.sent)

    async def resume_bracket(self, player: Player) -> None:
        """Pick up a bracket they walked away from, which may be after predictions closed."""
        message, player.unfinished_bracket = player.unfinished_bracket, None
        self.sim._note(f"({player.user.display_name} comes back to the bracket they left)")
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
                self.sim._note(f"({user.display_name} walks away from their bracket)")
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

    async def send_feedback(self, player: Player) -> None:
        opened = await self.sim.run(player.user, "feedback")
        if opened.response.modal is None:
            return
        roll = self.rng.random()
        if roll < 0.15:
            self.sim._note(f"({player.user.display_name} closes the form without sending)")
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
        await self._housekeeping()
        await self.sim.run(org, "tournament conclude", name=self.name)
        self.status()
        for player in self.players:
            if self.rng.random() < player.competence.curiosity:
                await self.sim.run(player.user, "tournament leaderboard", name=self.name)

    async def _run_signups(self) -> None:
        org, style = self.organizer.user, self.organizer.style
        await self.sim.run(org, "registration open", name=self.name,
                           closes_in_hours=1.0 if style.uses_deadline else None)
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
        await self._close_signups()

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
                self.banned = self.rng.choice(troublemakers)
                await self.sim.run(org, "admin ban", user=self.banned.user, reason="kept poking admin commands")
                await self.sim.run(org, "admin ban", user=org, reason=None)  # can't ban an organizer
        await self.sim.run(org, "admin bans")

    async def _player_turns(self) -> None:
        for _ in range(self.scenario.turns_per_stage):
            for player in self.rng.sample(self.players, len(self.players)):
                await player.take_turn(self)

    async def _close_signups(self) -> None:
        org = self.organizer.user
        if entrant_count(self) < 2:
            await self.sim.run(org, "registration add", tournament=self.name, user=None, name="Stand-in")
            self.guest_skill["Stand-in"] = 1500
        if self.organizer.style.uses_deadline:
            conn = db.get_connection()
            conn.execute("UPDATE tournaments SET registration_closes_at = ? WHERE id = ?",
                         ("2000-01-01T00:00:00.000Z", self.tournament_id))
            conn.commit()
            conn.close()
            cog = self.sim.cogs["RegistrationCog"]
            await self.sim.run_background("sign-up deadline passes", lambda: cog.close_due_registrations.coro(cog))
        if self.status() == "registration":
            await self.sim.run(org, "registration close", name=self.name)

    async def _play_matches(self) -> None:
        while self.status() == "in_progress":
            if not await self._record_one_result():
                self.sim._problem("the organizer couldn't record any result, so the tournament can't finish")
                return
            for player in self.rng.sample(self.players, min(2, len(self.players))):
                await player.take_turn(self)

    async def _record_one_result(self) -> bool:
        """Record the real result of one ready match. False if there was none to record."""
        org, style = self.organizer.user, self.organizer.style
        reply = await self.sim.run(org, "match result", tournament=self.name)
        message = reply.sent
        menu = _menu(message)
        if menu is None:
            return False
        match_id = int(self.rng.choice(menu.options).value)
        await self.sim.choose(org, message, str(match_id))
        matches_by_id, feeders, teams_by_id, results = self._bracket()
        team_a, team_b = resolve_all_slots(matches_by_id, results.get, feeders)[match_id]
        winner = self._real_winner(team_a, team_b)
        loser = team_b if winner == team_a else team_a
        if self.rng.random() < style.wrong_result:
            await self.sim.click(org, message, team_label(teams_by_id, loser)[:80])
            await self._fix_result(match_id, winner, teams_by_id)
        else:
            await self.sim.click(org, message, team_label(teams_by_id, winner)[:80])
        self.truth[match_id] = winner
        if self.rng.random() < style.wrong_result / 2:
            await self._clear_result(match_id)
        return True

    async def _clear_result(self, match_id: int) -> None:
        """Clears a result they just recorded, as if unsure; it gets recorded again later."""
        org = self.organizer.user
        reply = await self.sim.run(org, "admin clearresult", tournament=self.name)
        if await self.sim.choose(org, reply.sent, str(match_id)) is None:
            self.sim._problem("/admin clearresult didn't offer the result just recorded")
            return
        await self.sim.click(org, reply.sent, "Confirm")
        del self.truth[match_id]

    async def _fix_result(self, match_id: int, winner: int, teams_by_id) -> None:
        org = self.organizer.user
        reply = await self.sim.run(org, "admin setresult", tournament=self.name)
        message = reply.sent
        if await self.sim.choose(org, message, str(match_id)) is None:
            self.sim._problem("/admin setresult didn't offer the match that needed fixing")
            return
        await self.sim.click(org, message, team_label(teams_by_id, winner)[:80])
        await self.sim.click(org, message, "Confirm")

    def _real_winner(self, team_a: int, team_b: int) -> int:
        a_wins = 1 / (1 + 10 ** ((self.skill_of(team_b) - self.skill_of(team_a)) / 400))
        return team_a if self.rng.random() < a_wins else team_b

    # -- the final comparison ------------------------------------------------------------

    def check_final_state(self) -> None:
        self.sim._begin(None, "final checks")
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament["status"] != "concluded":
                self.sim._problem(f"the tournament ended at status {tournament['status']}, not concluded")
                return
            matches_by_id, feeders, _ = db.load_bracket(conn, self.tournament_id)
            results = db.get_results(conn, self.tournament_id)
            if results != self.truth:
                self.sim._problem(f"stored results {results} differ from the ones played {self.truth}")
            podium, _, scores = db.load_final_results(conn, self.tournament_id)
            if podium.champion != self.truth.get(final_match(matches_by_id).id):
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
            user_id: sum(2 ** (matches_by_id[m].played_round - 1) for m, team in picks.items() if self.truth.get(m) == team)
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
    random.seed(seed)  # closing sign-ups draws the bracket with the random module
    with tempfile.TemporaryDirectory() as directory:
        sim = await Simulation.create(Path(directory) / "sim.db")
        organizer = Organizer(sim.add_user(1, "Organizer", organizer=True), scenario.organizer)
        players = []
        names = iter(NAMES * 3)
        for competence, count in scenario.players:
            for _ in range(count):
                user_id = 100 + len(players)
                name = next(names) if user_id < 100 + len(NAMES) else f"Player {user_id}"
                players.append(Player(sim.add_user(user_id, name), competence, rng.gauss(1500, 200)))
        if scenario.duplicate_names and len(players) > 1:
            players[1].user.display_name = players[0].user.display_name
        run = TournamentRun(sim, scenario, rng, organizer, players)
        await run.play()
        run.check_final_state()
    return sim
