from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from common import requires_manage_server
from labels import CURRENT_FEATURES, PLANNED_FEATURES
from lobby_posts import DRAFTER_HOST_URL


def _command_list_embed(title: str, sections: list[tuple[str, str]]) -> discord.Embed:
    embed = discord.Embed(title=title)
    for name, value in sections:
        embed.add_field(name=name, value=value, inline=False)
    return embed


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="help", description="Show what commands are available", guild_only=True)

    @group.command(name="players", description="Commands for filling out and checking your bracket")
    async def players(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("Player commands", [
            ("/register tournament:<name>", "Sign up for a tournament that's taking entrants (`/unregister` to withdraw). The first time, you choose whether to link your entries, results and rating to your account."),
            ("/optout confirm:opt out", "Unlink your entries, results and rating from your account if you agreed before."),
            ("/registration entrants name:<name>", "See who has signed up."),
            ("/rating", "See your Elo rating from past tournaments, which decides your seed. Only you see it."),
            ("/predict tournament:<name>", "Fill out your bracket prediction, round by round."),
            ("/mypicks tournament:<name>", "See your saved picks and current score."),
            ("Ready Up", "When the bot calls your match, press **Ready Up** on its post. Once both sides are ready, it tells you which voice room to play and stream in. A side that isn't ready within 10 minutes forfeits."),
            ("/match report tournament:<name>", "Report who won your match. It's recorded once your opponent agrees, or after 10 minutes with no answer from them (they're reminded in the channel halfway through). Until then you can change your vote or take it back. When the bot called your match, its post has a Report Result button that does the same."),
            ("/match dropout tournament:<name> confirm_name:<name>", "Drop out of a tournament you're playing in. Your remaining matches go to your opponents."),
            ("/tournament bracket name:<name>", "See the real tournament bracket's progress."),
            ("/tournament leaderboard name:<name>", "See the standings."),
            ("/match stats tournament:<name>", "See what % of the pool picked each side of a matchup."),
            ("/history list", "See every concluded tournament, with its champion, size, format and date."),
            ("/history bracket tournament:<name>", "Look back at a concluded tournament. `/history seeding` and `/history leaderboard` show how it was seeded and how the pool did."),
            ("5v5 events", "Signing up for a 5v5 played on the drafter site? `/help 5v5` walks through it."),
            ("/features", "What the bot can be used for, and what's coming."),
            ("/feedback", "Send the organizers anonymous feedback."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="organizers", description="How to run a tournament, step by step")
    @requires_manage_server
    async def organizers(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("Running a tournament", [
            ("1. Create it",
             "Take sign-ups:\n"
             "`/registration open name:<name> closes_in_hours:<hours>`\n"
             "Players enter with `/register` and can see who's in with `/registration entrants`. "
             "Leave out `closes_in_hours` to close sign-ups by hand. `seeding:` picks how the "
             "bracket is drawn: `standard` (the default) seeds by rating with the top seeds playing "
             "the lowest first, `slide` has them play mid-ranked seeds first so the lowest aren't "
             "knocked out straight away, and `random` draws at random. "
             "`/registration add` signs up "
             "a member for them (by name only, unless they've agreed to link their data), or an "
             "entrant with no Discord account, and `/registration remove` withdraws anyone.\n"
             "Or build it from a bracket CSV:\n"
             "`/tournament create name:<name> csv_file:<file>`\n"
             "The CSV needs a `team_a,team_b` header and one row per round-1 matchup in "
             "bracket order (the row count must be a power of 2: 2, 4, 8, 16...). "
             "Leave one side blank to give that entrant a bye. A CSV tournament skips step 2.\n"
             "To have the bot call matches into voice rooms, first pick the category holding "
             "the rooms with `/admin matchrooms`, then add `match_slots:<n>` (up to the number "
             "of rooms) when you create the tournament, or set it later with `/admin matchslots`."),
            ("2. Close sign-ups",
             "`/registration close name:<name>`\n"
             "This happens automatically once `closes_in_hours` passes. Entrants are seeded by "
             "their Elo rating from past tournaments, and the top seeds get any byes. Players "
             "who haven't played yet count as average. Give `seeding:` here to override the "
             "choice made when sign-ups opened. The bracket is posted to the channel where "
             "sign-ups opened."),
            ("3. Open predictions",
             "`/tournament open name:<name>`\n"
             "Lets players start filling out brackets with `/predict`."),
            ("4. Lock it",
             "`/tournament lock name:<name>`\n"
             "Do this once the real tournament is about to start. It stops new or changed "
             "predictions, and from then on results are reported and recorded."),
            ("5. Record results",
             "Players report their own matches with `/match report`. When both sides agree, or "
             "one side doesn't answer within 10 minutes, the result is recorded and announced. "
             "When they disagree, the bot posts it in the channel for you to settle:\n"
             "`/match result tournament:<name>`\n"
             "Pick a ready match, then the winner. Use it too for matches against an entrant "
             "with no Discord account, who can't report. If someone can't play on, withdraw "
             "them or call the match off (`/help admin`): their opponents advance by walkover, "
             "and games that weren't played score no picks."),
            ("6. Check progress anytime",
             "`/tournament bracket name:<name>`: the live bracket\n"
             "`/tournament leaderboard name:<name>`: standings\n"
             "`/match stats tournament:<name>`: pick breakdown for a matchup"),
            ("7. Conclude it",
             "`/tournament conclude name:<name>`\n"
             "Once the final is recorded, this posts the podium and the top pick'em scorers "
             "to the channel. The tournament's results are kept and count toward players' "
             "ratings. From then on only `/history` shows it, and its name can't be used "
             "again. Only a completed tournament can be concluded."),
            ("5v5 events",
             "For 5v5 lobbies played on the drafter site instead of a bracket, see `/help 5v5`."),
            ("Fixing mistakes",
             "Wrong result, bad team name, etc.? See `/help admin`."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="admin", description="Troubleshooting/correction commands")
    @requires_manage_server
    async def admin(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("Admin / troubleshooting commands", [
            ("/admin setstatus tournament:<name> status:<status>", "Force a tournament's status directly, bypassing the normal open/lock flow."),
            ("/admin setresult tournament:<name>", "Force or overwrite any known matchup's result. A result later ones depend on has to wait until those are cleared, latest first."),
            ("/admin clearresult tournament:<name>", "Delete a recorded match result, or undo a match being called off. Clear later results that depend on it first."),
            ("/admin withdraw tournament:<name> entrant:<name> reason:<withdrawn|disqualified>", "Take an entrant out. Their remaining matches go to their opponents by walkover, and the post says an organizer did it. `/admin reinstate` brings them back."),
            ("/admin matchrooms category:<category>", "Pick the category whose voice channels called matches are played in. Add or remove rooms in Discord."),
            ("/admin matchslots tournament:<name> match_slots:<n>", "Set how many of a tournament's matches the bot calls at once, or leave it blank to stop calling them."),
            ("/admin skipmatch tournament:<name>", "Call a match off with no winner, such as when neither side turns up. Whoever would have played the winner next advances by walkover."),
            ("/admin editteam tournament:<name> team:<name>", "Fix an entrant's name or seed after import."),
            ("/admin clearpredictions tournament:<name> user:<member>", "Wipe one user's saved predictions."),
            ("/admin deletetournament tournament:<name> confirm_name:<name>", "Permanently delete a tournament and all its data."),
            ("/rating user:<member>", "See any member's Elo rating. Members can only see their own."),
            ("/admin linkplayer tournament:<name> entrant:<name> user:<member>", "Link an entrant to the member who plays as them, such as one entered by name before they agreed, so its games count for their rating."),
            ("/admin unlinkplayer tournament:<name> entrant:<name>", "Unlink the member behind an entrant, such as one linked to the wrong entry. Their agreement and other entries stay as they are."),
            ("/admin setcaptain tournament:<name> entrant:<name>", "5v5: make a player (with a linked account) captain of their team, such as when a team has none."),
            ("/admin lobbyswap tournament:<name> leaving:<name> joining:<name>", "5v5: put a waitlisted player in someone's spot. The player leaving goes to the front of the waitlist."),
            ("/admin lobbymove tournament:<name> entrant:<name>", "5v5: move a player to the other team in their lobby, such as to give a team with no captain one. Their old spot is filled from the waitlist."),
            ("/admin checkin tournament:<name> entrant:<name>", "5v5: check a player in on their behalf while check-in is open."),
            ("/admin feedback [page:<n>]", "Read anonymous feedback, newest first. `/admin deletefeedback number:<n>` removes one."),
            ("/admin ban user:<member> [reason:<text>]", "Block a member from using the bot. `/admin unban` undoes it, and `/admin bans` lists everyone banned."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="5v5", description="How a 5v5 event works, for players and organizers")
    async def five_v_five(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("5v5 events", [
            ("1. Sign up",
             "Press **Register** on the sign-ups post (or `/register tournament:<name> captain:<choice>`) and say whether you want to captain, can if "
             "needed, or won't. Each team needs a captain, who hosts the drafter room and reports the points."),
            ("2. Lobbies are drawn",
             "Ahead of the event, players are drawn into lobbies of two teams of up to five, balanced by past "
             "5v5 results, with captains picked automatically. They're all on one post in the channel, "
             "with the waitlist, and you're pinged there. Can't make it? `/match dropout` gives your spot to the "
             "next player waiting."),
            ("3. Check in",
             "Before play starts you're pinged to press **Check In** (or `/checkin`). Anyone in a lobby who "
             "hasn't checked in by the end is replaced by the next checked-in player on the waitlist."),
            ("4. Play",
             f"A captain creates the room at {DRAFTER_HOST_URL} and presses **Submit Room Code**. "
             "Everyone else presses **Get Room Code** on the event's post, joins, drafts, trains their character "
             "and races. Each race gives 5, 3 and 1 points for 1st, 2nd and 3rd."),
            ("5. Results",
             "After the races (and a tiebreaker track if the teams are level), both captains press **Finish**. "
             "A results post for the lobby then has a button for each team, and each captain presses both to enter "
             "everyone's points. Matching points are final; if only one captain finishes entering theirs, they're "
             "accepted after 10 minutes. Points that don't match wait for a captain to correct theirs, or for an organizer. "
             "`/rating` shows your 5v5 contribution."),
            ("Organizers",
             "`/registration open5v5 name:<name> starts_in_hours:<hours>`, then `/registration close` "
             "(ideally a day before) to preview and create the lobbies. Start check-in with the button on the "
             "event's post or `/lobby startcheckin`, once every team has a captain; `/lobby cancelcheckin` calls it "
             "off. **Finish** (or `/lobby finish lobby:<n>`) finishes a lobby without waiting for its captains, "
             "**Finalize Scores** on a results post settles points that don't match, `/lobby report lobby:<n>` "
             "records points directly, and "
             "`/tournament conclude` posts how each lobby went."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="features", description="What WinningTicket can be used for, and what's coming")
    @app_commands.guild_only()
    async def features(self, interaction: discord.Interaction) -> None:
        # The feature list is longer than an embed field allows (1024 characters), so it goes in the description.
        embed = _command_list_embed("WinningTicket", [
            ("Coming soon", "\n".join(f"• {line}" for line in PLANNED_FEATURES)),
        ])
        embed.description = "**What it does**\n" + "\n".join(f"• {line}" for line in CURRENT_FEATURES)
        embed.set_footer(text="See /help players for the commands.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
