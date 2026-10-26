# WinningTicket

A Discord bot for running a bracket prediction pool (like a March Madness pool):
an admin imports the already-seeded bracket, members each fill out their own
predicted bracket, and as real results come in the bot scores everyone against
reality.

## Setup

1. Create a Discord application + bot at https://discord.com/developers/applications.
   Under **Bot**, reset/copy the token. No privileged gateway intents need to be
   enabled — this bot never reads message content, member lists, or presences.
2. Under **OAuth2 > URL Generator**, select scopes `bot` and `applications.commands`,
   and bot permissions at minimum `Send Messages`, `Embed Links`, `Use Application Commands`.
   Use the generated URL to invite the bot to your server.
3. `python3 -m venv .venv && source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. `cp .env.example .env` and fill in `DISCORD_BOT_TOKEN` (and `DEV_GUILD_ID` with
   your test server's ID for instant command sync during development — global
   command sync can take up to an hour to propagate).
6. `python bot.py`

## CSV format

One row per round-1 matchup, top-to-bottom bracket order. Row count must be a
power of 2 (2, 4, 8, 16, 32, ...).

```csv
team_a,team_b,seed_a,seed_b
Alpha,Delta,1,8
Beta,Gamma,4,5
```

`seed_a`/`seed_b` are optional and purely cosmetic.

**Byes:** leave exactly one of `team_a`/`team_b` blank to give that entrant a first-round
bye (they skip round 1 and advance straight to round 2). Byes never appear in `/predict`,
`/match result`, or `/match stats` -- they're invisible to players and auto-resolve with
no admin action needed. They still show up in `/tournament bracket` and `/mypicks`,
labeled `(bye)`, so the bracket shape stays clear. Both sides can't be blank in the same row.
Two byes in neighbouring rows meet in their first game, so that game counts as round 1.

```csv
team_a,team_b
Ash,Birch
Cedar,
Dogwood,Elm
Fir,
```

`example_bracket.csv` is a small ready-made bracket (with seeds and byes) for trying things
out, e.g. `create Demo example_bracket.csv` in the local CLI below.

## Usage

Admin:
- `/registration open name:<name> [closes_in_hours:<hours>]`: take sign-ups instead of
  uploading a CSV. Members enter with `/register`. Sign-ups close after `closes_in_hours`
  (or with `/registration close name:<name>`), and then the entrants are drawn into a
  bracket at random, with byes filling it out to a power of two.
- `/registration add tournament:<name> [user:<member>] [name:<text>]`: sign up a member
  for them, or an entrant with no Discord account. `/registration remove` withdraws anyone.
- `/tournament create name:<name> csv_file:<attach bracket.csv>`
- `/tournament open name:<name>` — let members start `/predict`
- `/tournament lock name:<name>` — stop prediction submissions, start recording real results
- `/match result tournament:<name>` — pick a ready match and record who really won
- `/tournament conclude name:<name>` — archive a completed tournament and post its podium
  and top pick'em scorers. Its results stay in the database (for future seeding), but it
  disappears from every command and its name can be reused.
- `/admin ...` — troubleshooting/correction commands (force status, force/overwrite a
  result, clear a result, edit a team's name/seed, clear a user's predictions, delete
  a tournament). All require the Manage Server permission.
- `/admin feedback [page:<n>]`: read the anonymous feedback members have sent, and
  `/admin deletefeedback number:<n>` to remove it once handled.
- `/admin ban user:<member> [reason:<text>]`: block a member from every bot command
  (`/admin unban` undoes it, `/admin bans` lists them). Members with Manage Server can't
  be banned.

Everyone:
- `/features`: a short summary of what the bot does and what's planned
- `/feedback`: send the organizers anonymous feedback (once every 5 minutes)
- `/register tournament:<name>` / `/unregister tournament:<name>`: sign up for, or withdraw
  from, a tournament that's taking entrants
- `/registration entrants name:<name>`: see who has signed up
- `/predict tournament:<name>` — click through and fill out your bracket (round 2+
  options are built from your own picks, just like ESPN/Yahoo bracket pools)
- `/mypicks tournament:<name>` — see your saved bracket and current score
- `/tournament bracket name:<name>` — see the real bracket's progress
- `/tournament leaderboard name:<name>` — see standings

## Local CLI (no Discord needed)

`cli.py` is a terminal REPL that drives the exact same `db.py`/`bracket.py`/`csv_import.py`
logic the bot uses, against its own `data/cli.db` (separate from the real bot's
database), so you can test full tournament flows without a bot token or server:

```
python cli.py
```

Commands mirror the slash commands: `create <name> <csv>`, `regopen <name> [--closes-in HOURS]`, `register <name>`,
`unregister <name>`, `entrants <name>`, `regadd <name> <entrant> [user_id]`, `regremove <name> <entrant>`,
`regclose <name>`, `open <name>`, `predict <name>`,
`lock <name>`, `conclude <name>`, `matchresult <name>`, `matchstats <name>`, `mypicks <name>`, `bracket <name>`,
`leaderboard <name>`, `tournaments`, `features`, `feedback <message>`, and the admin set: `admin_setstatus`, `admin_delete`,
`admin_editteam`, `admin_setresult`, `admin_clearresult`, `admin_clearpredictions`, `admin_feedback`,
`admin_deletefeedback`, `admin_ban`, `admin_unban`, `admin_bans`.
Use `as <user_id> <name>` to switch which simulated user you're acting as (so you can
submit multiple different predictions and see the leaderboard play out). Commands that
take more than one argument (like `create`) need quotes around a multi-word name;
single-argument commands (like `open`) take the whole rest of the line as the name, so
don't quote those. Type `help` to list everything, or `help <command>` for usage.
- `/match stats tournament:<name>` — see what percentage of the pool picked each
  team in a given matchup, and who the crowd favorite is

## Tests

```
pip install pytest
pytest
```

Covers bracket-tree generation, slot derivation (including upset propagation),
scoring, pick-distribution stats, and CSV validation — no live bot connection
needed. It also plays one simulated tournament per scenario (see below).

### Simulated tournaments

`python -m sim` plays whole tournaments through the real slash commands, buttons,
menus and forms, using a fake Discord layer and a throwaway database. Simulated
players and an organizer register, predict, check the leaderboard, make typos,
double-click, abandon brackets, poke at admin commands and so on, each according to
a competence profile. After every step it checks for crashes, unanswered
interactions, messages Discord would reject, organizer commands in public posts,
DMs and inconsistent data; at the end it compares the champion and every score
against the results it played.

```
python -m sim --list                         # the scenarios
python -m sim                                # every scenario, 5 seeds each
python -m sim --scenario chaos --seed 3 --transcript   # replay one run step by step
python -m sim --coverage                     # which commands ran at which stage
```

Scenarios, competence profiles and organizer styles live in `sim/scenarios.py`
and `sim/actors.py`. Buttons on real Discord still get a manual check before release.

## Deployment

The bot is designed to run persistently on a remote host as a systemd service, so it's
available for testing without needing a dev machine running. Deployment target (host, path,
remote user) is configured locally, not hardcoded in the repo — see `deploy.local.sh.example`.

**One-time setup on a fresh host:**

```
cp deploy.local.sh.example deploy.local.sh   # fill in your real host/path/user
./setup-remote.sh
```

Creates the remote directory, syncs code (including `data/` and `.env` this one time only),
sets up the venv, and installs+enables the systemd service from
`deploy/winningticket.service.template`.

**Updating the deployed code:**

```
./deploy.sh
```

Syncs local code to the host and restarts the service. Safe to run anytime — it
deliberately never touches the remote `data/tourney.db` or `.env`, so it can't clobber
live tournament data or the bot token. Only run `bot.py` in ONE place at a time (either
locally or on the remote host, never both) — two instances on the same Discord token will
race over gateway events and their databases will silently diverge.

**Operating the deployed bot** (replace `$DEPLOY_HOST`/`$DEPLOY_SERVICE_NAME` with your own,
or `source deploy.local.sh` first so they're set):

```
ssh $DEPLOY_HOST sudo systemctl status $DEPLOY_SERVICE_NAME    # check it's running
ssh $DEPLOY_HOST sudo journalctl -u $DEPLOY_SERVICE_NAME -f    # tail live logs
ssh $DEPLOY_HOST sudo systemctl restart $DEPLOY_SERVICE_NAME   # restart without redeploying
```
