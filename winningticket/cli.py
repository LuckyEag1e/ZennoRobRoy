#!/usr/bin/env python3
"""Local REPL for exercising WinningTicket's command logic without a live Discord bot.

Talks directly to db.py/bracket.py/csv_import.py -- the same modules the real
slash commands use -- so behavior here matches the bot exactly. Uses its own
sqlite file (data/cli.db) so it never touches a real bot's data.
"""

from __future__ import annotations

import cmd
import shlex
from pathlib import Path

import csv_import
import db
from ascii_bracket import prediction_marker, real_result_marker, render_tree
from bracket import (
    bracket_order,
    compute_leaderboard,
    is_bye,
    matches_in_round,
    matches_with_both_teams,
    next_opponent,
    pick_distribution,
    resolvable_matches,
    resolve_all_slots,
    resolve_slot,
    rounds,
)
from labels import (
    crowd_verdict,
    dropped_results_note,
    matchup_label,
    pick_review_lines,
    pick_share,
    score_label,
    team_label,
)

CLI_DB_PATH = Path(__file__).parent / "data" / "cli.db"
GUILD_ID = 1  # single pretend guild for local testing


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
        self._update_prompt()

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
            print("No tournaments yet. Use 'create' to make one.")
            return
        for name in names:
            tournament = db.get_tournament_by_name(self.conn, GUILD_ID, name)
            print(f"- {name} [{tournament['status']}]")

    def do_create(self, arg: str) -> None:
        "Create a tournament from a CSV file: create <name> <csv_path>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: create <name> <csv_path>")
            return
        name, csv_path = parts
        if db.get_tournament_by_name(self.conn, GUILD_ID, name) is not None:
            print(f"A tournament named '{name}' already exists.")
            return
        try:
            content = Path(csv_path).read_text()
        except OSError as e:
            print(f"Couldn't read {csv_path}: {e}")
            return
        try:
            rows = csv_import.parse_csv(content)
            csv_import.import_tournament(self.conn, GUILD_ID, name, self.user_id, rows)
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
        if self._advance_status(arg.strip(), "in_progress"):
            print(f"'{arg.strip()}' locked. Use 'matchresult' to record real outcomes.")

    def do_conclude(self, arg: str) -> None:
        "Archive a finished tournament, keeping its results: conclude <name>"
        if self._advance_status(arg.strip(), "concluded"):
            print(f"'{arg.strip()}' concluded. Its results are kept, but it can't be selected any more.")

    def do_bracket(self, arg: str) -> None:
        "Show the real bracket's current progress: bracket <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        results_by_match = db.get_results(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
        print(render_tree(
            matches_by_id, feeders, teams_by_id, slots,
            results_by_match.get, real_result_marker(results_by_match),
        ))

    def do_leaderboard(self, arg: str) -> None:
        "Show the current standings: leaderboard <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id = db.get_matches(self.conn, tournament["id"])
        results_by_match = db.get_results(self.conn, tournament["id"])
        predictions_by_user = db.get_all_predictions(self.conn, tournament["id"])
        scores = compute_leaderboard(matches_by_id, results_by_match, predictions_by_user)
        if not scores:
            print("No predictions submitted yet.")
            return
        participant_word = "participant" if len(scores) == 1 else "participants"
        print(f"{len(scores)} {participant_word}\n")
        for rank, s in enumerate(scores, start=1):
            print(f"{rank}. user {s.user_id}: {score_label(s)}")

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
        results_by_match = db.get_results(self.conn, tournament["id"])
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
        db.set_tournament_status(self.conn, tournament["id"], status)
        print(f"Status set to {status}.")

    def do_admin_delete(self, arg: str) -> None:
        "Permanently delete a tournament (asks you to retype the name): admin_delete <name>"
        name = arg.strip()
        tournament = self._require_tournament(name)
        if tournament is None:
            return
        if input(f"Type '{name}' again to confirm permanent deletion: ").strip() != name:
            print("Confirmation didn't match. Cancelled.")
            return
        db.delete_tournament(self.conn, tournament["id"])
        print(f"Deleted '{name}'.")

    def do_admin_editteam(self, arg: str) -> None:
        "Fix a team's name/seed: admin_editteam <name> <team> [new_name] [--seed N]"
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
        tournament = self._require_tournament(name)
        if tournament is None:
            return
        team_row = db.get_team_by_name(self.conn, tournament["id"], team)
        if team_row is None:
            print(f"No team named '{team}' in '{name}'.")
            return
        db.update_team(self.conn, team_row["id"], new_name, new_seed, seed_given)
        print("Updated.")

    def do_admin_setresult(self, arg: str) -> None:
        "Force/overwrite any known matchup's result: admin_setresult <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        self._pick_and_record_result(tournament, allow_overwrite=True)

    def do_admin_clearresult(self, arg: str) -> None:
        "Delete a recorded match result: admin_clearresult <name>"
        tournament = self._require_tournament(arg.strip())
        if tournament is None:
            return
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        results_by_match = db.get_results(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)
        decided = sorted((m for m in matches_by_id.values() if m.id in results_by_match), key=bracket_order)
        if not decided:
            print("No recorded results to clear.")
            return
        match = self._choose_match(decided, slots, teams_by_id)
        if match is None:
            return
        if input("Clear this result? [y/N] ").strip().lower() == "y":
            dropped = db.clear_result(self.conn, tournament["id"], match.id)
            print("Cleared." + dropped_results_note(dropped))
        else:
            print("Cancelled.")

    def do_admin_clearpredictions(self, arg: str) -> None:
        "Wipe a user's saved predictions: admin_clearpredictions <name> <user_id>"
        parts = shlex.split(arg)
        if len(parts) != 2:
            print("usage: admin_clearpredictions <name> <user_id>")
            return
        name, user_id_str = parts
        tournament = self._require_tournament(name)
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

    # -- shared helpers ---------------------------------------------------------

    def _advance_status(self, name: str, new_status: str) -> bool:
        """Move a tournament one step along its lifecycle, refusing to skip or go back."""
        tournament = self._require_tournament(name)
        if tournament is None:
            return False
        if not db.is_next_status(tournament["status"], new_status):
            print(f"'{name}' is {tournament['status']}, so it can't move to {new_status} from here. "
                  f"Use admin_setstatus to force a status.")
            return False
        db.set_tournament_status(self.conn, tournament["id"], new_status)
        return True

    def _require_tournament(self, name: str):
        if not name:
            print("Tournament name required.")
            return None
        tournament = db.get_tournament_by_name(self.conn, GUILD_ID, name)
        if tournament is None:
            print(f"No tournament named '{name}'. Use 'tournaments' to list them.")
            return None
        return tournament

    def _choose_match(self, matches: list, slots: dict, teams_by_id: dict, results_by_match: dict | None = None):
        """Print a numbered list of matches and return the one the user picks, or None."""
        for i, match in enumerate(matches, start=1):
            tag = " (has result)" if results_by_match and match.id in results_by_match else ""
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

    def _pick_and_record_result(self, tournament, allow_overwrite: bool):
        matches_by_id, feeders, teams_by_id = db.load_bracket(self.conn, tournament["id"])
        results_by_match = db.get_results(self.conn, tournament["id"])
        slots = resolve_all_slots(matches_by_id, results_by_match.get, feeders)

        if allow_overwrite:
            candidates = matches_with_both_teams(matches_by_id, slots)
        else:
            candidates = resolvable_matches(matches_by_id, feeders, results_by_match.get, set(results_by_match))

        if not candidates:
            print(
                "No matches have both teams known yet."
                if allow_overwrite
                else "No matches are ready to record (either the bracket is fully decided, "
                "or earlier rounds aren't finished yet)."
            )
            return
        match = self._choose_match(candidates, slots, teams_by_id, results_by_match)
        if match is None:
            return
        team_a, team_b = slots[match.id]
        a_label = team_label(teams_by_id, team_a)
        b_label = team_label(teams_by_id, team_b)

        if match.id in results_by_match:
            if input("This match already has a result. Overwrite? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return

        winner_choice = input(f"Winner: [1] {a_label}  [2] {b_label}  > ").strip()
        if winner_choice not in ("1", "2"):
            print("Invalid choice.")
            return
        winner_id = team_a if winner_choice == "1" else team_b
        loser_id = team_b if winner_choice == "1" else team_a
        dropped = db.record_result(self.conn, tournament["id"], match.id, winner_id, self.user_id)

        winner_label = team_label(teams_by_id, winner_id)
        loser_label = team_label(teams_by_id, loser_id)
        if match.feeds_into_match_id is None:
            print(f"Recorded: {winner_label} defeats {loser_label}. CHAMPION: {winner_label}!{dropped_results_note(dropped)}")
            return
        opponent = next_opponent(match, feeders, db.get_results(self.conn, tournament["id"]).get)
        print(
            f"Recorded: {winner_label} defeats {loser_label}. "
            f"Advances to Round {matches_by_id[match.feeds_into_match_id].played_round} vs {team_label(teams_by_id, opponent)}."
            f"{dropped_results_note(dropped)}"
        )

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
