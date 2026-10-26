from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db

# Long enough for a real message, short enough that each one fits an embed field when read back.
FEEDBACK_MAX_LENGTH = 1000


class FeedbackModal(discord.ui.Modal, title="Anonymous feedback"):
    feedback_text = discord.ui.TextInput(
        label="Your feedback",
        style=discord.TextStyle.paragraph,
        placeholder="Organizers can read this, but they won't see who sent it.",
        max_length=FEEDBACK_MAX_LENGTH,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            db.add_feedback(conn, interaction.guild_id, self.feedback_text.value.strip())
        finally:
            conn.close()
        await interaction.response.send_message(
            "Thanks! Your feedback was sent to the organizers anonymously.", ephemeral=True
        )


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="feedback", description="Send anonymous feedback to the organizers")
    @app_commands.guild_only()
    # Keeps one person from flooding the inbox. The limit lives only in memory, so nothing
    # stored links a message to its sender.
    @app_commands.checks.cooldown(1, 300, key=lambda interaction: (interaction.guild_id, interaction.user.id))
    async def feedback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(FeedbackModal())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackCog(bot))
