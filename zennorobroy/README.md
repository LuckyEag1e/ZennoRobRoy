# ZennoRobRoy

Discord bot that reports Umamusume club (circle) stats from the Chrono Genesis API.

- `/club [circle_id]` — on-demand lookup, posts a summary embed + fan count chart.
- Daily auto-post — once a day, posts the same summary for your configured circle to a configured channel.

## Setup

1. **Create a Discord application**
   - Go to https://discord.com/developers/applications → New Application.
   - Under **Bot**, click "Reset Token" to get a bot token, and enable nothing extra (no privileged intents needed).
   - Under **OAuth2 → URL Generator**, check scopes `bot` and `applications.commands`, and under bot permissions check `Send Messages` and `Embed Links`. Open the generated URL to invite the bot to your server.
   - Note your **Application ID** (Client ID) from the General Information page.

2. **Get the channel ID**
   - In Discord, enable Developer Mode (User Settings → Advanced), then right-click the target channel → Copy Channel ID.

3. **Fill in `.env`**
   Copy `.env.example` to `.env` and fill it in. At minimum:
   ```
   CHRONO_API_KEY=your-chrono-genesis-key
   CIRCLE_ID=your-circle-id
   DISCORD_TOKEN=your-bot-token
   CLIENT_ID=your-application-id
   GUILD_ID=your-server-id      # optional, makes slash commands register instantly for dev (comma-separated for multiple servers)
   CHANNEL_ID=your-channel-id
   ```

4. **Install & register commands**
   ```
   npm install
   npm run deploy-commands
   ```

5. **Run it**
   ```
   npm start
   ```

## Notes

- The daily post time is controlled by `POST_HOUR_UTC` in `.env` (default 9 = 9:00 UTC).
- To track a different circle by default, change `CIRCLE_ID` in `.env`. The `/club` command also accepts a `circle_id` option to look up any circle on demand.
- Chart images are rendered via QuickChart.io (a public chart-rendering service) — the fan count history for the circle is sent there as chart data to generate the image URL embedded in Discord.
