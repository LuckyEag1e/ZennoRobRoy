#!/usr/bin/env python3
"""Local REPL for exercising WinningTicket's command logic without a live Discord bot.

Talks directly to db.py/bracket.py/csv_import.py -- the same modules the real
slash commands use -- so behavior here matches the bot exactly. Uses its own
sqlite file (data/cli.db) so it never touches a real bot's data.
"""

from __future__ import annotations

import cmd
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

import csv_import
import coordination
import db
import lobbies
import lobby_db
import lobby_flow
import lobby_posts
import match_posts
import reports
import tournament_builder
from ascii_bracket import prediction_marker, real_result_marker, render_tree
from bracket import (
    BracketError,
    Match,
    bracket_order,
    compute_leaderboard,
    is_bye,
    matches_in_round,
    matches_with_both_teams,
    pick_distribution,
    ranked_places,
    resolvable_matches,
    resolve_all_slots,
    resolve_slot,
    rounds,
)
from bracket_changes import (
    REINSTATABLE_STATUSES,
    WITHDRAWABLE_STATUSES,
    clear_result_and_describe,
    later_results_problem,
    new_walkovers,
    reinstate_problem,
    set_result_and_describe,
    skip_and_describe,
    withdraw_and_describe,
)
from labels import (
    CONSENT_NOTICE,
    CURRENT_FEATURES,
    OPT_OUT_CONFIRMATION,
    OPTED_OUT_NOTE,
    PLANNED_FEATURES,
    SETTLED_BEFORE_LOCK,
    ban_lines,
    bracket_notes,
    contribution_line,
    crowd_verdict,
    draw_description,
    dropped_results_note,
    entrant_lines,
    feedback_page,
    history_line,
    matchup_label,
    name_held_message,
    name_taken_message,
    opted_out_message,
    pick_review_lines,
    pick_share,
    pickem_podium_lines,
    podium_lines,
    rating_lines,
    reinstated_line,
    seeding_lines,
    score_label,
    team_label,
    wrong_format_message,
)
from contribution import contributions
from ratings import compute_ratings

CLI_DB_PATH = Path(__file__).parent / "data" / "cli.db"
GUILD_ID = 1  # single pretend guild for local testing
# What a banned user can still do: switch user, look around the CLI, opt out, and leave.
ALLOWED_WHEN_BANNED = {"as", "whoami", "help", "exit", "quit", "EOF", "optout"}


def user_label(user_id: int) -> str:
    """How the CLI names a Discord user, standing in for a mention."""
    return f"user {user_id}"


def take_option(arg: str, flag: str) -> tuple[str, str | None]:
    """Remove `flag VALUE` from a command line. Returns the rest of the line and VALUE,
    which is None when the flag isn't there and "" when it has no value."""
    found = re.search(rf"(?:^|\s){re.escape(flag)}(?:\s+(\S+))?", arg)
    if found is None:
        return arg.strip(), None
    return (arg[:found.start()] + arg[found.end():]).strip(), found.group(1) or ""


def room_label(room_id: int) -> str:
    """How the CLI names a match room, standing in for a voice channel mention."""
    return f"room {room_id}"


def parse_match_slots(value: str | None) -> int | None | bool:
    """The number given with --match-slots, None when it wasn't given, or False, after
    saying so, when it isn't a whole number of at least 1."""
    if value is None:
        return None
    if not value.isdigit() or int(value) < 1:
        print("--match-slots must be a whole number of at least 1")
        return False
    return int(value)


def check_seeding(seeding: str | None) -> bool:
    """False, after saying so, if a --seeding value was given but isn't a seeding mode."""
    if seeding is not None and seeding not in db.SEEDING_MODES:
        print(f"--seeding must be one of: {', '.join(db.SEEDING_MODES)}")
        return False
    return True


class WinningTicketCLI(cmd.Cmd):
    intro = (
        "WinningTicket local CLI -- simulates the Discord slash commands without "
        "Discord. Type 'help' for commands, 'exit' to quit.\n"
        "Using db: " + str(CLI_DB_PATH)
    )

    def __init__(self) -> None:
        super().__init__()
        self.conn = db.get_connection(CLI_DB_PATH)
        db.init_db(self.conn)
        self.user_id = 1
        self.user_name = "tester"
        self.rooms: list[int] = []  # pretend voice channels, set with admin_matchrooms
        self._update_prompt()

    def onecmd(self, line: str) -> bool:
        self._close_due_registrations()
        self._tend_matches()
        command = self.parseline(line)[0]
        if command and command not in ALLOWED_WHEN_BANNED and db.is_banned(self.conn, GUILD_ID, self.user_id):
            print("An admin has blocked you from using WinningTicket. Use 'as' to switch user.")
            return False
        return super().onecmd(line)

    def _close_due_registrations(self) -> None:
        """The bot closes overdue sign-ups from a background loop; the CLI has none, so it
        does the same before each command."""
        for tournament in db.get_tournaments_due_to_close(self.conn):
            name = tournament["name"]
            try:
                count = tournament_builder.close_overdue_registration(self.conn, tournament["id"])
            except tournament_builder.RegistrationClosedError:
                continue  # something else sharing the database closed it since the check
            except BracketError as e:
                print(f"Registration for '{name}' couldn't close automatically: {e} It stays open until 'regclose'.")
                continue
            except Exception as e:
                # This runs before every command, so a close that keeps failing mustn't take the CLI down with it.
                print(f"Registration for '{name}' couldn't close automatically: {e!r}")
                continue
            print(f"Registration for '{name}' closed automatically; {count} entrants {draw_description(tournament['seeding_mode'])}.")

    def _tend_matches(self) -> None:
        """The bot calls matches, warns about and gives unanswered votes, and updates each
        match's post from a background loop. The CLI has none, so it does the same before
        each command, printing what the bot would post or edit."""
        now = datetime.now(timezone.utc)
        for tournament in db.get_tournaments_to_tend(self.conn):
            coordination.tend(self.conn, tournament, now, self.rooms)
        coordination.call_queued(self.conn, GUILD_ID, self.rooms)
        for step in reports.due_steps(self.conn, now):
            vote = step.vote
            if step.kind == "warn":
                reports.warn(self.conn, vote)
            else:
                text = reports.give_win(self.conn, vote)
                if text is not None:
                    db.delete_match_post(self.conn, vote["match_id"])
                    print(text)
        for post in db.get_match_posts(self.conn):
            self._sync_match_post(post["match_id"])
        lobby_flow.tend(self.conn, now)
        for tournament in lobby_db.get_tournaments_with_posts(self.conn):
            for post in lobby_db.get_posts(self.conn, tournament["id"]):
                self._sync_lobby_post(post)

    def _sync_lobby_post(self, post) -> None:
        """The CLI's version of lobby_posting's post syncing: it prints a post when it would
        be sent or edited, with who it notifies and its buttons."""
        state = lobby_posts.describe(self.conn, post, user_label)
        if state is None:
            return
        note = lobby_posts.notification(state, post["pinged"])
        shown = state.text + "\n" + "|".join(state.buttons)
        fresh = post["message_id"] is None or note is not None
        if not fresh and shown == post["shown"]:
            return
        notified = f" notifying {', '.join(user_label(u) for u in note[1])}" if note and note[1] else ""
        buttons = "".join(f" [{name}]" for name in state.buttons)
        print(f"(5v5 post{'' if fresh else ' edited'}{notified}) {state.text}{buttons}".replace("**", ""))
        key = post["post_key"]
        if fresh:
            claim = lobby_db.claim_post_send(self.conn, post["tournament_id"], key)
            lobby_db.take_over_post(self.conn, post["tournament_id"], key, claim, 0, shown,
                                    note[0] if note else post["pinged"])
        else:
            lobby_db.set_post_shown(self.conn, post["tournament_id"], key, 0, shown)

    def _sync_match_post(self, match_id: int) -> None:
        """The CLI's version of posting.sync_match_post. It has no messages, so every post
        gets message id 0, and a post sent afresh is printed the same as an edit."""
        post = db.get_match_post(self.conn, match_id)
        if post is None:
            return
        state = match_posts.describe(self.conn, post, user_label, room_label)
        fresh = post["message_id"] is None or (state.ping is not None and state.ping != post["pinged"])
        if fresh or state.text != post["shown"]:
            buttons = [name for name, shown in (("Ready Up", state.ready_button), ("Report Result", state.report_button))
                       if shown]
            print(f"(match post{'' if fresh else ' edited'}) {state.text}" + "".join(f" [{name}]" for name in buttons))
            if fresh:
                db.take_over_match_post(self.conn, match_id, db.claim_post_send(self.conn, match_id), 0, state.text,
                                        state.ping or post["pinged"])
                if state.ping is not None and state.ping.startswith("warning:"):
                    db.warn_on_match_post(self.conn, match_id)
            else:
                db.set_match_post(self.conn, match_id, 0, state.text, post["pinged"])
        if state.final:
            db.delete_match_post(self.conn, match_id)

    def _update_prompt(self) -> None:
        self.prompt = f"(tourney:{self.user_name}) "

    # -- simulated user ----------------------------------------------------

    def do_whoami(self, arg: str) -> None:
        "Show the simulated current user."
        print(f"user_id={self.user_id} name={self.user_name}")

    def do_as(self, arg: str) -> None:
        "Switch the simulated user, to test multiple predictors: as <user_id> <display_name>"
        parts = shlex.split(arg)
        if len(parts) < 2:
            print("usage: as <user_id> <display_name>")
            return
        try:
            self.user_id = int(parts[0])
        except ValueError:
            print("user_id must be an integer")
            return
        self.user_name = " ".join(parts[1:])
        self._update_prompt()
        print(f"Now acting as user_id={self.user_id} ({self.user_name})")

    # -- tournament lifecycle ----------------------------------------------

    def do_tournaments(self, arg: str) -> None:
        "List all tournaments."
        names = db.get_tournament_names(self.conn, GUILD_ID)
        if not names:
            print("No tournaments yet. Use 'create' or 'regopen' to make one.")
            return
        for name in names:
            tournament = db.get_tournament_by_name(self.conn, GUILD_ID, name)
            print(f"- {name} [{tournament['status']}]")

    def do_create(self, arg: str) -> None:
        "Create a tournament from a CSV file: create <name> <csv_path> [--match-slots N]"
        rest, slots = take_option(arg, "--match-slots")
        parts = shlex.split(rest)
        if len(parts) != 2:
            print("usage: create <name> <csv_path> [--match-slots N]")
            return
        name, csv_path = parts
        match_slots = parse_match_slots(slots)
        if match_slots is False or self._name_taken(name) or not self._match_slots_fit(match_slots):
            return
        try:
            content = Path(csv_path).read_text()
        except OSError as e:
            print(f"Couldn't read {csv_path}: {e}")
            return
        try:
            rows = csv_import.parse_csv(content)
            tournament_id = csv_import.import_tournament(self.conn, GUILD_ID, name, self.user_id, rows)
            db.set_match_slots(self.conn, tournament_id, match_slots)
        except csv_import.CsvValidationError as e:
            print(f"CSV error: {e}")
            return
        print(f"Created tournament '{name}' with {len(rows)} round-1 matchups.")

    def do_open(self, arg: str) -> None:
        "Open a tournament for predictions: open <name>"
        if self._advance_status(arg.strip(), "predictions_open"):
            print(f"Predictions open for '{arg.strip()}'.")

    def do_lock(self, arg: str) -> None:
        "Lock a tournament and start recording real results: lock <name>"
        status = self._advance_status(arg.strip(), "in_progress")
        if status is None:
            return
        tournament = db.get_tournament_by_name(self.conn, GUILD_ID, arg.strip())
        db.set_tournament_channel_if_missing(self.conn, tournament["id"], 0)
        if status == "completed":
            print(f"'{arg.strip()}' locked. {SETTLED_BEFORE_LOCK}")
        else:
            print(f"'{arg.strip()}' locked. Players report with 'matchreport'; 'matchresult' records any result.")

    def do_conclude(self, arg: str) -> None:
        "Archive a finished tournament and show its final results: conclude <name>"
        name = arg.strip()
        tournament = self._require_next_status(name, "concluded", db.FORMATS)
        if tournament is None:
            return
        if tournament["format"] == "lobbies":
            print(f"{name} is over! How each lobby went:")
            print("\n".join(lobby_flow.lobby_summary_lines(self.conn, tournament)))
            db.set_tournament_status(self.conn, tournament["id"], "concluded")
            print(f"\n'{name}' concluded. Its results count toward balancing future 5v5 teams, and 'history' lists it.")
            return
        if not db.final_decided(self.conn, tournament["id"]):
            print(f"'{name}' has no result for its final yet, so it can't be concluded.")
            return
        result, teams_by_id, scores = db.load_final_results(self.conn, tournament["id"])
        print(f"Final results: {name}\n")
        print("\n".join(podium_lines(result, teams_by_id)))
        print("\nPick'em leaders")
        print("\n".join(pickem_podium_lines(scores, user_label)))
        db.set_tournament_status(self.conn, tournament["id"], "concluded")
        print(f"\n'{name}' concluded. Its results are saved and count toward ratings, and 'history' lists it.")

    def do_bracket(self, arg: str) -> None:
        "Show the real bracket's current progress: bracket <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is not None:
            self._print_bracket(tournament)

    def _print_bracket(self, tournament) -> None:
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        outcomes = db.get_outcomes(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
        print(render_tree(
            matches_by_id, feeders, teams_by_id, slots,
            outcomes.winners.get, real_result_marker(outcomes.winners),
        ))
        for note in bracket_notes(matches_by_id, slots, outcomes, teams_by_id):
            print(note)

    def do_leaderboard(self, arg: str) -> None:
        "Show the current standings: leaderboard <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is not None:
            self._print_leaderboard(tournament)

    def _print_leaderboard(self, tournament) -> None:
        scores = db.load_scores(self.conn, tournament["id"])
        if not scores:
            print("No predictions submitted yet.")
            return
        participant_word = "participant" if len(scores) == 1 else "participants"
        print(f"{len(scores)} {participant_word}\n")
        for place, s in ranked_places(scores):
            print(f"{place}. {user_label(s.user_id)}: {score_label(s)}")

    # -- history -------------------------------------------------------------

    def do_history(self, arg: str) -> None:
        "List concluded tournaments, newest first, with their champion, size, format and date."
        summaries = db.get_concluded_summaries(self.conn, GUILD_ID)
        if not summaries:
            print("No tournaments have concluded yet.")
        for summary in summaries:
            print(f"- {history_line(summary)}")

    def do_history_bracket(self, arg: str) -> None:
        "Show a concluded tournament's final bracket, or a 5v5 event's lobbies: history_bracket <name>"
        tournament = self._require_concluded(arg.strip())
        if tournament is None:
            return
        if tournament["format"] == "lobbies":
            print("\n".join(lobby_flow.history_lines(self.conn, tournament, user_label)).replace("**", ""))
        else:
            self._print_bracket(tournament)

    def do_history_seeding(self, arg: str) -> None:
        "Show how a concluded tournament was seeded: history_seeding <name>"
        tournament = self._require_concluded(arg.strip())
        if tournament is not None:
            print("\n".join(seeding_lines(tournament, db.get_teams(self.conn, tournament["id"]))))

    def do_history_leaderboard(self, arg: str) -> None:
        "Show a concluded tournament's final prediction leaderboard: history_leaderboard <name>"
        tournament = self._require_concluded(arg.strip())
        if tournament is not None:
            self._print_leaderboard(tournament)

    # -- registration --------------------------------------------------------

    def do_regopen(self, arg: str) -> None:
        "Create a bracket tournament that takes sign-ups, closing automatically after --closes-in hours if given: regopen <name> [--closes-in HOURS] [--seeding standard|slide|random] [--match-slots N]"
        rest, seeding = take_option(arg, "--seeding")
        rest, slots = take_option(rest, "--match-slots")
        name, closes_at = self._take_closes_in(rest)
        if not name:
            print("usage: regopen <name> [--closes-in HOURS] [--seeding standard|slide|random] [--match-slots N]")
            return
        match_slots = parse_match_slots(slots)
        if closes_at is False or not check_seeding(seeding) or match_slots is False or not self._match_slots_fit(match_slots):
            return
        if self._name_taken(name):
            return
        tournament_id = db.open_registration(self.conn, GUILD_ID, name, self.user_id, None,
                                             db.to_db_time(closes_at) if closes_at is not None else None,
                                             seeding or "standard")
        db.set_match_slots(self.conn, tournament_id, match_slots)
        self._report_opened(name, closes_at)

    def do_regopen5v5(self, arg: str) -> None:
        "Create a 5v5 event that takes sign-ups, starting --starts-in hours from now: regopen5v5 <name> --starts-in HOURS [--closes-in HOURS] [--spread tier|balanced|random]"
        rest, starts_in = take_option(arg, "--starts-in")
        rest, spread = take_option(rest, "--spread")
        name, closes_at = self._take_closes_in(rest)
        if not name:
            print("usage: regopen5v5 <name> --starts-in HOURS [--closes-in HOURS] [--spread tier|balanced|random]")
            return
        if spread is not None and spread not in lobbies.SPREADS:
            print(f"--spread must be one of {', '.join(lobbies.SPREADS)}")
            return
        try:
            starts_at = datetime.now(timezone.utc) + timedelta(hours=float(starts_in or ""))
        except ValueError:
            print("A 5v5 event needs --starts-in HOURS, so players know when to play.")
            return
        if closes_at is False:
            return
        if closes_at is not None and closes_at >= starts_at:
            print("Sign-ups have to close before play starts.")
            return
        if self._name_taken(name):
            return
        # A 5v5 event's posts need a channel to belong to; the CLI's one pretend channel is 0.
        db.open_registration(self.conn, GUILD_ID, name, self.user_id, 0,
                             db.to_db_time(closes_at) if closes_at is not None else None, "standard", "lobbies",
                             db.to_db_time(starts_at), spread or "balanced")
        self._report_opened(name, closes_at)

    def _take_closes_in(self, arg: str) -> tuple[str, datetime | None | bool]:
        """The rest of `arg`, and when --closes-in says sign-ups close: None without it,
        False (after saying why) if it's not a usable number of hours."""
        name, hours = take_option(arg, "--closes-in")
        if not hours:
            return name, None
        low, high = tournament_builder.MIN_CLOSES_IN_HOURS, tournament_builder.MAX_CLOSES_IN_HOURS
        try:
            closes_in = float(hours)
        except ValueError:
            closes_in = None
        # Written so a NaN fails it too.
        if closes_in is None or not low <= closes_in <= high:
            print(f"--closes-in must be a number of hours from {low} to {high:g}")
            return name, False
        return name, datetime.now(timezone.utc) + timedelta(hours=closes_in)

    def _report_opened(self, name: str, closes_at: datetime | None) -> None:
        closing = f" Sign-ups close at {closes_at:%H:%M:%S} UTC." if closes_at is not None else ""
        print(f"Sign-ups open for '{name}'.{closing} Use 'as' to switch users, then 'register {name}'.")

    def do_register(self, arg: str) -> None:
        "Sign up the current user (5v5 events ask whether you'll captain): register <name> [--captain want|can|no]"
        name, captain = take_option(arg, "--captain")
        tournament = self._require_tournament(name, needs_bracket=False, formats=db.FORMATS)
        if tournament is not None and tournament["format"] == "lobbies":
            self._lobby_sign_up(tournament, captain)
            return
        tournament = self._require_registration(name)
        if tournament is None:
            return
        if db.get_entrant_team_id(self.conn, tournament["id"], self.user_id) is not None:
            print("You're already signed up.")
            return
        if not db.has_agreed(self.conn, GUILD_ID, self.user_id):
            print(CONSENT_NOTICE.replace("**", ""))
            if input("Agree? [y/N] ").strip().lower() != "y":
                db.opt_out(self.conn, GUILD_ID, self.user_id)
                if db.register_entrant(self.conn, tournament["id"], self.user_name, None) is None:
                    print(f"Someone is already signed up as {self.user_name}.")
                    return
                print(f"{self.user_name} signed up by name only. {OPTED_OUT_NOTE}")
                return
            db.record_agreement(self.conn, GUILD_ID, self.user_id)
        if db.register_entrant(self.conn, tournament["id"], self.user_name, self.user_id) is None:
            print(name_held_message(tournament["name"], self.user_name).replace("**", ""))
            return
        print(f"{self.user_name} signed up ({len(db.get_entrants(self.conn, tournament['id']))} entered so far).")

    def do_optout(self, arg: str) -> None:
        "Unlink the current user's entries, results and rating from their account (asks you to confirm)."
        if input(f"Type '{OPT_OUT_CONFIRMATION}' to confirm: ").strip().lower() != OPT_OUT_CONFIRMATION:
            print("Nothing changed.")
            return
        print(opted_out_message(db.opt_out(self.conn, GUILD_ID, self.user_id)))

    def do_unregister(self, arg: str) -> None:
        "Withdraw the current user: unregister <name>"
        found = self._require_tournament(arg.strip(), needs_bracket=False, formats=db.FORMATS)
        if found is not None and found["format"] == "lobbies" and found["status"] != "registration":
            self.do_dropout(arg)
            return
        tournament = self._require_registration(arg.strip())
        if tournament is None:
            return
        if db.unregister_entrant(self.conn, tournament["id"], self.user_id):
            print(f"{self.user_name} withdrew.")
        else:
            print("You aren't signed up.")

    def do_entrants(self, arg: str) -> None:
        "List who has signed up: entrants <name>"
        tournament = self._require_tournament(arg.strip(), needs_bracket=False, formats=db.FORMATS)
        if tournament is None:
            return
        entrants = db.get_entrants(self.conn, tournament["id"])
        if not entrants:
            print("Nobody has signed up yet.")
        lines = (lobby_flow.entrant_lines(self.conn, tournament, user_label) if tournament["format"] == "lobbies"
                 else entrant_lines(entrants, user_label))
        for line in lines:
            print(line)

    def do_regadd(self, arg: str) -> None:
        "Sign up someone else, or an entrant with no account if no user_id. For a 5v5 event, --captain says whether they'll captain: regadd <name> <entrant> [user_id] [--captain want|can|no]"
        arg, captain = take_option(arg, "--captain")
        parts = shlex.split(arg)
        if len(parts) not in (2, 3) or (captain is not None and captain not in lobby_flow.CAPTAIN_CHOICES):
            print("usage: regadd <name> <entrant> [user_id] [--captain want|can|no]")
            return
        tournament = self._require_registration(parts[0])
        if tournament is None:
            return
        user_id = None
        if len(parts) == 3:
            try:
                user_id = int(parts[2])
            except ValueError:
                print("user_id must be an integer")
                return
            if db.get_entrant_team_id(self.conn, tournament["id"], user_id) is not None:
                print(f"User {user_id} is already signed up.")
                return
            if not db.has_agreed(self.conn, GUILD_ID, user_id):
                print(f"{user_label(user_id)} hasn't agreed to link their data, so they're entered by name only.")
                user_id = None
        problem = (lobby_flow.captain_problem(captain or "no", user_id is not None, by_organizer=True)
                   if tournament["format"] == "lobbies" else None)
        if problem is not None:
            print(problem)
            return
        entrant = db.register_entrant(self.conn, tournament["id"], parts[1], user_id)
        if entrant is None:
            link_hint = " Use 'admin_linkplayer' to link them to it." if user_id is not None else ""
            print(f"'{parts[0]}' already has an entrant named '{parts[1]}'.{link_hint}")
            return
        team_id, entrant_name = entrant
        if tournament["format"] == "lobbies":
            lobby_db.set_captain_pref(self.conn, tournament["id"], team_id, captain or "no")
        print(f"Signed up {entrant_name} ({len(db.get_entrants(self.conn, tournament['id']))} entered so far).")

    def do_regremove(self, arg: str) -> None:
        "Withdraw any entrant by name: regremove <name> <entrant>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: regremove <name> <entrant>")
            return
        tournament = self._require_registration(parts[0])
        if tournament is None:
            return
        team = db.get_team_by_name(self.conn, tournament["id"], parts[1])
        if team is None:
            print(f"No entrant named '{parts[1]}'.")
            return
        db.delete_team(self.conn, team["id"])
        print(f"Withdrew {parts[1]}.")

    def do_regclose(self, arg: str) -> None:
        "Close sign-ups and draw the bracket, optionally overriding how it's seeded (brackets only). For a 5v5 event, --lobbies picks how many lobbies (the proposal if left out): regclose <name> [--seeding standard|slide|random] [--lobbies N] [--spread tier|balanced|random]"
        arg, lobby_count = take_option(arg, "--lobbies")
        arg, spread = take_option(arg, "--spread")
        name, seeding = take_option(arg, "--seeding")
        if not check_seeding(seeding):
            return
        tournament = self._require_registration(name)
        if tournament is None:
            return
        if tournament["format"] == "lobbies":
            if seeding is not None:
                print("5v5 events aren't seeded: leave out --seeding, and use --spread for how players are spread.")
                return
            self._close_lobbies(tournament, lobby_count, spread)
            return
        seeding = seeding or tournament["seeding_mode"]
        try:
            count = tournament_builder.close_registration(self.conn, tournament["id"], seeding_mode=seeding)
        except BracketError as e:
            print(f"Couldn't close registration: {e}")
            return
        print(f"Registration closed; {count} entrants {draw_description(seeding)}.\n")
        self.do_bracket(tournament["name"])

    def do_rating(self, arg: str) -> None:
        "Show a user's singles and team Elo and 5v5 contribution (yours if no user_id): rating [user_id]"
        user_id = self.user_id
        if arg.strip():
            try:
                user_id = int(arg)
            except ValueError:
                print("user_id must be an integer")
                return
        ratings = compute_ratings(db.load_match_history(self.conn, GUILD_ID))
        print(f"Elo rating: {user_label(user_id)}")
        by_user = contributions(lobby_db.load_lobby_history(self.conn, GUILD_ID))
        for line in rating_lines(user_id, ratings) + [contribution_line(user_id, by_user)]:
            print(f"  {line.replace('**', '')}")

    # -- predictions ---------------------------------------------------------

    def do_predict(self, arg: str) -> None:
        "Fill out your bracket prediction round by round: predict <name>"
        name = arg.strip()
        tournament = self._require_tournament(name)
        if tournament is None:
            return
        if tournament["status"] != "predictions_open":
            print(f"Predictions aren't open for '{name}' (status: {tournament['status']}).")
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])

        picks: dict[int, int] = {}
        for round_num in rounds(matches_by_id):
            decision_matches = [m for m in matches_in_round(matches_by_id, round_num) if not is_bye(m)]
            for i, match in enumerate(decision_matches, start=1):
                team_a, team_b = resolve_slot(match, matches_by_id, picks.get, feeders)
                a_label = team_label(teams_by_id, team_a)
                b_label = team_label(teams_by_id, team_b)
                while True:
                    choice = input(
                        f"Round {round_num} match {i}/{len(decision_matches)}: [1] {a_label}  [2] {b_label}  > "
                    ).strip()
                    if choice in ("1", "2"):
                        picks[match.id] = team_a if choice == "1" else team_b
                        break
                    print("Enter 1 or 2.")

        print("\nYour bracket:")
        for line in pick_review_lines(matches_by_id, picks, teams_by_id):
            print(f"  {line}")

        if input("Save this bracket? [y/N] ").strip().lower() == "y":
            db.upsert_predictions(self.conn, tournament["id"], self.user_id, picks)
            print("Saved.")
        else:
            print("Discarded.")

    def do_mypicks(self, arg: str) -> None:
        "Show your saved predictions and current score: mypicks <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        picks = db.get_user_predictions(self.conn, tournament["id"], self.user_id)
        if not picks:
            print("You have no saved predictions for this tournament.")
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        results_by_match = db.get_outcomes(self.conn, tournament["id"]).played
        score = compute_leaderboard(matches_by_id, results_by_match, {self.user_id: picks})[0]

        slots = resolve_all_slots(matches_by_id, picks.get, feeders)
        print(render_tree(
            matches_by_id, feeders, teams_by_id, slots,
            picks.get, prediction_marker(results_by_match, picks),
        ))
        print(f"\nScore: {score_label(score)}")

    # -- match results ---------------------------------------------------------

    def do_matchresult(self, arg: str) -> None:
        "Record a real match result: matchresult <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        if tournament["status"] != "in_progress":
            print(f"'{tournament['name']}' must be locked first (use 'lock'). Current status: {tournament['status']}.")
            return
        self._pick_and_record_result(tournament, allow_overwrite=False)

    def do_matchreport(self, arg: str) -> None:
        "Report who won your match, as the current user: matchreport <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        found = reports.find_reportable_match(self.conn, tournament, self.user_id)
        if isinstance(found, str):
            print(found)
            return
        teams_by_id = db.get_teams(self.conn, tournament["id"])
        team_a, team_b = found.teams
        prompt = f"Round {found.match.played_round}: [1] {team_label(teams_by_id, team_a)}  [2] {team_label(teams_by_id, team_b)}"
        if found.vote is not None:
            prompt += f"  [t] take back your side's vote for {team_label(teams_by_id, found.vote['winner_team_id'])}"
        choice = input(prompt + "  > ").strip().lower()
        if choice == "t" and found.vote is not None:
            db.take_back_report(self.conn, found.match.id, found.team_id)
            print("Your vote is taken back.")
            self._sync_match_post(found.match.id)
            return
        if choice not in ("1", "2"):
            print("Invalid choice.")
            return
        winner_id = team_a if choice == "1" else team_b
        before = db.get_outcomes(self.conn, tournament["id"])
        outcome = db.submit_report(self.conn, tournament["id"], found.match.id, found.team_id, winner_id,
                                   self.user_id, 0)
        print(reports.vote_reply(self.conn, outcome, found, winner_id, teams_by_id))
        if outcome in ("agreed", "skipped"):
            # The bot edits any match post into this announcement, so the CLI forgets it.
            db.delete_match_post(self.conn, found.match.id)
            print(reports.ending_text(self.conn, tournament, found.match))
            if outcome == "skipped":
                for line in new_walkovers(self.conn, tournament, before):
                    print(line)
            return
        if db.get_match_post(self.conn, found.match.id) is not None:
            self._sync_match_post(found.match.id)
        elif outcome == "disputed":
            db.claim_match_post(self.conn, found.match.id, 0, found.teams)
            self._sync_match_post(found.match.id)

    def do_ready(self, arg: str) -> None:
        "Ready up for your called match, as the current user: ready <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        team_id = db.get_entrant_team_id(self.conn, tournament["id"], self.user_id)
        call = next((c for c in db.get_calls(self.conn, tournament["id"]) if team_id in (c["team_a_id"], c["team_b_id"])), None)
        if call is None:
            print("You don't have a called match waiting for you to ready up.")
            return
        print(coordination.ready_up(self.conn, call["match_id"], self.user_id, self.rooms))
        self._sync_match_post(call["match_id"])

    def do_dropout(self, arg: str) -> None:
        "Drop the current user out, giving their remaining matches to their opponents (or a 5v5 spot to the waitlist): dropout <name>"
        tournament = self._require_tournament(arg.strip(), formats=db.FORMATS)
        if tournament is None:
            return
        team_id = db.get_entrant_team_id(self.conn, tournament["id"], self.user_id)
        if tournament["format"] == "lobbies" and team_id is not None:
            if input(f"Retype '{tournament['name']}' to confirm: ").strip() != tournament["name"]:
                print("Confirmation didn't match. You're still in.")
                return
            print(lobby_flow.dropout(self.conn, tournament, team_id, "dropped", self.user_id).replace("**", ""))
            return
        team = db.get_teams(self.conn, tournament["id"]).get(
            db.get_entrant_team_id(self.conn, tournament["id"], self.user_id))
        if team is None:
            print("You aren't an entrant in that tournament.")
        elif team["withdrawn"]:
            print("You've already left that tournament.")
        elif tournament["status"] not in WITHDRAWABLE_STATUSES:
            print("That tournament has finished, so there's nothing left to drop out of.")
        elif input(f"Retype '{tournament['name']}' to confirm: ").strip() != tournament["name"]:
            print("Confirmation didn't match. You're still in.")
        else:
            lines, _ = withdraw_and_describe(self.conn, tournament, team, "dropped", self.user_id, user_label)
            print("\n".join(lines))

    def do_matchstats(self, arg: str) -> None:
        "See the pool's pick breakdown for a matchup: matchstats <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        results_by_match = db.get_results(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
        known = matches_with_both_teams(matches_by_id, slots)
        if not known:
            print("No matchups are known yet.")
            return
        match = self._choose_match(known, slots, teams_by_id)
        if match is None:
            return
        team_a, team_b = slots[match.id]
        predictions_by_user = db.get_all_predictions(self.conn, tournament["id"])
        counts = pick_distribution(matches_by_id, predictions_by_user, match.played_round, team_a, team_b)
        total = sum(counts.values())
        for team_id in (team_a, team_b):
            print(f"{team_label(teams_by_id, team_id)}: {pick_share(counts[team_id], total)}")
        print(crowd_verdict(counts, team_a, team_b, teams_by_id))

    # -- admin ---------------------------------------------------------------

    def do_admin_setstatus(self, arg: str) -> None:
        "Force a tournament's status: admin_setstatus <name> <setup|predictions_open|in_progress|completed>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print(f"usage: admin_setstatus <name> <{'|'.join(db.FORCEABLE_STATUSES)}>")
            return
        name, status = parts
        if status not in db.FORCEABLE_STATUSES:
            print(f"status must be one of: {', '.join(db.FORCEABLE_STATUSES)} (use 'conclude' to conclude)")
            return
        tournament = self._require_tournament(name)
        if tournament is None:
            return
        print(f"Status set to {db.set_tournament_status(self.conn, tournament['id'], status)}.")
        db.set_tournament_channel_if_missing(self.conn, tournament["id"], 0)

    def do_admin_delete(self, arg: str) -> None:
        "Permanently delete a tournament (asks you to retype the name): admin_delete <name>"
        name = arg.strip()
        tournament = self._require_tournament(name, needs_bracket=False, formats=db.FORMATS)
        if tournament is None:
            return
        if input(f"Type '{name}' again to confirm permanent deletion: ").strip() != name:
            print("Confirmation didn't match. Cancelled.")
            return
        db.delete_tournament(self.conn, tournament["id"])
        print(f"Deleted '{name}'.")

    def do_admin_editteam(self, arg: str) -> None:
        "Fix an entrant's name/seed: admin_editteam <name> <team> [new_name] [--seed N]"
        parts = shlex.split(arg)
        if len(parts) < 2:
            print("usage: admin_editteam <name> <team> [new_name] [--seed N]")
            return
        name, team = parts[0], parts[1]
        rest = parts[2:]
        new_name, new_seed, seed_given = None, None, False
        i = 0
        while i < len(rest):
            if rest[i] == "--seed" and i + 1 < len(rest):
                try:
                    new_seed = int(rest[i + 1])
                except ValueError:
                    print("--seed must be an integer")
                    return
                seed_given = True
                i += 2
            else:
                new_name = rest[i]
                i += 1
        if new_name is None and not seed_given:
            print("Provide a new name and/or --seed N")
            return
        tournament = self._require_tournament(name, needs_bracket=False, formats=db.FORMATS)
        if tournament is None:
            return
        team_row = db.get_team_by_name(self.conn, tournament["id"], team)
        if team_row is None:
            print(f"No entrant named '{team}' in '{name}'.")
            return
        if new_name is not None and db.team_name_taken(self.conn, tournament["id"], new_name, team_row["id"]):
            print(f"'{name}' already has an entrant named '{new_name}'.")
            return
        db.update_team(self.conn, team_row["id"], new_name, new_seed, seed_given)
        print("Updated.")

    def do_admin_linkplayer(self, arg: str) -> None:
        "Link an entrant to the user who plays as them, so its games count for their rating: admin_linkplayer <name> <entrant> <user_id>"
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: admin_linkplayer <name> <entrant> <user_id>")
            return
        name, entrant, user_id = parts
        try:
            user_id = int(user_id)
        except ValueError:
            print("user_id must be an integer")
            return
        tournament = db.get_tournament_by_name_any_status(self.conn, GUILD_ID, name)
        if tournament is None:
            print(f"No tournament named '{name}'.")
            return
        if not db.has_agreed(self.conn, GUILD_ID, user_id):
            print(f"{user_label(user_id)} hasn't agreed to link their data, so entries can't be linked to them.")
            return
        entrant_row = db.get_team_by_name(self.conn, tournament["id"], entrant)
        if entrant_row is None:
            print(f"No entrant named '{entrant}' in '{name}'.")
            return
        existing = db.team_member_ids(self.conn, entrant_row["id"])
        if existing:
            print(f"{entrant} in '{name}' is already linked to {user_label(existing[0])}.")
            return
        if not db.link_team_member(self.conn, tournament["id"], entrant_row["id"], user_id):
            print(f"{user_label(user_id)} is already linked to an entrant in '{name}'.")
            return
        print(f"Linked {user_label(user_id)} to {entrant} in '{name}'.")

    def do_admin_unlinkplayer(self, arg: str) -> None:
        "Unlink the user behind an entrant, keeping their agreement and other entries: admin_unlinkplayer <name> <entrant>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_unlinkplayer <name> <entrant>")
            return
        name, entrant = parts
        tournament = db.get_tournament_by_name_any_status(self.conn, GUILD_ID, name)
        if tournament is None:
            print(f"No tournament named '{name}'.")
            return
        entrant_row = db.get_team_by_name(self.conn, tournament["id"], entrant)
        if entrant_row is None:
            print(f"No entrant named '{entrant}' in '{name}'.")
            return
        user_id = db.unlink_entrant(self.conn, entrant_row["id"])
        if user_id is None:
            print(f"{entrant} isn't linked to anyone.")
            return
        print(f"Unlinked {user_label(user_id)} from {entrant} in '{name}'.")
        if tournament["status"] == "registration":
            print("To enter them separately, use 'regadd' with another name.")

    def do_admin_setresult(self, arg: str) -> None:
        "Force/overwrite any known matchup's result: admin_setresult <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        self._pick_and_record_result(tournament, allow_overwrite=True)

    def do_admin_clearresult(self, arg: str) -> None:
        "Delete a recorded match result or skip: admin_clearresult <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        outcomes = db.get_outcomes(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
        # Walkovers from withdrawals are undone with admin_reinstate instead.
        clearable = set(outcomes.played) | outcomes.skipped | outcomes.forfeits
        decided = sorted((m for m in matches_by_id.values() if m.id in clearable), key=bracket_order)
        if not decided:
            print("No recorded results to clear.")
            return
        match = self._choose_match(decided, slots, teams_by_id)
        if match is None or self._said_later_results_problem(tournament, match):
            return
        if input("Clear this result? [y/N] ").strip().lower() == "y":
            line, dropped = clear_result_and_describe(self.conn, tournament, match, slots[match.id], self.user_id,
                                                      user_label)
            print(line + dropped_results_note(dropped))
        else:
            print("Cancelled.")

    def do_admin_withdraw(self, arg: str) -> None:
        "Withdraw or disqualify an entrant: admin_withdraw <name> <entrant> withdrawn|disqualified"
        parts = shlex.split(arg)
        if len(parts) != 3 or parts[2] not in ("withdrawn", "disqualified"):
            print("usage: admin_withdraw <name> <entrant> withdrawn|disqualified")
            return
        found = self._require_entrant(*parts[:2], WITHDRAWABLE_STATUSES, db.FORMATS)
        if found is None:
            return
        tournament, team = found
        if team["withdrawn"]:
            print(f"{team['name']} has already left.")
            return
        if tournament["format"] == "lobbies":
            print(lobby_flow.dropout(self.conn, tournament, team["id"], parts[2], self.user_id).replace("**", ""))
            return
        lines, dropped = withdraw_and_describe(self.conn, tournament, team, parts[2], self.user_id, user_label)
        print("\n".join(lines) + dropped_results_note(dropped))

    def do_admin_reinstate(self, arg: str) -> None:
        "Undo a dropout, withdrawal or disqualification: admin_reinstate <name> <entrant>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_reinstate <name> <entrant>")
            return
        found = self._require_entrant(*parts, REINSTATABLE_STATUSES)
        if found is None:
            return
        tournament, team = found
        if not team["withdrawn"]:
            print(f"{team['name']} is still playing.")
            return
        refusal = reinstate_problem(self.conn, tournament, team["id"])
        if refusal is not None:
            print(refusal)
            return
        dropped = db.reinstate_entrant(self.conn, tournament["id"], team["id"])
        print(reinstated_line(team["name"], tournament["name"], user_label(self.user_id)) + dropped_results_note(dropped))

    def do_admin_skipmatch(self, arg: str) -> None:
        "Call a match off with no winner: admin_skipmatch <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        outcomes = db.get_outcomes(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
        skippable = [m for m in matches_with_both_teams(matches_by_id, slots) if m.id not in outcomes.skipped]
        if not skippable:
            print("No matches have both teams known yet.")
            return
        match = self._choose_match(skippable, slots, teams_by_id, outcomes.decided)
        if match is None or self._said_later_results_problem(tournament, match):
            return
        if input("Call this match off with no winner? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return
        lines, dropped = skip_and_describe(self.conn, tournament, match.id, slots[match.id], self.user_id, user_label)
        print("\n".join(lines) + dropped_results_note(dropped))

    def do_admin_matchrooms(self, arg: str) -> None:
        "Pretend the server has this many voice rooms for called matches: admin_matchrooms <count>"
        if not arg.strip().isdigit():
            print("usage: admin_matchrooms <count>")
            return
        self.rooms = list(range(1, int(arg.strip()) + 1))
        print(f"Called matches are played in {len(self.rooms)} room(s).")

    def do_admin_matchslots(self, arg: str) -> None:
        "Set how many matches the bot calls at once, or 'off' to stop: admin_matchslots <name> <N|off>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_matchslots <name> <N|off>")
            return
        tournament = self._require_tournament(parts[0], needs_bracket=False)
        match_slots = None if parts[1] == "off" else parse_match_slots(parts[1])
        if tournament is None or match_slots is False or not self._match_slots_fit(match_slots):
            return
        db.set_match_slots(self.conn, tournament["id"], match_slots)
        db.set_tournament_channel_if_missing(self.conn, tournament["id"], 0)
        print(f"'{parts[0]}' " + (f"has up to {match_slots} matches called at once." if match_slots
                                  else "no longer has its matches called."))

    def _match_slots_fit(self, match_slots: int | None) -> bool:
        """False, after saying why, if the bot can't call `match_slots` matches at once."""
        if match_slots is None:
            return True
        if not self.rooms:
            print("Set up the match rooms first with 'admin_matchrooms'.")
            return False
        if match_slots > len(self.rooms):
            print(f"There are only {len(self.rooms)} room(s), so at most {len(self.rooms)} matches can be called at once.")
            return False
        return True

    def do_admin_clearpredictions(self, arg: str) -> None:
        "Wipe a user's saved predictions: admin_clearpredictions <name> <user_id>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_clearpredictions <name> <user_id>")
            return
        name, user_id_str = parts
        tournament = self._require_tournament(name, needs_bracket=False)
        if tournament is None:
            return
        try:
            user_id = int(user_id_str)
        except ValueError:
            print("user_id must be an integer")
            return
        existing = db.get_user_predictions(self.conn, tournament["id"], user_id)
        if not existing:
            print("That user has no saved predictions for this tournament.")
            return
        if input(f"Clear {len(existing)} saved picks for user {user_id}? [y/N] ").strip().lower() == "y":
            db.clear_user_predictions(self.conn, tournament["id"], user_id)
            print("Cleared.")
        else:
            print("Cancelled.")

    def do_admin_feedback(self, arg: str) -> None:
        "Read anonymous feedback, newest first: admin_feedback [page]"
        try:
            page = int(arg.strip() or 1)
        except ValueError:
            print("page must be an integer")
            return
        entries, page, page_count = feedback_page(db.get_feedback(self.conn, GUILD_ID), page)
        if not entries:
            print("No feedback yet.")
            return
        for entry in entries:
            print(f"#{entry['id']} ({entry['created_on']}): {entry['message']}")
        print(f"Page {page} of {page_count}.")

    def do_admin_deletefeedback(self, arg: str) -> None:
        "Remove handled feedback: admin_deletefeedback <number>"
        try:
            number = int(arg.strip())
        except ValueError:
            print("usage: admin_deletefeedback <number>")
            return
        deleted = db.delete_feedback(self.conn, GUILD_ID, number)
        print(f"Deleted feedback #{number}." if deleted else f"There's no feedback #{number}.")

    def do_admin_ban(self, arg: str) -> None:
        "Block a user from the bot: admin_ban <user_id> [reason]"
        parts = arg.split(maxsplit=1)
        try:
            user_id = int(parts[0])
        except (IndexError, ValueError):
            print("usage: admin_ban <user_id> [reason]")
            return
        db.ban_user(self.conn, GUILD_ID, user_id, self.user_id, parts[1] if len(parts) > 1 else None)
        print(f"User {user_id} is banned.")

    def do_admin_unban(self, arg: str) -> None:
        "Let a banned user use the bot again: admin_unban <user_id>"
        try:
            user_id = int(arg.strip())
        except ValueError:
            print("usage: admin_unban <user_id>")
            return
        print(f"User {user_id} is unbanned." if db.unban_user(self.conn, GUILD_ID, user_id) else f"User {user_id} isn't banned.")

    def do_admin_bans(self, arg: str) -> None:
        "List banned users."
        bans = db.get_bans(self.conn, GUILD_ID)
        if not bans:
            print("Nobody is banned.")
        for line in ban_lines(bans, user_label):
            print(line)

    # -- everyone else ----------------------------------------------------------

    # -- 5v5 events -----------------------------------------------------------

    def _lobby_sign_up(self, tournament, captain: str | None) -> None:
        if captain not in lobby_flow.CAPTAIN_CHOICES:
            print(lobby_flow.CAPTAIN_QUESTION)
            captain = input("want, can or no? ").strip().lower()
            if captain not in lobby_flow.CAPTAIN_CHOICES:
                print("Nothing changed.")
                return
        linked = True
        signed_up = db.get_entrant_team_id(self.conn, tournament["id"], self.user_id) is not None
        if not signed_up and not db.has_agreed(self.conn, GUILD_ID, self.user_id):
            print(CONSENT_NOTICE.replace("**", ""))
            linked = input("Agree? [y/N] ").strip().lower() == "y"
            problem = lobby_flow.captain_problem(captain, linked, by_organizer=False)
            if problem is not None:
                print(f"{problem}\nNothing changed.")
                return
            if linked:
                db.record_agreement(self.conn, GUILD_ID, self.user_id)
            else:
                db.opt_out(self.conn, GUILD_ID, self.user_id)
        print(lobby_flow.sign_up(self.conn, tournament, self.user_name, self.user_id, linked, captain).replace("**", ""))

    def _close_lobbies(self, tournament, lobby_count: str | None, spread: str | None) -> None:
        proposed, options = tournament_builder.lobby_split_options(self.conn, tournament["id"])
        if proposed is None:
            print(f"'{tournament['name']}' can't be drawn yet: a lobby needs two players who can captain (with a "
                  f"linked account).")
            return
        split = proposed
        if lobby_count is not None:
            split = next((option for option in options if str(len(option.lobby_sizes)) == lobby_count), None)
            if split is None:
                print("Possible splits: " + "; ".join(f"--lobbies {len(o.lobby_sizes)}: {lobby_flow.split_label(o)}"
                                                      for o in options))
                return
        if spread is not None and spread not in lobbies.SPREADS:
            print(f"--spread must be one of {', '.join(lobbies.SPREADS)}")
            return
        try:
            tournament_builder.close_lobby_registration(self.conn, tournament["id"], split, spread)
        except lobbies.LobbyError as e:
            print(f"Couldn't create the lobbies: {e}")
            return
        print(f"Created the lobbies: {lobby_flow.split_label(split)}. The post follows; start check-in with "
              f"'lobby_startcheckin {tournament['name']}' when it's nearly time to play.")
        self._tend_matches()

    def _require_event(self, name: str):
        return self._require_tournament(name, needs_bracket=False, formats=("lobbies",))

    def _lobby_and_role(self, args: list[str]):
        """(event, lobby, role) for `<name> [lobby number]`, the current user's own lobby
        if no number is given, or None after saying why not. The event's creator counts
        as an organizer, since the CLI has no server permissions."""
        if not args:
            print("Tournament name required.")
            return None
        tournament = self._require_event(args[0])
        if tournament is None:
            return None
        drawn = lobby_db.get_lobbies(self.conn, tournament["id"])
        team_id = db.get_entrant_team_id(self.conn, tournament["id"], self.user_id)
        if len(args) > 1:
            lobby = next((lobby for lobby in drawn if str(lobby.number) == args[1]), None)
        else:
            lobby = next((lobby for lobby in drawn if team_id in lobby.players), None)
        if lobby is None:
            print("No such lobby. Give the lobby's number if you aren't playing in one.")
            return None
        organizer = tournament["created_by"] == self.user_id
        return tournament, lobby, lobby_flow.role_in(self.conn, lobby, self.user_id, organizer)

    def _say(self, text: str) -> None:
        print(text.replace("**", ""))
        self._tend_matches()

    def do_lobby_startcheckin(self, arg: str) -> None:
        "Open check-in for a 5v5 event (organizers): lobby_startcheckin <name> [minutes]"
        parts = shlex.split(arg)
        tournament = self._require_event(parts[0] if parts else "")
        if tournament is None:
            return
        minutes = parts[1] if len(parts) > 1 else str(lobby_flow.DEFAULT_CHECKIN_MINUTES)
        if not minutes.isdigit() or not 1 <= int(minutes) <= lobby_flow.MAX_CHECKIN_MINUTES:
            print(f"minutes must be a whole number from 1 to {lobby_flow.MAX_CHECKIN_MINUTES}")
            return
        self._say(lobby_flow.start_checkin(self.conn, tournament, int(minutes), datetime.now(timezone.utc)))

    def do_checkin(self, arg: str) -> None:
        "Check in for a 5v5 event: checkin <name>"
        tournament = self._require_event(arg.strip())
        if tournament is not None:
            self._say(lobby_flow.check_in(self.conn, tournament, self.user_id))

    def do_lobby_roomcode(self, arg: str) -> None:
        "Give the bot your lobby's drafter room code (captains and organizers): lobby_roomcode <name> <code> [lobby]"
        parts = shlex.split(arg)
        if len(parts) < 2:
            print("usage: lobby_roomcode <name> <code> [lobby]")
            return
        found = self._lobby_and_role([parts[0]] + parts[2:])
        if found is not None:
            tournament, lobby, role = found
            self._say(lobby_flow.submit_room_code(self.conn, tournament, lobby, role, self.user_id, parts[1]))

    def do_lobby_code(self, arg: str) -> None:
        "Get your lobby's room code: lobby_code <name> [lobby]"
        found = self._lobby_and_role(shlex.split(arg))
        if found is not None:
            tournament, lobby, role = found
            print(lobby_flow.get_room_code(self.conn, tournament, lobby, role).replace("**", ""))

    def do_lobby_report(self, arg: str) -> None:
        "Report everyone's points in a lobby (captains and organizers), asked player by player: lobby_report <name> [lobby]"
        found = self._lobby_and_role(shlex.split(arg))
        if found is None:
            return
        tournament, lobby, role = found
        problem = lobby_flow.report_problem(self.conn, tournament, lobby, role)
        if problem is not None:
            print(problem.replace("**", ""))
            return
        names = lobby_posts.Names(self.conn, tournament["id"], user_label)
        entered = {player: input(f"{names.plain(player)}'s points: ") for player in lobby.players}
        points = lobby_flow.parse_points(lobby, entered)
        if isinstance(points, str):
            print(points)
            return
        self._say(lobby_flow.submit_report(self.conn, tournament, lobby, role, self.user_id, points))

    def do_lobby_confirm(self, arg: str) -> None:
        "Confirm the other captain's report: lobby_confirm <name> [lobby]"
        found = self._lobby_and_role(shlex.split(arg))
        if found is not None:
            _, lobby, role = found
            self._say(lobby_flow.confirm_report(self.conn, lobby, role, self.user_id))

    def do_lobby_dispute(self, arg: str) -> None:
        "Dispute the other captain's report: lobby_dispute <name> [lobby]"
        found = self._lobby_and_role(shlex.split(arg))
        if found is not None:
            _, lobby, role = found
            self._say(lobby_flow.dispute_report(self.conn, lobby, role))

    def do_admin_setcaptain(self, arg: str) -> None:
        "Make an entrant captain of their 5v5 team: admin_setcaptain <name> <entrant>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_setcaptain <name> <entrant>")
            return
        found = self._require_entrant(*parts, ("in_progress",), ("lobbies",))
        if found is None:
            return
        tournament, team = found
        lobby = lobby_db.find_lobby_of(self.conn, tournament["id"], team["id"])
        if lobby is None:
            print(f"{team['name']} isn't playing in a lobby.")
        elif not db.team_member_ids(self.conn, team["id"]):
            print(f"{team['name']} has no linked account, so they couldn't give the room code or report.")
        else:
            lobby_db.set_captain(self.conn, lobby.id, lobby.side_of(team["id"]), team["id"])
            self._say(f"{team['name']} now captains their team in lobby {lobby.number}.")

    def do_admin_lobbyswap(self, arg: str) -> None:
        "Bring a waitlisted player into someone's 5v5 spot: admin_lobbyswap <name> <leaving> <joining>"
        parts = shlex.split(arg)
        if len(parts) != 3:
            print("usage: admin_lobbyswap <name> <leaving> <joining>")
            return
        found = self._require_entrant(*parts[:2], ("in_progress",), ("lobbies",))
        if found is None:
            return
        tournament, leaving = found
        joining = db.get_team_by_name(self.conn, tournament["id"], parts[2])
        if joining is None or not lobby_db.bring_in(self.conn, tournament["id"], leaving["id"], joining["id"]):
            print(f"{parts[1]} has to be in a lobby and {parts[2]} on the waitlist.")
            return
        self._say(f"{parts[2]} takes {parts[1]}'s spot, and {parts[1]} is first on the waitlist.")

    def do_features(self, arg: str) -> None:
        "What the bot can be used for, and what's coming."
        print("What it does:")
        for line in CURRENT_FEATURES:
            print(f"  - {line.replace('**', '')}")
        print("Coming soon:")
        for line in PLANNED_FEATURES:
            print(f"  - {line.replace('**', '')}")

    def do_feedback(self, arg: str) -> None:
        "Send anonymous feedback: feedback <message>"
        message = arg.strip()
        if not message:
            print("usage: feedback <message>")
            return
        db.add_feedback(self.conn, GUILD_ID, message)
        print("Thanks! Your feedback was sent anonymously.")

    # -- shared helpers ---------------------------------------------------------

    def _require_next_status(self, name: str, new_status: str, formats: tuple[str, ...] = ("single_elim",)):
        """The tournament, if it's at the step just before `new_status`; otherwise None."""
        tournament = self._require_tournament(name, formats=formats)
        if tournament is None:
            return None
        if not db.is_next_status(tournament["status"], new_status):
            print(f"'{name}' is {tournament['status']}, so it can't move to {new_status} from here. "
                  f"Use admin_setstatus to force a status.")
            return None
        return tournament

    def _advance_status(self, name: str, new_status: str) -> str | None:
        """Move a tournament one step along its lifecycle, refusing to skip or go back.
        Returns the status stored (see db.set_tournament_status)."""
        tournament = self._require_next_status(name, new_status)
        if tournament is None:
            return None
        return db.set_tournament_status(self.conn, tournament["id"], new_status)

    def _require_registration(self, name: str):
        """The tournament, if it's taking sign-ups; otherwise None."""
        tournament = self._require_tournament(name, needs_bracket=False, formats=db.FORMATS)
        if tournament is None:
            return None
        if tournament["status"] != "registration":
            print(f"'{name}' isn't taking sign-ups (status: {tournament['status']}).")
            return None
        return tournament

    def _require_tournament(self, name: str, needs_bracket: bool = True, formats: tuple[str, ...] = ("single_elim",)):
        """The tournament, or None after saying why not. One still taking sign-ups has no
        bracket yet, so it doesn't count unless needs_bracket=False, and one whose format
        isn't in `formats` doesn't either (bracket commands are the default)."""
        if not name:
            print("Tournament name required.")
            return None
        tournament = db.get_tournament_by_name(self.conn, GUILD_ID, name)
        if tournament is None:
            if db.get_concluded_tournament_by_name(self.conn, GUILD_ID, name) is not None:
                print(f"'{name}' has concluded. 'history_bracket', 'history_seeding' and 'history_leaderboard' show it.")
            else:
                print(f"No tournament named '{name}'. Use 'tournaments' to list them.")
            return None
        if tournament["format"] not in formats:
            print(wrong_format_message(tournament).replace("**", ""))
            return None
        if needs_bracket and tournament["status"] == "registration":
            print(f"'{name}' is still taking sign-ups, so it has no bracket yet. "
                  f"'entrants' shows who's in, and 'regclose' draws the bracket.")
            return None
        return tournament

    def _require_concluded(self, name: str):
        """The concluded tournament called `name`, or None after saying why not."""
        tournament = db.get_concluded_tournament_by_name(self.conn, GUILD_ID, name)
        if tournament is None:
            if db.get_tournament_by_name(self.conn, GUILD_ID, name) is not None:
                print(f"'{name}' hasn't concluded yet. 'bracket' and 'leaderboard' show it.")
            else:
                print(f"No concluded tournament named '{name}'. Use 'history' to list them.")
        return tournament

    def _require_entrant(self, name: str, entrant: str, allowed_statuses: tuple[str, ...],
                         formats: tuple[str, ...] = ("single_elim",)):
        """(tournament, entrant) if the tournament's status is one of `allowed_statuses`, else None after saying why."""
        tournament = self._require_tournament(name, formats=formats)
        if tournament is None:
            return None
        if tournament["status"] not in allowed_statuses:
            print(f"'{name}' has finished.")
            return None
        team = db.get_team_by_name(self.conn, tournament["id"], entrant)
        if team is None:
            print(f"No entrant named '{entrant}' in '{name}'.")
            return None
        return tournament, team

    def _name_taken(self, name: str) -> bool:
        """True, after saying why, if a tournament already has `name`."""
        existing = db.get_tournament_by_name_any_status(self.conn, GUILD_ID, name)
        if existing is not None:
            print(name_taken_message(existing))
        return existing is not None

    def _choose_match(self, matches: list, slots: dict, teams_by_id: dict, decided: set[int] = frozenset()):
        """Print a numbered list of matches and return the one the user picks, or None."""
        for i, match in enumerate(matches, start=1):
            tag = " (has result)" if match.id in decided else ""
            print(f"[{i}] {matchup_label(match, slots, teams_by_id)}{tag}")
        choice = input("Pick a match number: ").strip()
        try:
            index = int(choice) - 1
            if index < 0:
                raise ValueError
            return matches[index]
        except (ValueError, IndexError):
            print("Invalid choice.")
            return None

    def _said_later_results_problem(self, tournament, match: Match) -> bool:
        """Whether later results stop `match`'s result from changing, after saying so."""
        refusal = later_results_problem(self.conn, tournament, [match.id])
        if refusal is not None:
            print(refusal)
        return refusal is not None

    def _pick_and_record_result(self, tournament, allow_overwrite: bool):
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        outcomes = db.get_outcomes(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)

        if allow_overwrite:
            # Walkovers are undone with admin_reinstate, not overwritten.
            candidates = [m for m in matches_with_both_teams(matches_by_id, slots)
                          if m.id not in outcomes.from_withdrawals]
        else:
            candidates = resolvable_matches(matches_by_id, feeders, outcomes.winners.get, outcomes.decided)

        if not candidates:
            print(
                "No matches have both teams known yet."
                if allow_overwrite
                else "No matches are ready to record (either the bracket is fully decided, "
                "or earlier rounds aren't finished yet)."
            )
            return
        match = self._choose_match(candidates, slots, teams_by_id, outcomes.decided)
        if match is None:
            return
        team_a, team_b = slots[match.id]
        a_label = team_label(teams_by_id, team_a)
        b_label = team_label(teams_by_id, team_b)

        winner_choice = input(f"Winner: [1] {a_label}  [2] {b_label}  > ").strip()
        if winner_choice not in ("1", "2"):
            print("Invalid choice.")
            return
        winner_id = team_a if winner_choice == "1" else team_b
        loser_id = team_b if winner_choice == "1" else team_a
        if outcomes.winners.get(match.id) == winner_id:
            print(f"{team_label(teams_by_id, winner_id)} already won this match, so there's nothing to change.")
            return
        if match.id in outcomes.decided:
            if self._said_later_results_problem(tournament, match):
                return
            if input("This match already has a different result. Overwrite? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return
        line, dropped = set_result_and_describe(self.conn, tournament, match, winner_id, loser_id, self.user_id,
                                                user_label)
        print(line + dropped_results_note(dropped))

    # -- exit ---------------------------------------------------------------

    def do_exit(self, arg: str) -> bool:
        "Exit the CLI."
        print("Bye.")
        return True

    do_quit = do_exit

    def do_EOF(self, arg: str) -> bool:
        print()
        return True


if __name__ == "__main__":
    WinningTicketCLI().cmdloop()
