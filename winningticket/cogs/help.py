from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from common import requires_manage_server


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
            ("/predict tournament:<name>", "Fill out your bracket prediction, round by round."),
            ("/mypicks tournament:<name>", "See your saved picks and current score."),
            ("/tournament bracket name:<name>", "See the real tournament bracket's progress."),
            ("/tournament leaderboard name:<name>", "See the standings."),
            ("/match stats tournament:<name>", "See what % of the pool picked each side of a matchup."),
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="organizers", description="How to run a tournament, step by step")
    @requires_manage_server
    async def organizers(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Running a tournament",
            description=(
                "**1. Create it** from a bracket CSV\n"
                "`/tournament create name:<name> csv_file:<file>`\n"
                "CSV needs a `team_a,team_b` header, one row per round-1 matchup in "
                "bracket order (row count must be a power of 2: 2, 4, 8, 16...). "
                "Leave one side blank to give that entrant a bye.\n\n"
                "**2. Open predictions**\n"
                "`/tournament open name:<name>`\n"
                "Lets players start filling out brackets with `/predict`.\n\n"
                "**3. Lock it** once the real tournament is about to start\n"
                "`/tournament lock name:<name>`\n"
                "Stops new or changed predictions; from here on `/match result` is used "
                "to record what actually happens.\n\n"
                "**4. Record results** as real games finish\n"
                "`/match result tournament:<name>`\n"
                "Pick a ready match from the list, then pick the winner. Repeat for "
                "each match, in any order, until a champion is decided — the bot "
                "announces each result and advancement to the channel automatically.\n\n"
                "**5. Check progress anytime**\n"
                "`/tournament bracket name:<name>` — the live bracket\n"
                "`/tournament leaderboard name:<name>` — standings\n"
                "`/match stats tournament:<name>` — pick breakdown for a matchup\n\n"
                "**6. Conclude it** once the final is recorded\n"
                "`/tournament conclude name:<name>`\n"
                "Posts the podium and the top pick'em scorers to the channel. The tournament's "
                "results are kept, but it's removed from every command and its name is free "
                "for a new tournament. Only a completed tournament can be concluded.\n\n"
                "Need to fix a mistake (wrong result, bad team name, etc.)? See `/help admin`."
            ),
        )
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
        ])
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
