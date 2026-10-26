from __future__ import annotations

import time
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

import db

# Long enough for a real message, short enough that each one fits an embed field when read back.
FEEDBACK_MAX_LENGTH = 1000
# How long someone waits between messages, so one person can't flood the inbox.
FEEDBACK_COOLDOWN_SECONDS = 300


class FeedbackModal(discord.ui.Modal, title="Anonymous feedback"):
    feedback_text = discord.ui.TextInput(
        label="Your feedback",
        style=discord.TextStyle.paragraph,
        placeholder="Organizers can read this, but they won't see who sent it.",
        max_length=FEEDBACK_MAX_LENGTH,
    )

    def __init__(self, on_sent: Callable[[], None]) -> None:
        super().__init__()
        self.on_sent = on_sent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        message = self.feedback_text.value.strip()
        if not message:
            await interaction.response.send_message("Your feedback was empty, so nothing was sent.", ephemeral=True)
            return
        conn = db.get_connection()
        try:
            db.add_feedback(conn, interaction.guild_id, message)
        finally:
            conn.close()
        self.on_sent()
        await interaction.response.send_message(
            "Thanks! Your feedback was sent to the organizers anonymously.", ephemeral=True
        )


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # When each (guild, user) last sent feedback. It lives only in memory, so nothing
        # stored links a message to its sender.
        self.last_sent: dict[tuple[int, int], float] = {}

    def _record_sent(self, key: tuple[int, int]) -> None:
        now = time.monotonic()
        self.last_sent = {k: sent for k, sent in self.last_sent.items() if now - sent < FEEDBACK_COOLDOWN_SECONDS}
        self.last_sent[key] = now

    @app_commands.command(name="feedback", description="Send anonymous feedback to the organizers")
    @app_commands.guild_only()
    async def feedback(self, interaction: discord.Interaction) -> None:
        key = (interaction.guild_id, interaction.user.id)
        wait = self.last_sent.get(key, float("-inf")) + FEEDBACK_COOLDOWN_SECONDS - time.monotonic()
        if wait > 0:
            minutes = max(1, round(wait / 60))
            await interaction.response.send_message(
                f"You can send more feedback in about {minutes} minute{'s' if minutes != 1 else ''}.", ephemeral=True
            )
            return
        await interaction.response.send_modal(FeedbackModal(on_sent=lambda: self._record_sent(key)))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackCog(bot))
