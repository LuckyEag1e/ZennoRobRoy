"""The people in a simulation: players and organizers, each with a competence profile that
decides how often they slip up."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import db
from sim.discord_fakes import FakeUser

if TYPE_CHECKING:
    from sim.scenarios import TournamentRun


@dataclass(frozen=True)
class Competence:
    """How a player behaves. Each number is the chance of doing that thing on a turn."""

    name: str
    typo: float             # mistypes the tournament name instead of using autocomplete
    off_script: float       # tries something that doesn't fit the tournament's current stage
    double_click: float     # clicks a button twice, or on a step that's already gone
    abandon: float          # walks away from /predict partway through
    curiosity: float        # checks the bracket, leaderboard, picks or stats
    skips_predicting: float
    pokes_admin: float      # tries organizer commands
    feedback: float         # sends feedback (and sometimes a blank form)
    guess_noise: float      # how far off their read of who'll win is, in rating points


DILIGENT = Competence("diligent", typo=0.0, off_script=0.02, double_click=0.0, abandon=0.0, curiosity=0.3,
                      skips_predicting=0.0, pokes_admin=0.0, feedback=0.02, guess_noise=200)
CASUAL = Competence("casual", typo=0.05, off_script=0.1, double_click=0.05, abandon=0.15, curiosity=0.5,
                    skips_predicting=0.2, pokes_admin=0.02, feedback=0.05, guess_noise=350)
CHAOTIC = Competence("chaotic", typo=0.3, off_script=0.4, double_click=0.3, abandon=0.3, curiosity=0.8,
                     skips_predicting=0.1, pokes_admin=0.25, feedback=0.3, guess_noise=600)


@dataclass(frozen=True)
class OrganizerStyle:
    name: str
    wrong_order: float    # runs a stage's command too early or twice
    wrong_result: float   # records the wrong winner, then fixes it with /admin setresult
    uses_deadline: bool   # closes sign-ups with a deadline instead of by hand


CAREFUL = OrganizerStyle("careful", wrong_order=0.0, wrong_result=0.0, uses_deadline=False)
SLOPPY = OrganizerStyle("sloppy", wrong_order=0.4, wrong_result=0.15, uses_deadline=True)


class Player:
    def __init__(self, user: FakeUser, competence: Competence, skill: float) -> None:
        self.user = user
        self.competence = competence
        self.skill = skill  # true strength, which decides real match results
        self.unfinished_bracket = None  # the /predict step they walked away from, if any

    def __repr__(self) -> str:
        return f"{self.user.display_name} ({self.competence.name})"

    async def take_turn(self, run: TournamentRun) -> None:
        """One turn: maybe the main thing for this stage, plus whatever else they feel like."""
        rng, c = run.rng, self.competence
        status = run.status()
        if status == "registration":
            await self._registration_turn(run)
        elif self.unfinished_bracket is not None and rng.random() < 0.5:
            await run.resume_bracket(self)
        elif status == "predictions_open" and not run.has_predicted(self) and rng.random() >= c.skips_predicting:
            await run.predict(self)
        if rng.random() < c.curiosity:
            await self._look_around(run)
        if rng.random() < c.off_script:
            await self._off_script(run)
        if rng.random() < c.pokes_admin:
            await self._poke_admin(run)
        if rng.random() < c.feedback:
            await run.send_feedback(self)

    async def _registration_turn(self, run: TournamentRun) -> None:
        if not run.is_registered(self):
            await run.sim.run(self.user, "register", tournament=await run.tournament_name(self, "register"))
        elif run.rng.random() < self.competence.off_script:
            # Changes their mind, then signs up again.
            await run.sim.run(self.user, "unregister", tournament=await run.tournament_name(self, "unregister"))
            await run.sim.run(self.user, "register", tournament=await run.tournament_name(self, "register"))

    async def _look_around(self, run: TournamentRun) -> None:
        """Checks something that makes sense at this stage of the tournament."""
        status = run.status()
        if status == "registration":
            options = ["entrants", "features", "help"]
        elif status == "predictions_open":
            options = ["entrants", "bracket", "stats", "features"] + (["mypicks"] if run.has_predicted(self) else [])
        elif status in ("in_progress", "completed"):
            options = ["bracket", "leaderboard", "stats"] + (["mypicks"] if run.has_predicted(self) else [])
        else:
            options = ["leaderboard"]  # after it's over, people still look for the standings
        choice = run.rng.choice(options)
        if choice == "bracket":
            await run.sim.run(self.user, "tournament bracket", name=await run.tournament_name(self, "tournament bracket", "name"))
        elif choice == "leaderboard":
            await run.sim.run(self.user, "tournament leaderboard",
                              name=await run.tournament_name(self, "tournament leaderboard", "name"))
        elif choice == "mypicks":
            await run.sim.run(self.user, "mypicks", tournament=await run.tournament_name(self, "mypicks"))
        elif choice == "stats":
            await run.match_stats(self)
        elif choice == "entrants":
            await run.sim.run(self.user, "registration entrants",
                              name=await run.tournament_name(self, "registration entrants", "name"))
        elif choice == "features":
            await run.sim.run(self.user, "features")
        else:
            await run.sim.run(self.user, "help players")

    async def _off_script(self, run: TournamentRun) -> None:
        """Something that doesn't fit right now, which should get a clear refusal."""
        command = run.rng.choice(["register", "unregister", "predict", "mypicks"])
        await run.sim.run(self.user, command, tournament=await run.tournament_name(self, command))

    async def _poke_admin(self, run: TournamentRun) -> None:
        name = run.name
        command, options = run.rng.choice([
            ("registration close", {"name": name}),
            ("tournament open", {"name": name}),
            ("match result", {"tournament": name}),
            ("admin feedback", {}),
            ("help organizers", {}),
        ])
        await run.sim.run(self.user, command, **options)


class Organizer:
    def __init__(self, user: FakeUser, style: OrganizerStyle) -> None:
        self.user = user
        self.style = style

    def __repr__(self) -> str:
        return f"{self.user.display_name} ({self.style.name} organizer)"


def guessed_winner(player: Player, run: TournamentRun, team_a: int, team_b: int, rng: random.Random) -> int:
    """Who a player thinks will win: the truly stronger side, read through their own noise."""
    noise = player.competence.guess_noise
    a = run.skill_of(team_a) + rng.gauss(0, noise)
    b = run.skill_of(team_b) + rng.gauss(0, noise)
    return team_a if a >= b else team_b


def entrant_count(run: TournamentRun) -> int:
    conn = db.get_connection()
    try:
        tournament = db.get_tournament_by_name(conn, 1, run.name)
        return len({e["team_id"] for e in db.get_entrants(conn, tournament["id"])}) if tournament else 0
    finally:
        conn.close()
