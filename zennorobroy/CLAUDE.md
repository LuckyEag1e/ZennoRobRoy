# Uma Club Bot — Session Notes

Discord bot for an Umamusume Pretty Derby club, pulling data from the (reverse-engineered,
undocumented) Chrono Genesis API and reporting club/member performance into a Discord server.

## Current state

- Fully working, running locally (not containerized/systemd'd — just `npm start` in a terminal).
- Tested end-to-end against the live API and a real test Discord server ("Eagle archives",
  guild ID `1048404951212249258`, channel `#bot` = `1435304436636450869`).
- Bot identity: renamed at least twice since — was **Fine Motion#6572** (application/client ID
  `1535455956463259668`), as of 2026-08-24 logs in as **Zenno Rob Roy#6572** (same client ID, so
  this is a Username change on the **Bot** tab of the Developer Portal, same mechanism as the
  earlier rename, not a new application). If it logs in under yet another name later, that's
  expected — the rename happens directly in the Discord Developer Portal, outside this repo, so
  these notes will lag until someone notices and updates them.
- No git repo initialized in this directory yet.
- **Confirmed production target**: `1426009141100548300` ("TOKAI TEIO - Umamusume Club") is the
  real club server, not another dev/test guild — the user has been given permission to roll the
  bot out there for real. `GUILD_ID` already includes it (see "Key implementation decisions" for
  the full list), so slash commands are live there.
- Not running right now unless a previous session's background `npm start` is still alive —
  check before assuming it's live, and restart it after any code change (no hot reload).
- As of 2026-09-27, when a session starts it in the background it uses `nohup ... & disown` so it
  survives the launching terminal/session closing — the earlier unexplained downtime (bot found
  not running with a clean log, no crash) was most likely a bare `npm start` dying to a `SIGHUP`
  when its terminal went away.
- **Shutdown/crash DM notifier** (added 2026-09-27, explicit user request — "is there any way for
  it to message me before it dies?"): `src/index.js` now DMs `ADMIN_USER_ID` (the user's own
  Discord ID) on `SIGINT`, `SIGTERM`, `uncaughtException`, and `unhandledRejection`, then exits.
  Verified working end-to-end (manual `SIGTERM` test). Optional — blank/unset `ADMIN_USER_ID`
  just skips the DM. **Known and inherent limitation, explained to the user up front**: this only
  covers deaths the process gets a chance to see coming. `kill -9`, an OOM kill, or the host
  crashing/losing power give zero warning — no code change can fix that class of failure.
- **Daily auto-post is re-enabled** (as of 2026-08-24, explicit user request to reinstate it):
  `scheduleDailyPost();` is called from the `ready` handler in `src/index.js` again. `CHANNEL_ID`
  now points at `1536373450103455834`, a channel in the production TOKAI TEIO guild (no longer the
  Eagle archives test channel). `POST_HOUR_UTC=14` (14:00 UTC = 16:00 in the user's local
  timezone, UTC+2 at time of setting — re-derive the offset rather than assuming it's still UTC+2
  if this ever needs revisiting, since it wasn't given as a named timezone). All slash commands
  (`/club`, `/members`, `/members_detailed`, etc.) are unaffected either way and still work on
  demand.

- **umamusume.com news feed** (added 2026-10-05, user's pick over X/Twitter — X API is paid and
  scraping is fragile; the Steam news feed was checked and lacks gift/banner posts):
  `src/newsFeed.js` polls every 10 min the undocumented JSON endpoint the site's SvelteKit news page
  calls (`POST https://umamusume.com/api/ajax/pr_info_index?format=json`, body
  `{announce_label:0, limit, offset}`), posts new items as embeds to `NEWS_CHANNEL_ID`
  (`#umamusume-news` in TOKAI TEIO), and reposts as "[Updated]" when `update_at` changes (gift
  distributions are often announced as edits to an existing notice). State in `data/news.json`;
  first run with no state file seeds silently. Unset `NEWS_CHANNEL_ID` disables it.

- **Second club: Hachimiers** (added 2026-10-12, after the clubs merged). Circle `609694326`,
  set as `CIRCLE_ID_2` in `.env`; the same `CHRONO_API_KEY` works for both circles. Both clubs are
  in the TOKAI TEIO Discord server. `/members` → **`/stats_simple`** and `/members_detailed` →
  **`/stats`** (renamed on the user's request). Both take an optional `club` choice option
  (TOKAI TEIO, the default, or Hachimiers). The club registry is `src/clubs.js`. Each club has
  one palette (`theme` in `clubs.js`) used by `/stats`, `/stats_simple` and the daily post (which posts one `/stats` card per club, TOKAI TEIO first, to `CHANNEL_ID`):
  as of 2026-10-13 both are dark pastel themes set in `.env` (user request: "keep the concept of a
  peach and an orange, but more pastel and dark themed"): TOKAI TEIO = dark pastel red (peach → pink → red on 2026-10-13, user asked for "more of a red"; warning-row red bumped to a more saturated `#ff4d4d` so it still stands out from the red border) via
  `MEMBERS_COLOR_*`, Hachimiers = dark orange via `HACHIMIERS_COLOR_*` (its "yellow" role is a
  pastel blue so warning rows stand out from the orange). The light pink defaults in
  `memberCardShared.js` are now only fallbacks. `/stats` is
  the user's default/main leaderboard command from 2026-10-13 on; the dark theme now only
  applies to `/profile`. **Per-club quota/goal** (same day): `/quota` and
  `/set_goal` take the same `club` option. Values now live in `data/config.json` under
  `clubQuota[guildId][clubKey]` / `clubGoal[guildId][clubKey]`; the old per-guild `quota`/`goal`
  keys were migrated to `tokai_teio` and removed (backup: `data/config.json.bak-2026-10-12`).
  `/link_trainer` and `/check_links` search every club's roster (`fetchAllClubs` in `clubs.js`), so
  Hachimiers members can link, and `/approve_inactivity` (keyed by friend_viewer_id) then works for
  them too. **Still single-club**: `/club` and `/profile` use `CIRCLE_ID` only. The command descriptions below still use the old `/members`
  names and the old per-guild quota wording.

## Environment

- Host: Linux box, user `lzb`, LAN IP `192.168.2.85`, sshd active on port 22. The user connects
  via Windows Remote Desktop and transferred files here over `scp`/WinSCP.
- Test circle used throughout development: `507766504` ("TOKAI TEIO"), 30 members.

## Architecture

```
src/
  index.js              Bot entry point: logs in, loads commands. Daily-post cron scheduling
                        (scheduleDailyPost/postDailySummary) is defined but currently NOT called
                        from the ready handler — disabled for now, see "Current state"
  deploy-commands.js     Registers slash commands with Discord (run after adding/changing a command)
  config.js              Persistent per-guild config (quota override, authorised users, trainer
                          links, approved-inactivity credit) -> data/config.json
  archive.js              Archives every API fetch: full raw JSON + a lightweight per-circle
                          history log used to build trend charts locally over time, plus (as of
                          2026-09-12) a per-member history log powering /profile's trend chart —
                          see "Per-member history tracking" below
  memberStats.js          Turns raw club_friend_profile/club_friend_history into a sorted,
                          per-member leaderboard (avg/gain/latest-gain/days)
  api/
    chronoApi.js          Chrono Genesis API client (single endpoint: club_profile)
  discord/
    embed.js               Thin embed wrapper around the /club report image
    reportImage.js         Renders the /club report card PNG locally (@napi-rs/canvas)
    memberCardShared.js     Shared canvas helpers (colors, section boxes, header, stats box,
                            threshold-line divider) used by both member-leaderboard renderers
                            below, extracted to avoid duplicating the near-identical chrome
                            between the two card styles
    memberImage.js          Renders the /members leaderboard PNG (compact: Gain/Avg only)
    memberDetailedImage.js   Renders the /members_detailed leaderboard PNG (full: Avg/Gain/+latest/Days)
    profileImage.js          Renders the /profile card PNG (header + stats + fan-count trend chart),
                            reuses memberCardShared's palette/section() but has its own small
                            drawTrendChart (a deliberate copy of reportImage.js's version, just
                            recolored to match memberCardShared's purple instead of /club's indigo)
  commands/
    club.js                /club              — club summary card + fan-count trend chart
    members.js              /members            — per-member leaderboard (compact, 2 data columns)
    members_detailed.js      /members_detailed    — per-member leaderboard (full, 4 data columns)
    profile.js                /profile [user] [name] — one member's stats + fan-count trend over time
    parent.js                /parent ...          — fills in the "parent adoption" copypasta template
    quota.js                 /quota [amount]       — view/set the daily quota (0 = disabled)
    set_goal.js               /set_goal [amount]    — view/set the daily aspirational goal (0 = disabled)
    authorise.js              /authorise <user>     — server-owner-only, grants quota-change permission
    link_trainer.js            /link_trainer <name>  — self-service link: Discord account -> friend_viewer_id
    approve_inactivity.js       /approve_inactivity <user> <days> — excuses N days of quota (needs a link first)
    check_links.js              /check_links          — authorised-only: lists every Discord<->trainer link
    unlink_trainer.js            /unlink_trainer <user> — authorised-only: removes a member's link
data/                        gitignored: raw/<circle_id>/<timestamp>.json, history/<circle_id>.jsonl,
                              history/members/<circle_id>/<friend_viewer_id>.jsonl, config.json
reference/                   gitignored-by-convention (not actually in .gitignore, but user-supplied,
                              don't treat as source): reference.png and members.png the user sent via
                              scp as visual targets for the /members leaderboard design
```

## Slash commands

- **`/club`** — rank, rank change, fan count, daily gain, members, monthly avg, plus
  a fan-count trend line chart. Chart is built from the local archive (`data/history`), seeded on
  first run from the API's own ~7-day `club_daily_history` so it isn't empty immediately.
- **`/members`** — simplified from an earlier 4-column version. A stats header (Fans
  +period gain, Buffer, Quota, Expected Gain, Goal, Goal Gain) sits above the table; the table
  itself now shows only **Gain/Avg** per member (the `+latest`/Days columns were dropped from the
  render — `memberStats.js` still computes `latestGain` and `days` per member, but only `days`
  survives as a `>0` guard for showing "–" on gap rows). Sorted by average daily gain descending.
  Color coding is purely **rank-based**, not quota-based: bottom 2 rows red, next 2 up yellow
  (explicit user decision — "our club is kinda loose"). Two independent divider lines can appear
  between rows, drawn by `drawThresholdLine` in `memberCardShared.js`: a bold solid **red** line at
  the quota cutoff (the hard requirement), and a subtle dashed **yellow** line at the separate
  `/set_goal` cutoff (an aspirational stretch target) — see the `/set_goal` entry below. Each only
  draws when its own threshold is enabled *and* at least one member is under it; if both cutoffs
  land on the same row, the goal line is drawn first so the red quota line wins visually.
- **`/members_detailed`** — the full 4-column breakdown that `/members` used to show,
  for whenever the compact view isn't enough: **Avg/Gain/+/-/Days**, same sort order, same header
  and stats box, same rank-based color coding and same pair of quota/goal divider lines as
  `/members` (both renderers share `memberCardShared.js` for everything except the table itself).
  The **Days** column renders as
  `9(-3)` when the member has approved-inactivity credit applied — first number is real tracked
  days, the `(-N)` is the credited days from `/approve_inactivity`; with no credit it's just the
  plain day count. Wider canvas (820px vs. 700px) to fit the extra two columns.
- **`/profile [user] [name]`** — one member's card: name, fan count, daily/monthly gain and
  averages (straight from `club_friend_profile`, no `memberStats.js` involved — this isn't a
  leaderboard row, it's the API's own per-member profile), plus a fan-count trend chart built from
  the new per-member history log (see "Per-member history tracking" below). Resolution order: if
  `name` is given, resolves it against the current roster exactly like `/link_trainer` does (fails
  closed on no match or ambiguous match); otherwise resolves `user` (or the caller themself if
  `user` is omitted) via their `/link_trainer` link, erroring out by name if there isn't one yet.
  Added 2026-09-12, explicit user request ("a profile lookup would be awesome!") after being asked
  what kind of over-time tracking they wanted, on top of the club-level trend `/club` already had.
- **`/parent`** — fills in a club "parent adoption" copypasta template with 9 required args
  (`name`, `rating`, `rank`, `blue_spark`, `pink_spark`, `total_blue`, `total_pink`,
  `white_spark_1`, `white_spark_2`). User-supplied text is run through discord.js's
  `escapeMarkdown` to stop stray `*` characters from italicizing text across fields. Output is a
  single continuous paragraph (no newlines), per explicit request.
- **`/quota [amount]`** — no argument = view current quota (ephemeral). With an amount, sets it;
  `0` disables quota tracking entirely (memberImage.js then shows "Disabled", blanks Buffer/Expected
  Gain, and skips the divider line). Gated to the server owner or anyone `/authorise`'d.
- **`/set_goal [amount]`** — same shape as `/quota` (view with no arg, set with an amount, `0`
  disables), same authorisation gate (owner or `/authorise`'d), and its own key in
  `data/config.json` (`goal[guildId]`, via `getGoal`/`setGoal` in `config.js`) — entirely separate
  from `quota[guildId]`, so the two can be set independently and one being 0/disabled doesn't
  affect the other. Unlike `/quota`, there's no `.env` fallback default (`GOAL_PER_DAY` doesn't
  exist) — an unset goal is just off until someone runs this command. Explicit user framing: quota
  is the hard requirement, goal is an aspirational stretch target, and the two must look visually
  distinct — see the divider-line description under `/members` above.
- **`/authorise <user>`** — only the actual guild owner (`interaction.guild.ownerId`) can run this,
  checked in code (not just Discord's permission UI, which is only used as a secondary
  "Administrator" gate to hide the command from regular members). Adds the target user to a
  per-guild allowlist in `data/config.json` for use by `/quota`.
- **`/link_trainer <name>`** — self-service: each member links their own Discord account to their
  in-game trainer by exact name (case-insensitive), resolved against the *current* roster
  (`club.circle_user_array`) in a fresh API fetch. Required because nothing else in the bot maps a
  Discord user to a `friend_viewer_id` — `/members` is keyed entirely off the game API. Stored in
  `data/config.json` under `trainerLinks[guildId][discordUserId] = friendViewerId`. Fails closed on
  no match or on a duplicate name (ambiguous). Runs on the **honor system**: there is no ownership
  proof and no uniqueness check — a security review briefly added a first-claim-wins guard here,
  but the user explicitly asked to revert it ("scrap the link trainer fix, we will run on an honor
  system") in favor of `/check_links`/`/unlink_trainer` below for after-the-fact admin oversight
  instead of blocking at claim time.
- **`/check_links`** — authorised-only (owner or `/authorise`'d, same gate as `/quota` and
  `/approve_inactivity`). Lists every `<@discordUser> → trainerName` link in the guild, resolving
  names via a fresh API fetch (`resolveTrainerName` in `memberStats.js`, shared with
  `/unlink_trainer`); shows `(not in current roster, id ...)` for links to someone who's since left
  the club. Ephemeral reply. This is the oversight mechanism for the honor-system linking above —
  admins can audit who claimed what and catch bad-faith or mistaken claims after the fact.
- **`/unlink_trainer <user>`** — authorised-only, same gate. Removes a member's link outright so
  they (or someone else) can `/link_trainer` again; replies with an error if the target has no
  link. No confirmation step — a single authorised call is destructive immediately, consistent with
  how `/quota` and `/approve_inactivity` also act on the first call with no undo prompt.
- **`/approve_inactivity <user> <days>`** — excuses a linked member from `days` of quota; gated the
  same way as `/quota` (owner or `/authorise`'d). Requires the target to have already run
  `/link_trainer` — replies with an error naming that command if no link exists. Negative `days`
  partially or fully undoes a previous grant; credit is cumulative per trainer and clamped at 0
  (never goes negative — you can't use this to *raise* someone's requirement above baseline).
  Persists indefinitely in `data/config.json` (`inactivityCredit[guildId][friendViewerId]`) — there
  is no auto-reset on month rollover, so reversing a grant once the excused period ends is a manual
  step, by explicit design.
  - **Effect is scoped to the quota check only**: in both `memberImage.js` and
    `memberDetailedImage.js` (each has its own local copy of the `effectiveAvg` helper — small
    enough not to be worth pulling into `memberCardShared.js`), credited days shrink the
    denominator used to test a member against quota (`gain / max(1, days − credit)` vs.
    `quotaPerDay`) and shrink that member's share of the header's Buffer/Expected Gain math. The
    member's *displayed* Avg/Gain (and, in `/members_detailed`, the raw Days count before the
    `(-N)` suffix) and their position in the sorted leaderboard are computed from real `days` and
    are untouched by credit — this was an explicit choice so approved inactivity doesn't let
    someone appear to out-perform people who actually did.
  - Known imprecision (inherited from the pre-existing Buffer formula, itself flagged as an
    unverified guess): the header's total credit is capped per-member at the current global `day`
    count rather than each member's own tracked days, matching the existing simplification that
    Buffer already assumes every member was tracked the full period.

## Key implementation decisions (the "why", not obvious from code alone)

- **Local-only rendering**: originally used QuickChart.io (a public chart-rendering API) for the
  `/club` trend chart, but the user asked to keep everything local — switched to `@napi-rs/canvas`
  (prebuilt binaries, no native build/apt deps needed) drawing both report cards fully by hand.
  QuickChart's dual-axis charts were also found to be broken on their public server during
  testing (crashes regardless of Chart.js v2/v3 syntax) — another reason to drop it.
- **Local archive**: every fetch (via `/club`, `/members`, `/members_detailed`, `/profile`, and the
  daily cron) writes the full raw API response to `data/raw/<circle_id>/<timestamp>.json` and
  appends a summary line to `data/history/<circle_id>.jsonl`. This is a deliberate, explicit user
  request ("archive the data") — the local history log grows richer over time independent of the
  API's own rolling window, and is what powers the `/club` trend chart.
- **Per-member history tracking** (added 2026-09-12): the same `archiveSnapshot()` call also runs
  `archiveMemberSnapshots()`, appending one line per current roster member to
  `data/history/members/<circle_id>/<friend_viewer_id>.jsonl` (`{timestamp, name, fan_count,
  daily_average, monthly_average, daily_diff, monthly_diff}` — straight off `club_friend_profile`).
  This piggybacks on every existing `archiveSnapshot()` call site rather than needing its own —
  any command that fetches `club_profile` grows every member's history a little, not just
  `/profile`. On first sight of a member (no file yet), it backfills using that member's own
  `club_friend_history` rows the same way `seedFromDailyHistory` backfills the club-level log, so
  `/profile`'s chart isn't a single dot the first time someone runs it. Unlike the club-level log,
  this survives period resets by design — `club_friend_history` only covers the current period,
  this file accumulates indefinitely across periods. `friend_viewer_id` gets the same
  `assertValidId()` path-safety check as `circle_id` before touching the filesystem, since it also
  flows into a file path (though in practice it's always API-derived, never raw user input).
- **rank_diff / rank_gain sign convention**: `= old_rank − new_rank`. Positive means the rank
  *number* went down, i.e. the club improved. This was reverse-engineered by inspecting
  `club_daily_history` — not documented anywhere by Chrono Genesis.
- **Member stats formulas** (from `club_friend_history`, per member):
  `Avg = cumulative_gain / days_tracked`, `Gain = latest adjusted_fan_gain_cumulative`,
  `+/- = latest day's adjusted_interpolated_fan_gain`, `Days = count of that member's history
  entries this period` (varies for members who joined mid-period).
- **Buffer formula is an unverified best guess**: `club total period gain − (quota × member_num ×
  day)`. The one reference screenshot the user provided showed Buffer as blank/"–", so this
  couldn't be checked against a known-good example. Flag this to the user once someone in their
  club actually falls behind quota and the number can be sanity-checked.
- **Badges skipped**: the reference screenshot has `#1`/`#2`/`#3` superscript badges and a crown
  icon on certain trainers. The user explicitly said to skip these for the first version
  ("Skip badges for now" / "leave them out ... add badges later once I explain the exact rule").
  Not implemented — ask the user for the exact rule before attempting it.
- **Leaderboard color palettes**: `memberCardShared.js` exports `DARK_THEME` (the default, used by
  `/members_detailed` and `/profile`) and `LIGHT_THEME` (used by `/members` only). The shared
  helpers (`section`, `drawHeader`, `drawStatsBox`, `rankChangeText`) take an optional theme.
  - Dark: blue/purple per explicit request (`#12111f` bg, `#1e1b34` section, `#7c6ff2` border),
    with the quota-warning yellow kept as literal yellow (`#f2c94c`), also by request.
  - Light (2026-10-06, explicit user request for `/members`): `#f1d7d6` background, `#950001`
    border and subtext, black text. Red/yellow/green were darkened (`#e0002a`, `#b7791f`,
    `#1e7b34`) so they stay readable on the pale pink.
  - Every light-theme color can be overridden in `.env` (`MEMBERS_COLOR_BG`, `_SECTION`,
    `_BORDER`, `_TEXT`, `_SUBTEXT`, `_RED`, `_YELLOW`, `_GREEN`). The values above are the
    fallbacks, and an invalid value logs a warning and uses its fallback. The values **must be
    quoted** (`"#f1d7d6"`): dotenv treats an unquoted value that starts with `#` as a comment and
    reads it as empty. Read once at startup, so a restart is needed after editing.
- **Quota config lives in `data/config.json`, not `.env`**: `.env`'s `QUOTA_PER_DAY` is only a
  fallback default (`src/config.js`'s `getQuota`). Runtime changes via `/quota` are per-guild and
  persist across restarts without touching `.env` or requiring a redeploy.
- **`GUILD_ID` accepts a comma-separated list**: `deploy-commands.js` splits and registers commands
  to every listed guild individually (instant per-guild registration, rather than the up-to-1-hour
  global sync). Currently three guilds: `1048404951212249258` (Eagle archives, the original test
  server), `1536320938700705872` ("Bot test"), and `1426009141100548300` ("TOKAI TEIO - Umamusume
  Club" — the club's actual server, matching the club name used as the test circle throughout this
  project; likely the real production target, not just another dev server). No other code reads
  `GUILD_ID` — everything else in the bot (quota, authorise, trainer links, inactivity credit) is
  already scoped per-guild via `interaction.guildId` in `data/config.json`, so adding a guild here
  only affects command registration speed, not runtime behavior.

## Security review (2026-08-10)

A review ahead of the production rollout found two issues. One was fixed and kept; the other was
fixed and then explicitly reverted by the user in favor of an honor-system + admin-oversight
approach. One item remains open.

- **Fixed and kept — path traversal via `circle_id`**: `archive.js` built filesystem paths with
  `path.join(DATA_DIR, ..., circleId)`. `path.join` does **not** strip `../`, so an unvalidated
  `circle_id` was a real arbitrary file read/write primitive (confirmed with a proof-of-concept —
  `../../../../tmp/...` resolved clean outside `data/`), reachable by any guild member via the
  `circle_id` option on `/club`, `/members`, `/members_detailed` (not gated behind `/authorise`).
  Fixed two ways: (1) `archive.js` now has `assertValidCircleId()`, called at the top of both
  `archiveSnapshot()` and `readHistory()`, rejecting anything that isn't `^[0-9]+$`; (2) the
  `circle_id` option was removed entirely from `/club`, `/members`, `/members_detailed`, and
  `/link_trainer` (explicit user call — only one club uses this bot right now, so the flexibility
  wasn't worth the exposure). All four now always use `process.env.CIRCLE_ID`. The validation in
  `archive.js` stays regardless, as defense in depth for whenever multi-circle support comes back.
- **Fixed, then reverted — `/link_trainer` identity spoofing**: any Discord user can self-link to
  *any* current club member's exact name with no ownership proof, and nothing stops two different
  Discord accounts from both claiming the same trainer. A first-claim-wins uniqueness guard
  (`findLinkedUser()` in `config.js`) was added and verified working, but the user then explicitly
  asked to scrap it — "we will run on an honor system" — so it was removed again. In its place:
  `/check_links` (authorised-only, lists every `<@user> → trainer` link so bad-faith or mistaken
  claims can be spotted) and `/unlink_trainer` (authorised-only, removes a link so it can be redone
  correctly). This is deliberate after-the-fact oversight instead of at-claim-time blocking — do
  **not** silently re-add the uniqueness check if asked to revisit this; the user considered it and
  chose the honor-system/oversight approach on purpose.
- **Open — `.env` is world-readable**: mode `664` on a non-containerized host means any other local
  account on this box could read `DISCORD_TOKEN` and `CHRONO_API_KEY`. Not fixed yet — recommend
  `chmod 600 .env` (low risk if this is genuinely single-user, but cheap to harden regardless).
- Also noted but not fixed (low severity, no longer user-reachable after the `circle_id` option
  removal above): `chronoApi.js` interpolates `circle_id` into the request URL without
  `encodeURIComponent`. Harmless now that the value is always `process.env.CIRCLE_ID`, but worth
  fixing properly if a `circle_id` option is ever reintroduced anywhere.

## Chrono Genesis API notes

- Base URL: `https://api.chronogenesis.net`, single endpoint used: `GET /club_profile?circle_id=X`.
- Requires an `Authorization` header with the raw key (no `Bearer` prefix) and a normal browser
  `User-Agent` — see `src/api/chronoApi.js`. Key lives in `.env` as `CHRONO_API_KEY`.
- Response shape: `club` (array, single current-club object), `club_daily_history` (~7 days,
  newest-first), `club_monthly_history` (past *completed* months only — current partial month is
  NOT in here, has to be derived from `club_friend_history`/`club_daily_history` instead),
  `club_friend_profile` (current + historical members — filter by `club[0].circle_user_array` to
  get just current roster), `club_friend_history` (per-member per-day rows within the current
  period), `month_filter` (list of available month buckets, informational only, unused).

## Running it

```
npm install
npm run deploy-commands   # re-run any time a command file changes
npm start                 # no hot reload — restart after any src/ change
```

Required `.env` values: `DISCORD_TOKEN`, `CLIENT_ID`, `GUILD_ID` (test server(s), comma-separated,
for instant command
registration), `CHANNEL_ID`, `CHRONO_API_KEY`, `CIRCLE_ID`, `POST_HOUR_UTC`, `QUOTA_PER_DAY`
(fallback default only — real runtime value is per-guild via `/quota`). Optional: `ADMIN_USER_ID`
(see "Shutdown/crash DM notifier" under "Current state" — blank/unset just disables the DM). Full
details in `README.md`.
