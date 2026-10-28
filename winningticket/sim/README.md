# The WinningTicket simulator

`python -m sim` plays whole tournaments against the bot's real code. Simulated players and
organizers sign up, predict, check the leaderboard, record results and make the mistakes real
people make. A fake Discord layer stands in for Discord and a throwaway database stands in
for the real one. After every step, and again at the end, the simulator checks that nothing
broke.

Use it before a deploy, after changing anything a command touches, and whenever a new
command or format arrives. It replaces a lot of clicking around on a test server, though not
all of it (see [What it doesn't test](#what-it-doesnt-test)).

## Running it

Run these from inside `winningticket/`:

```
python -m sim --list                                   # the scenarios
python -m sim                                          # every scenario, seeds 0 to 4
python -m sim --scenario chaos --seeds 20              # one scenario, more seeds
python -m sim --scenario chaos --seed 3 --transcript   # replay one run step by step
python -m sim --coverage                               # which commands ran at which stage
```

Each run prints its step count and any problems. The exit code is 1 if anything was found,
so it works in scripts. `pytest` also runs seed 0 of every scenario except `large`, through
`tests/test_simulation.py`.

Runs are deterministic: the same scenario and seed always play out the same way, so a problem
found on a seed can be replayed exactly. Each run takes a fraction of a second; `large` takes
a few seconds.

## How a run works

1. **Setup.** `Simulation.create` builds a fresh database in a temporary folder and loads the
   bot's cogs through the same `setup()` functions `bot.py` uses, so the simulator runs exactly
   the commands the bot has.
2. **A fake Discord** (`discord_fakes.py`) stands in for users, a channel, messages and
   interactions. It records every message the bot sends, and whether it was public or
   private, and behaves like Discord where the cogs depend on it:
   - an interaction can only be answered once
   - a follow-up needs an answer first
   - private replies are only visible to the person they were sent to
   - deleted messages can't be edited
   - the channel has a history.
3. **The harness** (`harness.py`) does what Discord and `bot.py` would. To run a command it:
   - hides `/admin` from members without Manage Server
   - runs the bot's ban check
   - runs the command's permission checks
   - calls the real command.

   Actors can also click buttons, pick from menus, fill in forms and use autocomplete. Each
   action is a numbered **step** in the transcript.
4. **Actors** (`actors.py`) are players and organizers with a competence profile that sets how
   often they slip up (see [Actors](#actors)). Each turn, a player does the main thing for the
   tournament's stage (signing up, then predicting), and maybe looks around, goes off-script,
   pokes at admin commands, sends feedback or chats.
5. **The organizer follows the tournament's stages** (`scenarios.py`):
   - opens sign-ups (or uploads a CSV)
   - adds and tidies guest entrants
   - closes sign-ups by hand or with a deadline
   - opens predictions, then locks them
   - records results until there's a champion
   - concludes.

   Between stages they do some housekeeping: reading and clearing feedback, and banning and
   unbanning someone who keeps poking at admin commands.
6. **Real results come from hidden skill.** Every entrant has a skill rating, and each match's
   winner is drawn from the two ratings, Elo-style. It doesn't depend on anyone's predictions.
   The simulator remembers the real outcome of every matchup it played.
7. **At the end**, the bot's stored results, champion, saved brackets and leaderboard points
   are compared with what was actually played.

### People acting at the same time

Real servers overlap: ten people fill in brackets at once, and two organizers record results
side by side. A scenario can turn on two levels of overlap:

- **`crowded`**: players' flows run together. Each flow pauses before every Discord action, and
  a seeded random choice decides whose action comes next (`harness.Crowd`). Two players' clicks
  interleave, a menu can go out of date while someone else acts, and so on.
- **`simultaneous`**: the bot also handles interactions at the same instant. Every fake Discord
  call pauses as if waiting on the network, so other actors' steps can run *inside* the bot's
  code, wherever it waits on Discord, the way discord.py interleaves tasks. This is what catches
  "check, wait, then write" races.

With overlap on, a step's lines are written to the transcript when the step **finishes**, so
step numbers (which count when steps *started*) can appear out of order. Each step keeps its
own lines, so they never mix.

`co_organizer` adds a second organizer recording results alongside the first.

## What it checks

**After every step:**

- **Crashes:** any exception from the bot's code.
- **No reply:** an interaction the bot never answered, which Discord shows as "The application
  did not respond".
- **Logged errors:** anything the bot logs at error level, including errors it recovers from,
  such as a failed refresh or a bracket that couldn't be drawn.
- **Messages Discord would reject:** empty messages, content over 2,000 characters, embed
  limits (title, description, 25 fields, 6,000 characters in total, no empty fields), button
  labels, and menus with no options or over 25.
- **Organizer commands named in public posts.** The simulator works out which commands are
  organizer-only from their permission checks, so new ones are covered automatically.
- **DMs:** the bot should never send one.
- **Database consistency:**
  - a tournament in registration has no bracket
  - a bracket has exactly one final
  - no result names a team that isn't in that match
  - the status agrees with whether the final is decided
  - no blank feedback is stored.

**As it happens:**

- **Unconfirmed overwrites:** a match that already has a result being recorded again, other than
  through `/admin setresult`'s confirmation. The check wraps `db.record_result` for the run, so
  it fires at the moment of writing and can't be blamed on another step running at once.
- **Pointers to posts out of view:** a private "it's just above" link from `/tournament
  bracket` or `/tournament leaderboard` that points at a post that wasn't in view, either when
  the member asked or when the reply reached them, or that was deleted before the bot started
  answering. A post replaced while the answer is on its way doesn't count, since no bot can
  prevent that. Linking a post that another request had already replaced, before it got around
  to deleting it, looks the same from Discord's side, so `tests/test_live_posts.py` covers that
  race instead.
- **Channel noise:** a member's burst of commands putting more than 2 new public posts in the
  channel (the `spam` scenario).
- **Bans on open views:** a member banned while their feedback form or stats menu is open must
  not be able to use it.
- **Seeding:** when sign-ups close with seeding by rating, every entrant gets a seed and the
  rated ones are seeded best first. Where unrated players go is left unchecked, because the
  club hasn't decided it yet.

**At quiet moments:** after a refresh of the live posts, every live bracket and leaderboard post
must match a fresh render.

**At the end:**

- the tournament reached `concluded` (each one, in a scenario that plays several)
- every stored result is the real winner of the matchup played
- the stored champion won the final
- every saved bracket is complete and consistent with its own earlier picks
- every predictor's points match an independent recount.

## Scenarios

| Scenario | Who | What it exercises |
|---|---|---|
| `smooth` | 8 diligent players, careful organizer | The sunny day; nothing should go wrong. Steps run one at a time, so it's the easiest transcript to read. |
| `typical` | 4 diligent, 6 casual, 2 chaotic, 1 guest | A realistic mix acting at once (`crowded`, `simultaneous`), including two players with the same display name. |
| `chaos` | 2 diligent, 3 casual, 7 chaotic, 2 guests | Everyone at once, plus two sloppy organizers recording results side by side, a sign-up deadline, wrong results and corrections. |
| `large` | 40 players, 3 guests | A 64-line bracket, long menus that page, a long leaderboard. |
| `spam` | 6 casual, 1 spammer | One member firing public-posting commands as fast as they can. |
| `csv` | 8 players predicting an uploaded bracket | `/tournament create` from a CSV, with two organizers recording results. |
| `season` | 4 diligent, 3 casual, 1 chaotic, 2 guests | Two tournaments in a row under the same name. In between, a guest joins Discord and the organizer links their old entry to them with `/admin linkteam`, so the second tournament is seeded by real ratings. |

## Actors

**Players** have a competence profile. Each number is the chance of doing that thing on a turn:

| | diligent | casual | chaotic | spammer |
|---|---|---|---|---|
| typo (mistypes the tournament name instead of using autocomplete) | 0 | 0.05 | 0.3 | 0 |
| off-script (a command that doesn't fit the stage) | 0.02 | 0.1 | 0.4 | 0 |
| double-click (or a click on a step that's already gone) | 0 | 0.05 | 0.3 | 0 |
| abandon `/predict` partway (and maybe come back later) | 0 | 0.15 | 0.3 | 0 |
| curiosity (bracket, leaderboard, picks, stats, entrants) | 0.3 | 0.5 | 0.8 | 0.2 |
| skip predicting | 0 | 0.2 | 0.1 | 0 |
| poke at organizer commands | 0 | 0.02 | 0.25 | 0 |
| feedback (sometimes blank, or two forms at once) | 0.02 | 0.05 | 0.3 | 0 |
| spam a burst of public-posting commands | 0 | 0 | 0 | 1 |
| chat in the channel | 0.3 | 0.3 | 0.3 | 0.3 |

How well they guess winners also varies: their sense of each player's skill is off by about
±200 rating points for diligent players, ±350 for casual and ±600 for chaotic, so the
leaderboard spreads out the way a real pool's does.

**Organizers** are `careful` (no mistakes, closes sign-ups by hand) or `sloppy`. A sloppy
organizer runs commands too early or twice, records the wrong winner and later corrects it,
clears a result they just recorded, and uses a sign-up deadline.

## What it doesn't test

The simulator only knows what the fake Discord layer models. It doesn't cover:

- **Real Discord timing and limits:** network delay, the 3-second window to answer an
  interaction, rate limits and retries. Every fake call returns instantly.
- **Restarts:** buttons on messages sent before a restart stop working on a real server, and a
  run never restarts the bot. The live-post fingerprints that survive restarts are covered by
  `tests/test_live_posts.py` instead.
- **Discord's own behaviour and display:** how messages look, client glitches (like a brief
  "interaction failed" flash), command syncing, permission overrides set in server settings,
  and whether the bot's role can read message history.
- **Things members type that aren't commands.** Chat is modelled only as messages that push
  posts up the channel.
- **Load on the real host:** commands are fast here, and the Pi is slower.
- **The CLI** (`cli.py`), which has its own commands.
- **Formats other than single elimination**, since that's all the bot supports so far.

It also only tests what actors actually do. Run `python -m sim --coverage` to see which
commands ran at which stage. A command that never runs is never checked:
`/admin setstatus`, `/admin deletetournament`, `/admin clearpredictions` and `/help admin`
aren't used by any actor yet.

A manual pass on a test server is still worth doing before a release, for everything in this
list.

## Reading the output

```
  98 Ace: /predict tournament:Spring Cup
       -> [private] Round 1 — Match 1 of 4 / Who do you predict will win this matchup? / …
  99 Ace: clicks [Jinx]
       -> [private] Round 1 — Match 2 of 4 / …
       (Haze walks away from their bracket)
 112 Organizer: clicks [Ace]
       -> [private] ✅ Result recorded. **Ace** advances to Round 2 to face TBD.
       -> [public] 📣 Round 1: **Ace** defeats **Blaze**. …
       !! match 15 was recorded again without /admin setresult's confirmation
```

- A numbered line is a step: who did what.
- `->` lines are what the bot posted or edited, marked public or private.
- Lines in brackets narrate something that isn't a Discord action.
- `!!` lines are problems. They also appear in the summary after the run, with the step that
  caused them.
- `(bot)` steps are things the bot does on its own: the sign-up deadline passing, and refreshes
  of live posts.

## Extending it

- **A new competence profile or organizer style:** add a `Competence(...)` or
  `OrganizerStyle(...)` line in `actors.py`.
- **A new scenario:** add a `Scenario(...)` entry to `SCENARIOS` in `scenarios.py`. `pytest`
  picks it up automatically.
- **A new command:** give an actor a reason to use it (usually in `Player._look_around`,
  `_off_script` or the organizer's stages), then check `--coverage`.
- **A new check:** checks of a message or of database state go in `checks.py`. Checks that
  need to know who did what go in `harness.py`, or in the scenario if they compare against the
  real outcomes.
- **A new tournament format:** add a scenario with that `format`. It will need its own way of
  recording results and its own final checks.

**Prove a new check works.** A check that never fires proves nothing. Plant the bug it's meant
to catch, run the scenario, confirm the check flags it, then remove the bug. If a bug only shows
up under overlap, check that it's caught with `simultaneous` on, and that it really is missed
with it off.

## Using the harness in tests

`tests/test_stale_menus.py` and `tests/test_live_posts.py` drive the real cogs directly with the
harness, for exact situations a random scenario would rarely hit:

```python
sim = await Simulation.create(tmp_path / "test.db")
organizer = sim.add_user(1, "Organizer", organizer=True)
menu = (await sim.run(organizer, "match result", tournament="Cup")).sent
await sim.choose(organizer, menu, menu.view.children[0].options[0].value)
reply = await sim.click(organizer, menu, "Ace")
...
await sim.close()   # unloads the cogs and puts the db module back
```

`sim.together([...], random.Random(seed))` runs several flows at once. Set
`sim.simultaneous = True` to interleave inside the bot's code too, and loop over a range of
seeds to try many orderings.
