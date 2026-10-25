# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Two independent Discord bots for an Umamusume community. They share no code, runtime, or deployment target, so run every command from inside the bot's own directory, and read that directory's CLAUDE.md before working there.

- `zennorobroy/`: Node.js (discord.js). Reports club stats from the Chrono Genesis API. Its `CLAUDE.md` covers architecture, decisions and API notes. Deployment-specific details belong in the gitignored `zennorobroy/CLAUDE.local.md`.
- `winningticket/`: Python (discord.py). A bracket prediction pool, with its own `CLAUDE.md`.

## zennorobroy quick reference

```
npm install
npm run deploy-commands   # re-run after adding/changing any command file
npm start                 # no hot reload; restart after any src/ change
```

There is no test suite or linter. `scratch_*.js` files are throwaway experiments, not part of the bot.
