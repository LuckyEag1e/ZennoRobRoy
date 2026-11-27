import asyncio
from datetime import date

import pytest

from cogs.banners import BannerCog, PullDecisionButton
from store import Store
from tests.fakes import FakeBot, FakeGuild, FakeInteraction, FakePermissions, FakeRole, FakeUser, invoke

TODAY = date(2026, 11, 27)
APPROVER_ROLE = FakeRole()


class World:
    """One server with a Fuku cog in it, plus the people who use it."""

    def __init__(self) -> None:
        self.store = Store(":memory:")
        self.bot = FakeBot()
        self.cog = BannerCog(self.bot, self.store, approver_role_id=APPROVER_ROLE.id, today=lambda: TODAY)
        self.bot.cogs["BannerCog"] = self.cog
        self.owner = FakeUser("Owner")
        self.guild = FakeGuild(owner_id=self.owner.id)
        self.admin = FakeUser("Admin", guild_permissions=FakePermissions(administrator=True))
        self.pr_head = FakeUser("Head of PR", roles=[APPROVER_ROLE])
        self.member = FakeUser("Member")

    def interaction(self, user):
        return FakeInteraction(user, self.guild, self.bot)

    def run(self, command, user, **options):
        return asyncio.run(invoke(self.cog, command, self.interaction(user), **options))

    def create(self, user=None, name="Summer Maruzensky", **options):
        options = {"featured": "Maruzensky (Summer)", "end_date": "2026-12-10", **options}
        return self.run("create_banner", user or self.admin, name=name, **options)

    def click(self, user, pull_id, action):
        interaction = self.interaction(user)
        asyncio.run(PullDecisionButton(pull_id, action).callback(interaction))
        return interaction.reply


@pytest.fixture
def world():
    return World()


def pending_pull(world, user=None, pulls=10, featured=1, off_banner=2):
    user = user or world.member
    world.create()
    world.run("join", user, banner="Summer Maruzensky")
    reply = world.run("pulled", user, banner="Summer Maruzensky", pulls=pulls, featured=featured, off_banner=off_banner)
    [pull_id] = {button.pull_id for button in buttons(reply)}
    return pull_id


def buttons(reply):
    return [item for item in reply.view.children if isinstance(item, PullDecisionButton)]


# /create_banner


def test_members_cannot_create_banners(world):
    reply = world.create(world.member)
    assert reply.ephemeral
    assert world.store.open_banners(world.guild.id, TODAY) == []


@pytest.mark.parametrize("who", ["admin", "owner", "pr_head"])
def test_admins_the_owner_and_the_approver_role_can_create_banners(world, who):
    reply = world.create(getattr(world, who))
    assert not reply.ephemeral
    assert len(world.store.open_banners(world.guild.id, TODAY)) == 1


def test_the_banner_announcement_shows_what_it_is(world):
    reply = world.create(featured_rate=1.5)
    for detail in ("Summer Maruzensky", "Maruzensky (Summer)", "1.5%", "2026-12-10", "/join"):
        assert detail in reply.content


def test_the_featured_rate_defaults_to_0_75_percent(world):
    world.create()
    [banner] = world.store.open_banners(world.guild.id, TODAY)
    assert banner.featured_rate == 0.75


@pytest.mark.parametrize("rate", [0, -1, 100.5])
def test_a_featured_rate_must_be_a_percentage_above_zero(world, rate):
    reply = world.create(featured_rate=rate)
    assert reply.ephemeral
    assert world.store.open_banners(world.guild.id, TODAY) == []


@pytest.mark.parametrize("end_date", ["10/12/2026", "tomorrow", "2026-11-26"])
def test_the_end_date_must_be_a_real_date_that_has_not_passed(world, end_date):
    reply = world.create(end_date=end_date)
    assert reply.ephemeral
    assert "YYYY-MM-DD" in reply.content or "passed" in reply.content
    assert world.store.open_banners(world.guild.id, TODAY) == []


def test_a_banner_can_end_today(world):
    assert not world.create(end_date="2026-11-27").ephemeral


def test_two_banners_cannot_share_a_name(world):
    world.create()
    reply = world.create(name="summer maruzensky")
    assert reply.ephemeral
    assert "already" in reply.content


# /join


def test_members_can_join_an_open_banner(world):
    world.create()
    reply = world.run("join", world.member, banner="Summer Maruzensky")
    assert reply.ephemeral
    [banner] = world.store.open_banners(world.guild.id, TODAY)
    assert world.store.has_joined(banner.id, world.member.id)


def test_joining_twice_says_you_are_already_in(world):
    world.create()
    world.run("join", world.member, banner="Summer Maruzensky")
    assert "already" in world.run("join", world.member, banner="Summer Maruzensky").content


def test_joining_a_banner_that_does_not_exist_says_so(world):
    reply = world.run("join", world.member, banner="Nope")
    assert reply.ephemeral
    assert "Nope" in reply.content


def test_the_banner_option_suggests_open_banners_matching_what_was_typed(world):
    world.create()
    world.create(name="Halloween Rice")
    choices = asyncio.run(world.cog.banner_autocomplete(world.interaction(world.member), "rice"))
    assert [choice.value for choice in choices] == ["Halloween Rice"]


# /pulled


def test_you_have_to_join_a_banner_before_reporting_pulls_on_it(world):
    world.create()
    reply = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=10, featured=1, off_banner=0)
    assert reply.ephemeral
    assert "/join" in reply.content


@pytest.mark.parametrize("featured, off_banner", [(11, 0), (0, 11), (6, 5)])
def test_featured_and_off_banner_ssrs_together_cannot_be_more_than_the_pulls(world, featured, off_banner):
    world.create()
    world.run("join", world.member, banner="Summer Maruzensky")
    reply = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=10, featured=featured,
                      off_banner=off_banner)
    assert reply.ephemeral


def test_every_pull_can_be_an_ssr(world):
    world.create()
    world.run("join", world.member, banner="Summer Maruzensky")
    reply = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=10, featured=5, off_banner=5)
    assert not reply.ephemeral


def test_the_pulled_options_are_named_for_what_they_count(world):
    command = next(c for c in world.cog.get_app_commands() if c.name == "pulled")
    options = {param.display_name: param.description for param in command.parameters}
    assert options == {
        "banner": options["banner"],
        "number_of_pulls": "Number of Pulls",
        "count_of_featured": "Count of Featured",
        "count_of_off_banner_ssrs": "Count of Off-banner SSRs",
    }


def test_a_report_is_posted_publicly_for_approval(world):
    world.create()
    world.run("join", world.member, banner="Summer Maruzensky")
    reply = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=10, featured=1, off_banner=2)
    assert not reply.ephemeral
    assert world.member.mention in reply.content
    assert "10" in reply.content and "1 featured" in reply.content and "2 off-banner SSRs" in reply.content
    assert sorted(button.action for button in buttons(reply)) == ["approve", "reject"]
    [banner] = world.store.open_banners(world.guild.id, TODAY)
    assert world.store.totals(banner.id, world.member.id) == (0, 0, 0)  # not until approved


# Approving and rejecting


def test_members_cannot_approve_pulls(world):
    pull_id = pending_pull(world)
    other = FakeUser("Someone else")
    reply = world.click(other, pull_id, "approve")
    assert reply.ephemeral
    assert world.store.get_pull(pull_id).status == "pending"


@pytest.mark.parametrize("who", ["admin", "owner", "pr_head"])
def test_approvers_can_approve_their_own_pulls(world, who):
    approver = getattr(world, who)
    pull_id = pending_pull(world, user=approver)
    reply = world.click(approver, pull_id, "approve")
    assert reply.edited
    assert world.store.get_pull(pull_id).status == "approved"


@pytest.mark.parametrize("who", ["admin", "owner", "pr_head"])
def test_approving_counts_the_pulls_and_closes_the_post(world, who):
    pull_id = pending_pull(world, pulls=10, featured=1, off_banner=2)
    reply = world.click(getattr(world, who), pull_id, "approve")
    assert reply.edited
    assert reply.view is None
    pull = world.store.get_pull(pull_id)
    assert pull.status == "approved"
    assert world.store.totals(pull.banner_id, world.member.id) == (10, 1, 2)
    assert "approved" in reply.content.lower()
    assert "10 pulls, 1 featured, 2 off-banner SSRs" in reply.content  # the running total


def test_rejecting_does_not_count_the_pulls(world):
    pull_id = pending_pull(world)
    reply = world.click(world.admin, pull_id, "reject")
    assert reply.edited and reply.view is None
    pull = world.store.get_pull(pull_id)
    assert pull.status == "rejected"
    assert world.store.totals(pull.banner_id, world.member.id) == (0, 0, 0)


def test_a_second_click_after_a_decision_changes_nothing(world):
    pull_id = pending_pull(world)
    world.click(world.admin, pull_id, "reject")
    reply = world.click(world.owner, pull_id, "approve")
    assert reply.ephemeral
    assert world.store.get_pull(pull_id).status == "rejected"


def test_buttons_still_work_after_a_restart(world):
    # discord.py rebuilds the button from its custom_id alone.
    button = PullDecisionButton(42, "approve")
    match = PullDecisionButton.__discord_ui_compiled_template__.fullmatch(button.item.custom_id)
    rebuilt = asyncio.run(PullDecisionButton.from_custom_id(None, button.item, match))
    assert (rebuilt.pull_id, rebuilt.action) == (42, "approve")


def test_fuku_posts_have_her_fortune_telling_flair(world):
    sparkle = ("🔮", "✨")
    assert any(emoji in world.create().content for emoji in sparkle)
    assert any(emoji in world.run("join", world.member, banner="Summer Maruzensky").content for emoji in sparkle)
    report = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=10, featured=1, off_banner=0)
    assert any(emoji in report.content for emoji in sparkle)
    [pull_id] = {button.pull_id for button in buttons(report)}
    approved = world.click(world.admin, pull_id, "approve")
    assert any(emoji in approved.content for emoji in sparkle)


@pytest.mark.parametrize("pulls, wording", [(12, "pulled **12** times on the **Summer Maruzensky** banner"),
                                            (1, "pulled **1** time on the **Summer Maruzensky** banner")])
def test_a_report_reads_as_a_sentence(world, pulls, wording):
    world.create()
    world.run("join", world.member, banner="Summer Maruzensky")
    reply = world.run("pulled", world.member, banner="Summer Maruzensky", pulls=pulls, featured=0, off_banner=0)
    assert wording in reply.content
