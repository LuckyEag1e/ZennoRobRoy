# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this directory.

WinningTicket is a Python (discord.py) bot that runs a bracket prediction pool. It shares nothing with the other bot in this repo, so run every command from inside `winningticket/`.

## Commands

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # plus `pip install pytest` for tests
python bot.py                            # needs DISCORD_BOT_TOKEN in .env
python cli.py                            # terminal REPL, no Discord needed (uses data/cli.db)
pytest                                   # all tests
pytest tests/test_scoring.py::test_scoring_wrong_pick_awards_zero   # single test
./deploy.sh                              # rsync to remote host + restart systemd service
```

Setting `DEV_GUILD_ID` in `.env` syncs slash commands to that one guild instantly. Without it, the bot syncs globally, which can take up to an hour. If commands show up duplicated after switching between the two, run `clear_global_commands.py`.

## Architecture

Logic is layered so most of it can be tested without Discord:

- `bracket.py`: pure logic with no discord.py or sqlite3 imports, on purpose. It generates the tree, derives slots, scores, and computes pick distributions on plain dataclasses. Unit tests target this module.
- `db.py`: the SQLite layer (`data/tourney.db`) with no discord.py imports. It converts rows to `bracket.Match`, owns the `STATUSES` list, and `load_bracket()` returns the `(matches_by_id, feeders_by_target, teams_by_id)` trio most commands need.
- `labels.py`: plain-text labels (teams, matchups, scores, pick shares) used by both the embeds and the CLI, so the two stay worded the same.
- `csv_import.py`: validates a round-1 CSV and creates the tournament and teams.
- `tournament_builder.py`: saves the match tree for round-1 pairs (used by both CSV import and closing registration). `seeding.py` is pure logic that turns an ordered list of entrants into those pairs, giving byes to the top seeds.
- `cogs/*.py`: slash commands and button/select views. `common.py` holds helpers shared across cogs (tournament lookup, autocomplete, `TournamentSession` for multi-step flows, match select menus). `embeds.py` and `ascii_bracket.py` handle rendering.
- `cli.py`: drives the same `db`/`bracket`/`labels`/`csv_import` code as the cogs. When you add or change a command, keep its CLI counterpart in step. `example_bracket.csv` is a small test bracket for it. Real tournament CSVs are gitignored because they contain people's names.

Concepts that span several files:

- **Only round 1 stores teams.** Later rounds' teams are derived each time through `resolve_slot`/`resolve_all_slots` by following `feeds_into_match_id`/`feeds_into_side` back to the feeder winners. The `winner_of` callback decides whose bracket gets resolved: point it at real `match_results` for reality, or at one user's predictions for their own bracket. `/predict` only offers round 2+ choices that come from the user's earlier picks.
- **Played rounds:** `Match.round` is the tree column, but everything players see, and scoring, uses `Match.played_round`, set by `bracket.assign_played_rounds` when `db` loads a bracket (a bye's is 0, since no game is played). It stays `None` until then, so hand-built test fixtures go through `tests/helpers.numbered`. The two only differ when two byes meet: that game sits in tree round 2 but is played in round 1.
- **Scoring** compares the predicted winner with the actual winner at the same `match_id`. A correct pick is worth `2 ** (played_round - 1)` points.
- **Byes** are round-1 matches with exactly one team. They advance automatically through `effective_winner`, never get a result row, and are hidden from `/predict`, `/match result`, and `/match stats`. Uneven brackets can also make a game's winner wait out a later round; `bracket.byes_in_round` finds both kinds for the bracket summary and `/mypicks`.
- **Registration:** `/registration open` creates a tournament at status `registration` with no matches. Each `/register` adds a one-person team plus a `team_members` row linking it to the Discord user. Teams with no members (CSV teams, and entrants an organizer adds by name with `/registration add`) have no Discord account behind them. Closing (by hand, or by the once-a-minute loop in `cogs/registration.py` once `registration_closes_at` passes) draws the teams into a bracket through `tournament_builder.close_registration` and moves the tournament to `setup`. Commands that need a bracket must check for `registration` first.
- **Tournament status** moves `registration` → `setup` (closing registration; CSV tournaments start at `setup`) → `predictions_open` (`/tournament open`) → `in_progress` (`/tournament lock`) → `completed` → `concluded` (`/tournament conclude`). `db.get_tournament_by_name` and the autocomplete skip concluded tournaments, so they keep their data but can't be selected, and their names can be reused. `db.init_db` migrates older databases to allow this. Open and lock only move one step forward (`db.is_next_status`); `/admin setstatus` can force any status except `registration` and `concluded` (`db.FORCEABLE_STATUSES`): concluding can't be undone from Discord, and a tournament can only enter or leave registration by being opened or closed. Cogs gate their actions on the current status.
- **Results** are only written through `db.record_result` and `db.clear_result`. They drop later results whose winner is no longer in that match (`bracket.stale_results`) and keep `completed` in step with whether the final is decided.
- **Admin permissions:** Discord ignores `default_permissions` on subcommands, so it's only set on the all-admin `/admin` group. Admin subcommands in groups members also use (`/tournament`, `/registration`, `/match`, `/help`) use `common.requires_manage_server`, a check at run time. Every command is guild-only.
- **Select menus** hold at most 25 options, so match choices go through `common.MatchPicker`, which pages.

## Deployment

The bot runs as a systemd service on a remote host. The host, path, and user live in the gitignored `deploy.local.sh` (template: `deploy.local.sh.example`), and `DEPLOY_DIR` must be an absolute path because a `~` would expand on the local machine. `deploy.sh` never touches the remote `data/tourney.db` or `.env`. Only run one instance per bot token at a time, either locally or on the remote host. Two instances race over gateway events, and their databases drift apart without any warning.
