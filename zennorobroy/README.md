# ZennoRobRoy

Discord bot for an Umamusume club. It pulls club and member stats from the Chrono Genesis
API, draws them as image cards, and posts them on demand or once a day. It can also post
announcements from umamusume.com and make parent adoption cards from uma.moe profiles.

Up to two clubs (circles) can share one bot, both in the same Discord server.

## Commands

### Club stats

| Command | What it does |
| --- | --- |
| `/stats [club]` | Leaderboard of every member: average daily gain, period gain, latest day's gain and days tracked. The main command. |
| `/stats_simple [club]` | The same leaderboard with only Gain and Avg. |
| `/club` | Club summary card: rank, fans, daily gain, members and a fan-count trend chart. |
| `/profile [user] [name]` | One member's stats and fan-count trend. Looks the member up by trainer `name`, or by a linked Discord `user` (yourself if both are left out). |

`club` picks which club to show, and defaults to the first one. `/club` and `/profile` only
cover the first club.

On the leaderboards, the bottom two members are red and the next two up are yellow. A solid red
line marks where members fall under the daily quota, and a dashed yellow line where they fall
under the goal.

### Quota and goal

| Command | Who can use it | What it does |
| --- | --- | --- |
| `/quota [amount] [club]` | Anyone to view, admins to set | The required daily fan gain per member. `0` turns it off. |
| `/set_goal [amount] [club]` | Anyone to view, admins to set | A stretch goal, shown separately from the quota. `0` turns it off. |
| `/approve_inactivity <user> <end_date>` | Admins | Excuses a linked member from the quota until `end_date` (`YYYY-MM-DD`). A date today or earlier removes the approval. |

"Admins" means the server owner and anyone they've given access with `/authorise`. Approved
inactivity only changes the quota check: the member's shown averages and leaderboard position
stay the same. The bot gives them an **Approved Inactivity** role. At 00:05 UTC on the end date,
it removes the approval and the role.

### Linking Discord accounts to trainers

| Command | Who can use it | What it does |
| --- | --- | --- |
| `/link_trainer <name>` | Anyone | Links your Discord account to your trainer, by exact in-game name. Needed for `/profile` by user and for `/approve_inactivity`. |
| `/check_links` | Admins | Lists every link in the server. |
| `/unlink_trainer <user>` | Admins | Removes a member's link so it can be redone. |
| `/authorise <user>` | Server owner | Lets a member use the admin commands above. |

Linking runs on the honor system: nothing checks that a trainer is really yours, and two
accounts can link the same trainer. Admins can review links with `/check_links` and fix them
with `/unlink_trainer`.

### Parent adoption

| Command | What it does |
| --- | --- |
| `/share_parent <uid>` | Looks up a trainer on uma.moe by account ID and draws a parent card from their inheritance, with the character's portrait. Needs `UMA_MOE_API_KEY`. |
| `/copypasta ...` | Fills in the club's parent adoption text from the sparks you type in. |

### Automatic posts

- **Daily summary**: at `POST_HOUR_UTC` every day, a `/stats` card for each club goes to
  `CHANNEL_ID`.
- **News**: if `NEWS_CHANNEL_ID` is set, new umamusume.com announcements are posted there,
  checked every 10 minutes. Announcements that are edited later, such as gift distributions, are
  posted again marked "[Updated]". The first run only records what's already there, so the
  channel isn't flooded.
- **Shutdown warning**: if `ADMIN_USER_ID` is set, the bot DMs that user when it's stopped or
  crashes. It can't send anything if it's killed outright (`kill -9`, out of memory, or the
  machine losing power).

## Setup

1. **Create a Discord application**
   - Go to https://discord.com/developers/applications and click **New Application**.
   - Under **Bot**, click **Reset Token** to get the bot token. No privileged intents are needed.
   - Note the **Application ID** on the **General Information** page.
   - Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes, and
     these bot permissions: **Send Messages**, **Embed Links**, **Attach Files** and
     **Manage Roles**. Open the generated URL to invite the bot.
   - In **Server Settings → Roles**, keep the bot's role above **Approved Inactivity**, or it
     can't hand that role out. The bot creates the role the first time it's needed.

2. **Get the IDs**: in Discord, turn on **Developer Mode** (User Settings → Advanced), then
   right-click a server or channel and choose **Copy ID**.

3. **Fill in `.env`**: copy `.env.example` to `.env`, fill it in, and make it readable only by
   you (`chmod 600 .env`), since it holds the bot token and API keys.

   | Setting | Required | What it is |
   | --- | --- | --- |
   | `DISCORD_TOKEN` | Yes | The bot token. |
   | `CLIENT_ID` | Yes | The application ID. |
   | `CHRONO_API_KEY` | Yes | Chrono Genesis API key. Works for both clubs. |
   | `CIRCLE_ID`, `CLUB_NAME`, `CLUB_KEY` | Yes | The first club's circle ID, display name, and the key its quota and goal are saved under. |
   | `CIRCLE_ID_2`, `CLUB_NAME_2`, `CLUB_KEY_2` | No | The same for a second club. |
   | `CHANNEL_ID` | Yes | Channel for the daily summary. |
   | `POST_HOUR_UTC` | No | Hour of the daily summary, in UTC (default 9). |
   | `GUILD_ID` | No | Server IDs, comma-separated, to register commands in instantly. Left blank, commands register globally, which can take up to an hour. |
   | `QUOTA_PER_DAY` | No | Quota for any club that hasn't had one set with `/quota`. |
   | `NEWS_CHANNEL_ID` | No | Channel for umamusume.com news. Blank turns it off. |
   | `UMA_MOE_API_KEY` | No | uma.moe API key, for `/share_parent`. |
   | `ADMIN_USER_ID` | No | Discord user to DM on shutdown or a crash. |
   | `MEMBERS_COLOR_*`, `CLUB_2_COLOR_*` | No | Card colors for each club, as quoted hex (`"#f1d7d6"`). |

   Once a club's circle ID is set, the bot won't start without its name and key. Never change a
   key that's already in use, or that club's saved quota and goal stop being found. Color values
   need the quotes, because an unquoted `#` starts a comment in `.env`.

4. **Install and register the commands**
   ```
   npm install
   npm run deploy-commands
   ```
   Run `npm run deploy-commands` again whenever a command changes, or after changing club names
   or keys.

5. **Run it**
   ```
   npm start
   ```
   There's no hot reload, so restart after changing code or `.env`. To keep it running after
   you close the terminal, use a service manager, or
   `nohup npm start >> bot.log 2>&1 & disown`.

## Data

Everything the bot saves is in `data/`, which git doesn't track. Back it up.

- `config.json`: quotas, goals, admins, trainer links and approved inactivity, per server.
- `history/`: a line per fetch for each club and each member. The trend charts are drawn from
  this, so they reach further back than the API's own history, and grow every time a command
  fetches fresh data.
- `raw/`: every API response, in full.
- `news.json`: which announcements have been posted.

## Upgrading from the version with club names in `src/clubs.js`

Club names and keys used to be hardcoded. They now come from `.env`, and the bot moved from
the repo root into `zennorobroy/`. On the machine that runs it:

1. Stop the bot. `git pull` moves tracked files but leaves untracked ones at the repo
   root. Move `.env`, `data/` and `assets/` into `zennorobroy/`, and either move
   `node_modules/` too or run `npm install` there. `git status --ignored` lists anything
   else left behind. The bot refuses to start while `data/` or `assets/` is still at the
   root.
2. Add `CLUB_KEY`/`CLUB_NAME`, and `CLUB_KEY_2`/`CLUB_NAME_2` for a second club, to
   `zennorobroy/.env`. Each key must be the one already under your guild ID in the
   `clubQuota` and `clubGoal` sections of `data/config.json`, or that club's quota and goal
   are lost. Use the names the bot showed before.
3. Rename the second club's color variables to `CLUB_2_COLOR_*`. The bot warns at startup
   about any `*_COLOR_*` variable it doesn't read.
4. Update whatever starts the bot so it runs from `zennorobroy/`, then run
   `npm run deploy-commands` (so the `club` option offers the new keys) and `npm start`.

## Notes

- All images are drawn locally with `@napi-rs/canvas`. No stats are sent to a chart service.
- The Chrono Genesis API isn't documented. `CLAUDE.md` has what's been worked out about it.
