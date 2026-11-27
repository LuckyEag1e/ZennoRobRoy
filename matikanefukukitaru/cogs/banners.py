"""Gacha banners: staff create them, members /join and report their pulls with /pulled, and
each report only counts once an approver presses Approve on it."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from store import AlreadyDecided, Banner, BannerExists, Store

DEFAULT_FEATURED_RATE = 0.75  # percent
MAX_PULLS_PER_REPORT = 1000


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def is_approver(user, guild, approver_role_id: Optional[int]) -> bool:
    """Server admins, the owner, and anyone with the approver role (FUKU_APPROVER_ROLE_ID)."""
    if guild is not None and guild.owner_id == user.id:
        return True
    permissions = getattr(user, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    return approver_role_id is not None and any(role.id == approver_role_id for role in getattr(user, "roles", []))


def describe_report(user_id: int, banner: Banner, pulls: int, featured: int, off_banner: int) -> str:
    times = "time" if pulls == 1 else "times"
    return (f"<@{user_id}> pulled **{pulls}** {times} on the **{banner.name}** banner: "
            f"{featured} featured, {off_banner} off-banner SSRs.")


class PullDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"fuku:pull:(?P<action>approve|reject):(?P<id>\d+)"):
    """Approve / Reject on a /pulled post. Rebuilt from its custom_id after a restart
    (bot.add_dynamic_items), so old posts keep working across deploys."""

    def __init__(self, pull_id: int, action: str) -> None:
        approve = action == "approve"
        super().__init__(discord.ui.Button(
            label="Approve" if approve else "Reject",
            emoji="✅" if approve else "❌",
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
            custom_id=f"fuku:pull:{action}:{pull_id}"))
        self.pull_id = pull_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Button, match) -> PullDecisionButton:
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: BannerCog = interaction.client.get_cog("BannerCog")
        await cog.decide(interaction, self.pull_id, approved=self.action == "approve")


def decision_view(pull_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PullDecisionButton(pull_id, "approve"))
    view.add_item(PullDecisionButton(pull_id, "reject"))
    return view


class BannerCog(commands.Cog):
    def __init__(self, bot, store: Store, *, approver_role_id: Optional[int] = None,
                 today: Callable[[], date] = utc_today) -> None:
        self.bot = bot
        self.store = store
        self.approver_role_id = approver_role_id
        self.today = today

    def _is_approver(self, interaction: discord.Interaction) -> bool:
        return is_approver(interaction.user, interaction.guild, self.approver_role_id)

    @app_commands.command(name="create_banner", description="Start a new gacha banner (admins only)")
    @app_commands.guild_only()
    @app_commands.describe(name="What members pick in /join", featured="The featured character or card",
                           end_date="Last day of the banner, YYYY-MM-DD (UTC)",
                           featured_rate="Chance of the featured pull, in percent (default 0.75)")
    async def create_banner(self, interaction: discord.Interaction, name: str, featured: str, end_date: str,
                            featured_rate: float = DEFAULT_FEATURED_RATE) -> None:
        if not self._is_approver(interaction):
            await interaction.response.send_message("Only admins can create banners.", ephemeral=True)
            return
        if not 0 < featured_rate <= 100:
            await interaction.response.send_message("The featured rate is a percentage above 0 and at most 100.",
                                                    ephemeral=True)
            return
        try:
            ends_on = date.fromisoformat(end_date)
        except ValueError:
            await interaction.response.send_message("Write the end date as YYYY-MM-DD, like 2026-12-31.",
                                                    ephemeral=True)
            return
        if ends_on < self.today():
            await interaction.response.send_message("That end date has already passed.", ephemeral=True)
            return
        name = name.strip()
        try:
            banner = self.store.create_banner(interaction.guild_id, name, featured=featured.strip(),
                                              featured_rate=featured_rate, ends_on=ends_on,
                                              created_by=interaction.user.id)
        except BannerExists:
            await interaction.response.send_message(f"There's already a banner called **{name}**.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔮 New banner: **{banner.name}** ✨\n"
            f"✨ Featured: **{banner.featured}** at {banner.featured_rate:g}%\n"
            f"Open until the end of {banner.ends_on.isoformat()} (UTC)\n"
            f"🔮 Use `/join` to take part, then `/pulled` to report your pulls.")

    @app_commands.command(name="join", description="Join a gacha banner")
    @app_commands.guild_only()
    @app_commands.describe(banner="The banner to join")
    async def join(self, interaction: discord.Interaction, banner: str) -> None:
        found = self.store.find_open_banner(interaction.guild_id, banner, self.today())
        if found is None:
            await interaction.response.send_message(f"There's no open banner called **{banner}**.", ephemeral=True)
            return
        if self.store.join(found.id, interaction.user.id):
            message = f"🔮 You're in **{found.name}**! The fortune looks bright ✨ Report your pulls with `/pulled`."
        else:
            message = f"You've already joined **{found.name}**."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="pulled", description="Report pulls on a banner; an approver confirms them")
    @app_commands.guild_only()
    @app_commands.rename(pulls="number_of_pulls", featured="count_of_featured",
                         off_banner="count_of_off_banner_ssrs")
    @app_commands.describe(banner="The banner you pulled on", pulls="Number of Pulls", featured="Count of Featured",
                           off_banner="Count of Off-banner SSRs")
    async def pulled(self, interaction: discord.Interaction, banner: str,
                     pulls: app_commands.Range[int, 1, MAX_PULLS_PER_REPORT],
                     featured: app_commands.Range[int, 0, MAX_PULLS_PER_REPORT],
                     off_banner: app_commands.Range[int, 0, MAX_PULLS_PER_REPORT]) -> None:
        found = self.store.find_open_banner(interaction.guild_id, banner, self.today())
        if found is None:
            await interaction.response.send_message(f"There's no open banner called **{banner}**.", ephemeral=True)
            return
        if not self.store.has_joined(found.id, interaction.user.id):
            await interaction.response.send_message(f"Use `/join` on **{found.name}** first.", ephemeral=True)
            return
        if featured + off_banner > pulls:
            await interaction.response.send_message(
                "Each pull gives one card, so featured plus off-banner SSRs can't be more than the number of pulls.",
                ephemeral=True)
            return
        pull_id = self.store.record_pull(found.id, interaction.user.id, pulls=pulls, featured=featured,
                                         off_banner=off_banner)
        await interaction.response.send_message(
            describe_report(interaction.user.id, found, pulls, featured, off_banner) + "\n🔮 Waiting for an approver.",
            view=decision_view(pull_id))

    @join.autocomplete("banner")
    @pulled.autocomplete("banner")
    async def banner_autocomplete(self, interaction: discord.Interaction, current: str):
        typed = current.lower()
        return [app_commands.Choice(name=banner.name, value=banner.name)
                for banner in self.store.open_banners(interaction.guild_id, self.today())
                if typed in banner.name.lower()][:25]

    async def decide(self, interaction: discord.Interaction, pull_id: int, *, approved: bool) -> None:
        pull = self.store.get_pull(pull_id)
        if pull is None:
            await interaction.response.send_message("That report no longer exists.", ephemeral=True)
            return
        if not self._is_approver(interaction):
            await interaction.response.send_message("Only admins and approvers can decide on pulls.", ephemeral=True)
            return
        try:
            pull = self.store.decide(pull_id, approver_id=interaction.user.id, approved=approved)
        except AlreadyDecided:
            await interaction.response.send_message("Someone already decided on this one.", ephemeral=True)
            return
        banner = self.store.get_banner(pull.banner_id)
        text = describe_report(pull.user_id, banner, pull.pulls, pull.featured, pull.off_banner)
        if approved:
            total_pulls, total_featured, total_off_banner = self.store.totals(banner.id, pull.user_id)
            text += (f"\n✅ Approved by {interaction.user.mention}. ✨ Total on this banner: "
                     f"{total_pulls} pulls, {total_featured} featured, {total_off_banner} off-banner SSRs.")
        else:
            text += f"\n❌ Rejected by {interaction.user.mention}."
        await interaction.response.edit_message(content=text, view=None)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(PullDecisionButton)
    await bot.add_cog(BannerCog(bot, bot.store, approver_role_id=bot.approver_role_id))
