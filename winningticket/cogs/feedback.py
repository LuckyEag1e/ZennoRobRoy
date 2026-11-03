from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
from common import BotModal, MemberCooldown

# Long enough for a real message, short enough that each one fits an embed field when read back.
FEEDBACK_MAX_LENGTH = 1000
# How long someone waits between messages, so one person can't flood the inbox.
FEEDBACK_COOLDOWN_SECONDS = 300


class FeedbackCooldown(MemberCooldown):
    """When each member last sent feedback. Being only in memory, it never links a stored
    message to its sender."""

    def __init__(self) -> None:
        super().__init__(FEEDBACK_COOLDOWN_SECONDS)

    async def refuse_if_waiting(self, interaction: discord.Interaction) -> bool:
        """If this member sent feedback too recently, tell them how long to wait and return True."""
        wait = self.seconds_left(interaction)
        if wait <= 0:
            return False
        minutes = max(1, round(wait / 60))
        await interaction.response.send_message(
            f"You can send more feedback in about {minutes} minute{'s' if minutes != 1 else ''}.", ephemeral=True
        )
        return True


class FeedbackModal(BotModal, title="Anonymous Feedback"):
    feedback_text = discord.ui.TextInput(
        label="Your Feedback",
        style=discord.TextStyle.paragraph,
        placeholder="Organizers can read this, but they won't see who sent it.",
        max_length=FEEDBACK_MAX_LENGTH,
        # Discord rejects a required field holding only spaces with a vague error of its
        # own, so the field is optional and on_submit turns blank text away instead.
        required=False,
    )

    def __init__(self, cooldown: FeedbackCooldown) -> None:
        super().__init__()
        self.cooldown = cooldown

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Checked again here, since several forms can be opened before any is sent.
        if await self.cooldown.refuse_if_waiting(interaction):
            return
        message = self.feedback_text.value.strip()
        if not message:
            await interaction.response.send_message("Your feedback was empty, so nothing was sent.", ephemeral=True)
            return
        conn = db.get_connection()
        try:
            db.add_feedback(conn, interaction.guild_id, message)
        finally:
            conn.close()
        self.cooldown.record(interaction)
        await interaction.response.send_message(
            "Thanks! Your feedback was sent to the organizers anonymously.", ephemeral=True
        )


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cooldown = FeedbackCooldown()

    @app_commands.command(name="feedback", description="Send anonymous feedback to the organizers")
    @app_commands.guild_only()
    async def feedback(self, interaction: discord.Interaction) -> None:
        if await self.cooldown.refuse_if_waiting(interaction):
            return
        await interaction.response.send_modal(FeedbackModal(self.cooldown))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackCog(bot))
