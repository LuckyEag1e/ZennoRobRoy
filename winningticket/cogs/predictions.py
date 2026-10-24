from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

import db
from bracket import Match, compute_leaderboard, is_bye, matches_in_round, resolve_all_slots, resolve_slot, rounds
from common import resolve_tournament, tournament_name_autocomplete
from embeds import render_my_picks_embed
from labels import pick_review_lines, team_label

SESSION_TIMEOUT_SECONDS = 600


async def _dismiss(message: discord.Message | None) -> None:
    """Best-effort delete of the message the user just acted on, so the
    channel only ever shows the current step instead of accumulating one
    message per answered match."""
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


@dataclass
class PredictionSession:
    tournament_id: int
    user_id: int
    matches_by_id: dict[int, Match]
    feeders_by_target: dict
    teams_by_id: dict
    picks: dict[int, int] = field(default_factory=dict)
    current_round: int = 1
    current_match_index: int = 0
    message: discord.Message | None = None


def build_step_payload(session: PredictionSession) -> tuple[discord.Embed, discord.ui.View]:
    while True:
        round_matches = matches_in_round(session.matches_by_id, session.current_round)
        while session.current_match_index < len(round_matches) and is_bye(
            round_matches[session.current_match_index]
        ):
            session.current_match_index += 1
        if session.current_match_index < len(round_matches):
            break
        next_round = session.current_round + 1
        if next_round not in rounds(session.matches_by_id):
            return build_confirm_payload(session)
        session.current_round = next_round
        session.current_match_index = 0

    match = round_matches[session.current_match_index]
    team_a, team_b = resolve_slot(
        match, session.matches_by_id, session.picks.get, session.feeders_by_target
    )
    a_label = team_label(session.teams_by_id, team_a)
    b_label = team_label(session.teams_by_id, team_b)

    embed = discord.Embed(
        title=f"Round {session.current_round} — Match {session.current_match_index + 1} of {len(round_matches)}",
        description="Who do you predict will win this matchup?",
    )
    embed.add_field(name=a_label, value="​", inline=True)
    embed.add_field(name=b_label, value="​", inline=True)

    view = BracketPredictView(session)
    view.add_item(TeamChoiceButton(session, match.id, team_a, a_label))
    view.add_item(TeamChoiceButton(session, match.id, team_b, b_label))
    return embed, view


def build_confirm_payload(session: PredictionSession) -> tuple[discord.Embed, discord.ui.View]:
    lines = pick_review_lines(session.matches_by_id, session.picks, session.teams_by_id)
    embed = discord.Embed(
        title="Review your bracket",
        description="\n".join(lines) if lines else "No picks made.",
    )
    embed.set_footer(text="Confirm to save, or start over to redo your picks.")

    view = BracketPredictView(session)
    view.add_item(ConfirmSubmitButton(session))
    view.add_item(RestartButton(session))
    return embed, view


class BracketPredictView(discord.ui.View):
    def __init__(self, session: PredictionSession) -> None:
        super().__init__(timeout=SESSION_TIMEOUT_SECONDS)
        self.session = session

    async def on_timeout(self) -> None:
        if self.session.message is None:
            return
        try:
            await self.session.message.edit(
                content="⏰ Your prediction session expired before you finished. Run `/predict` again to restart.",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            pass


class TeamChoiceButton(discord.ui.Button):
    def __init__(self, session: PredictionSession, match_id: int, team_id: int, label: str) -> None:
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
        self.session = session
        self.match_id = match_id
        self.team_id = team_id

    async def callback(self, interaction: discord.Interaction) -> None:
        old_message = self.session.message
        self.session.picks[self.match_id] = self.team_id
        self.session.current_match_index += 1
        embed, view = build_step_payload(self.session)
        # A fresh message per step (never editing the previous one) avoids a
        # Discord desktop-client rendering bug seen with rapid repeated edits
        # to the same ephemeral message's components.
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        self.session.message = await interaction.original_response()
        await _dismiss(old_message)


class ConfirmSubmitButton(discord.ui.Button):
    def __init__(self, session: PredictionSession) -> None:
        super().__init__(label="Confirm & Save Bracket", style=discord.ButtonStyle.success)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        old_message = self.session.message
        conn = db.get_connection()
        try:
            db.upsert_predictions(conn, self.session.tournament_id, self.session.user_id, self.session.picks)
        except Exception:
            await interaction.response.send_message(
                "❌ Something went wrong saving your bracket. Please run `/predict` again.",
                ephemeral=True,
            )
            await _dismiss(old_message)
            raise
        finally:
            conn.close()
        await interaction.response.send_message(
            "✅ Your bracket has been saved! Use `/mypicks` anytime to review it.",
            ephemeral=True,
        )
        await _dismiss(old_message)


class RestartButton(discord.ui.Button):
    def __init__(self, session: PredictionSession) -> None:
        super().__init__(label="Start Over", style=discord.ButtonStyle.secondary)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        old_message = self.session.message
        self.session.picks = {}
        self.session.current_round = 1
        self.session.current_match_index = 0
        embed, view = build_step_payload(self.session)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        self.session.message = await interaction.original_response()
        await _dismiss(old_message)


class PredictionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="predict", description="Fill out your bracket prediction")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def predict(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            if tournament_row["status"] != "predictions_open":
                await interaction.response.send_message(
                    f"Predictions aren't open for **{tournament}** right now (status: {tournament_row['status']}).",
                    ephemeral=True,
                )
                return
            matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament_row["id"])
        finally:
            conn.close()

        session = PredictionSession(
            tournament_id=tournament_row["id"],
            user_id=interaction.user.id,
            matches_by_id=matches_by_id,
            feeders_by_target=feeders,
            teams_by_id=teams_by_id,
        )
        embed, view = build_step_payload(session)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        session.message = await interaction.original_response()

    @app_commands.command(name="mypicks", description="Show your saved bracket predictions")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=tournament_name_autocomplete)
    async def mypicks(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament)
            if tournament_row is None:
                return
            picks = db.get_user_predictions(conn, tournament_row["id"], interaction.user.id)
            if not picks:
                await interaction.response.send_message(
                    "You haven't submitted any predictions for this tournament yet.", ephemeral=True
                )
                return
            matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament_row["id"])
            slots = resolve_all_slots(matches_by_id, picks.get, feeders)
            results_by_match = db.get_results(conn, tournament_row["id"])
            predictions_by_user = db.get_all_predictions(conn, tournament_row["id"])
            scores = compute_leaderboard(matches_by_id, results_by_match, predictions_by_user)
            score = next((s for s in scores if s.user_id == interaction.user.id), None)
        finally:
            conn.close()

        embed = render_my_picks_embed(
            tournament, interaction.user, matches_by_id, feeders, slots, picks, teams_by_id, results_by_match, score
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PredictionsCog(bot))
