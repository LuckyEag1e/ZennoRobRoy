from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from common import requires_manage_server
from labels import CURRENT_FEATURES, PLANNED_FEATURES


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
            ("/register tournament:<name>", "Sign up for a tournament that's taking entrants (`/unregister` to withdraw)."),
            ("/registration entrants name:<name>", "See who has signed up."),
            ("/rating [user:<member>]", "See your Elo rating from past tournaments, which decides your seed."),
            ("/predict tournament:<name>", "Fill out your bracket prediction, round by round."),
            ("/mypicks tournament:<name>", "See your saved picks and current score."),
            ("/tournament bracket name:<name>", "See the real tournament bracket's progress."),
            ("/tournament leaderboard name:<name>", "See the standings."),
            ("/match stats tournament:<name>", "See what % of the pool picked each side of a matchup."),
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
             "Leave out `closes_in_hours` to close sign-ups by hand, and add `seeding:Random draw` "
             "to skip seeding by rating. `/registration add` signs up "
             "a member for them, or an entrant with no Discord account, and `/registration remove` "
             "withdraws anyone.\n"
             "Or build it from a bracket CSV:\n"
             "`/tournament create name:<name> csv_file:<file>`\n"
             "The CSV needs a `team_a,team_b` header and one row per round-1 matchup in "
             "bracket order (the row count must be a power of 2: 2, 4, 8, 16...). "
             "Leave one side blank to give that entrant a bye. A CSV tournament skips step 2."),
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
             "predictions, and from then on `/match result` records what actually happens."),
            ("5. Record results",
             "`/match result tournament:<name>`\n"
             "As real games finish, pick a ready match from the list, then pick the winner. "
             "Repeat for each match, in any order, until a champion is decided. The bot "
             "announces each result and who advances in the channel."),
            ("6. Check progress anytime",
             "`/tournament bracket name:<name>`: the live bracket\n"
             "`/tournament leaderboard name:<name>`: standings\n"
             "`/match stats tournament:<name>`: pick breakdown for a matchup"),
            ("7. Conclude it",
             "`/tournament conclude name:<name>`\n"
             "Once the final is recorded, this posts the podium and the top pick'em scorers "
             "to the channel. The tournament's results are kept and count toward players' "
             "ratings, but it's removed from every other command and its name is free for a "
             "new tournament. Only a completed tournament can be concluded."),
            ("Fixing mistakes",
             "Wrong result, bad team name, etc.? See `/help admin`."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="admin", description="Troubleshooting/correction commands")
    @requires_manage_server
    async def admin(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("Admin / troubleshooting commands", [
            ("/admin setstatus tournament:<name> status:<status>", "Force a tournament's status directly, bypassing the normal open/lock flow."),
            ("/admin setresult tournament:<name>", "Force or overwrite any known matchup's result (e.g. for testing)."),
            ("/admin clearresult tournament:<name>", "Delete a recorded match result."),
            ("/admin editteam tournament:<name> team:<name>", "Fix a team's name or seed after import."),
            ("/admin clearpredictions tournament:<name> user:<member>", "Wipe one user's saved predictions."),
            ("/admin deletetournament tournament:<name> confirm_name:<name>", "Permanently delete a tournament and all its data."),
            ("/admin linkteam tournament:<concluded> team:<name> user:<member>", "Link a team in a concluded tournament to the member who played it, so its games count for their rating."),
            ("/admin removehistory tournament:<concluded> confirm_name:<name>", "Delete a concluded tournament that shouldn't count for ratings, such as a test."),
            ("/admin feedback [page:<n>]", "Read anonymous feedback, newest first. `/admin deletefeedback number:<n>` removes one."),
            ("/admin ban user:<member> [reason:<text>]", "Block a member from using the bot. `/admin unban` undoes it, and `/admin bans` lists everyone banned."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="features", description="What WinningTicket can be used for, and what's coming")
    @app_commands.guild_only()
    async def features(self, interaction: discord.Interaction) -> None:
        embed = _command_list_embed("WinningTicket", [
            ("What it does", "\n".join(f"• {line}" for line in CURRENT_FEATURES)),
            ("Coming soon", "\n".join(f"• {line}" for line in PLANNED_FEATURES)),
        ])
        embed.set_footer(text="See /help players for the commands.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
