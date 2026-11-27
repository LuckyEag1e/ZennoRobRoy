# Fuku (Matikane Fukukitaru)

Python Discord bot. It's only a skeleton for now: one `/ping` command, the tests, and the scripts
that start and stop it.

## Setup

1. Create a Discord application and bot at https://discord.com/developers/applications, then
   invite it with the `bot` and `applications.commands` scopes.
2. Copy `.env.example` to `.env` and put the bot token in it. Set `DEV_GUILD_ID` to a test
   server's ID so command changes show up there straight away. Without it, commands are
   synced globally, which can take up to an hour.
3. The host needs Python 3.10+ with `venv` (on Debian/Ubuntu: `sudo apt install python3-venv`).

## Running in production

```
./start.sh   # run the tests, then start the bot in the background if they all pass
./stop.sh    # stop it
```

`start.sh` creates `.venv` on first run and reinstalls dependencies whenever a requirements
file changes. If any test fails, it refuses to start the bot. Output goes to `bot.log`. The bot
keeps running after you close the terminal but not after a reboot. To deploy new code, run
`git pull`, then `./stop.sh && ./start.sh`.

## Tests come first

Every change starts with a test that fails:

1. **Red.** Write a test in `tests/` for the behaviour you want, run it, and check that it
   fails for the reason you expect.
2. **Green.** Write the least code that makes it pass.
3. **Refactor**, keeping the suite green.

```
.venv/bin/python -m pytest          # everything
.venv/bin/python -m pytest -k ping  # one area
```

The tests don't need a token or a connection to Discord. `tests/fakes.py` stands in for the
parts of Discord a cog uses, and `invoke()` runs a slash command the way discord.py would.
`tests/test_ping.py` shows the pattern. The fakes copy the rules Discord enforces, such as an
interaction getting only one response, so a test fails where Discord would reject the reply.
When a command needs more of Discord, add it to the fakes rather than patching around it in
one test.

To add a command, write its tests, then add a module in `cogs/` and list it in `INITIAL_COGS`
in `bot.py`. `tests/test_bot.py` loads every cog in that list.
