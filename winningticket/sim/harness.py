"""Runs commands, button clicks, menu picks and forms through the real cogs, the way
Discord and bot.py would, and records a transcript and any problems along the way."""

from __future__ import annotations

import importlib
import os
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# bot.py reads the token when imported. The simulation never connects, so any value does.
os.environ.setdefault("DISCORD_BOT_TOKEN", "simulation")

import discord
from discord import app_commands

import bot as bot_module
import db
from sim import checks
from sim.discord_fakes import FakeBot, FakeChannel, FakeInteraction, FakeMessage, FakePermissions, FakeUser

GUILD_ID = 1


@dataclass
class Issue:
    step: int
    actor: str
    action: str
    problem: str

    def __str__(self) -> str:
        return f"step {self.step} ({self.actor}: {self.action}): {self.problem}"


@dataclass
class Simulation:
    """One server with one channel, the bot's cogs, and a throwaway database."""

    bot: FakeBot
    channel: FakeChannel
    cogs: dict[str, object]
    commands: dict[str, tuple[app_commands.Command, object]]
    organizer_commands: set[str]
    issues: list[Issue] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    step: int = 0
    # The tournament's status, kept current by the scenario, and how often each command ran in each.
    stage: str = "-"
    coverage: Counter = field(default_factory=Counter)
    _first_new_message: int = 0
    _actor: str = ""
    _action: str = ""

    @classmethod
    async def create(cls, db_path: Path) -> Simulation:
        """Load the bot's cogs against a new database at `db_path`. This points the db
        module at it; run_scenario puts the old path back afterwards."""
        db.DB_PATH = db_path
        conn = db.get_connection()
        db.init_db(conn)
        conn.close()

        fake_bot = _CogCollector()
        # Load exactly what the real bot loads, through each cog module's own setup().
        for module_name in bot_module.INITIAL_COGS:
            await importlib.import_module(module_name).setup(fake_bot)

        commands = {}
        for cog in fake_bot.cogs.values():
            for command in cog.walk_app_commands():
                if isinstance(command, app_commands.Command):
                    commands[command.qualified_name] = (command, cog)
        organizer_commands = {name for name, (command, _) in commands.items() if _organizer_only(command)}

        sim = cls(fake_bot, None, fake_bot.cogs, commands, organizer_commands)
        sim.channel = FakeChannel(sim._on_message)
        fake_bot.channels[sim.channel.id] = sim.channel
        return sim

    # -- people -------------------------------------------------------------------

    def add_user(self, user_id: int, name: str, organizer: bool = False) -> FakeUser:
        user = FakeUser(user_id, name, FakePermissions(manage_guild=organizer))
        self.bot.users[user_id] = user
        return user

    # -- actions --------------------------------------------------------------------

    async def run(self, user: FakeUser, name: str, /, **options) -> FakeInteraction:
        """Run a slash command as `user`. Returns the interaction, whose `sent` is the reply."""
        command, cog = self.commands[name]
        shown = " ".join(f"{key}:{value.display_name if isinstance(value, FakeUser) else value}"
                         for key, value in options.items() if value is not None)
        self._begin(user, f"/{name} {shown}".strip())
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.application_command, command=command,
                                      options=options)
        self.coverage[(name, self.stage)] += 1
        root = command.root_parent
        if root is not None and root.default_permissions is not None and not user.guild_permissions.manage_guild:
            # Discord hides the whole group from members without its default permissions.
            self._note("(Discord doesn't show this command to them)")
            return interaction

        async def invoke() -> None:
            try:
                if await command._check_can_run(interaction):
                    await command.callback(cog, interaction, **options)
            except app_commands.AppCommandError as error:
                await bot_module.WinningTicketBot.on_app_command_error(None, interaction, error)

        await self._dispatch(interaction, invoke)
        return interaction

    async def autocomplete(self, user: FakeUser, name: str, option: str, current: str = "", /, **filled) -> list[str]:
        command, _ = self.commands[name]
        callback = command._params[option].autocomplete
        if callback is None:
            return []
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.autocomplete, command=command, options=filled)
        if not await bot_module.WinningTicketTree.interaction_check(None, interaction):
            return []
        choices = await callback(interaction, current)
        if len(choices) > checks.SELECT_OPTIONS_LIMIT:
            self._problem(f"/{name} {option} autocomplete offered {len(choices)} choices")
        return [choice.value for choice in choices]

    async def click(self, user: FakeUser, message: FakeMessage, label: str,
                    late: bool = False) -> Optional[FakeInteraction]:
        """Click the button labelled `label` on `message`. Returns None if there's no such
        button the user could click. With `late`, the click counts even if the bot has
        since deleted the message, the way a quick double-click reaches Discord before
        the deletion does."""
        button = next((item for item in self._items(user, message, late)
                       if isinstance(item, discord.ui.Button) and item.label == label and not item.disabled), None)
        if button is None:
            return None
        self._begin(user, f"clicks [{label}]" + (" again, a moment too late" if late else ""))
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.component, message=message,
                                      data={"custom_id": button.custom_id})
        await self._dispatch(interaction, lambda: self._run_item(message.view, button, interaction))
        return interaction

    async def choose(self, user: FakeUser, message: FakeMessage, value: str) -> Optional[FakeInteraction]:
        """Pick `value` from the select menu on `message`."""
        menu = next((item for item in self._items(user, message) if isinstance(item, discord.ui.Select)), None)
        if menu is None or value not in [option.value for option in menu.options]:
            return None
        label = next(option.label for option in menu.options if option.value == value)
        self._begin(user, f"picks '{label}'")
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.component, message=message,
                                      data={"custom_id": menu.custom_id, "values": [value]})
        await self._dispatch(interaction, lambda: self._run_item(message.view, menu, interaction))
        return interaction

    async def submit_form(self, user: FakeUser, modal: discord.ui.Modal, text: str) -> Optional[FakeInteraction]:
        """Type `text` into a one-field form and send it."""
        text_input = next(item for item in modal.children if isinstance(item, discord.ui.TextInput))
        text = text[:text_input.max_length] if text_input.max_length else text  # the box stops further typing
        self._begin(user, f"sends form {text!r}")
        if text_input.required and not text.strip():
            # Discord trims the text and refuses a required field that ends up empty, with its own vague error.
            self._problem("Discord refused the form: a required field held only spaces")
            return None
        text_input._value = text
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.modal_submit)
        await self._dispatch(interaction, lambda: self._submit(modal, interaction))
        return interaction

    async def run_background(self, label: str, work) -> None:
        """Run something the bot does on its own, like the sign-ups loop."""
        self._begin(None, label)
        try:
            await work()
        except Exception:
            self._problem("crashed:\n" + traceback.format_exc())
        self._after_step()

    # -- reading what happened ----------------------------------------------------------

    def visible_messages(self, user: FakeUser) -> list[FakeMessage]:
        return [m for m in self.channel.messages if not m.deleted and m.visible_to in (None, user.id)]

    def public_posts_since(self, index: int) -> list[FakeMessage]:
        return [m for m in self.channel.messages[index:] if m.public]

    # -- internals ----------------------------------------------------------------------

    async def _dispatch(self, interaction: FakeInteraction, work) -> None:
        try:
            # The bot's ban check runs before commands; buttons and forms go through
            # their own views, so it only applies to commands and autocomplete.
            if interaction.type != discord.InteractionType.application_command or \
                    await bot_module.WinningTicketTree.interaction_check(None, interaction):
                await work()
        except Exception:
            self._problem("crashed:\n" + traceback.format_exc())
        else:
            if not interaction.response.is_done():
                self._problem("got no reply (Discord shows 'The application did not respond')")
        if interaction.sent is not None:
            self._note(_summary(interaction.sent))
        elif interaction.response.kind == "edit":
            self._note(_summary(interaction.message))
        elif interaction.response.kind == "modal":
            self._note("(a form opens)")
        self._after_step(shown=interaction.sent)

    @staticmethod
    async def _submit(modal: discord.ui.Modal, interaction: FakeInteraction) -> None:
        if await modal.interaction_check(interaction):
            await modal.on_submit(interaction)

    @staticmethod
    async def _run_item(view: discord.ui.View, item: discord.ui.Item, interaction: FakeInteraction) -> None:
        if await view.interaction_check(interaction):
            await item.callback(interaction)

    def _items(self, user: FakeUser, message: FakeMessage, late: bool = False) -> list[discord.ui.Item]:
        if (message.deleted and not late) or message.view is None or message.visible_to not in (None, user.id):
            return []
        return list(message.view.children)

    def _begin(self, user: Optional[FakeUser], action: str) -> None:
        self.step += 1
        self._actor = user.display_name if user is not None else "(bot)"
        self._action = action
        self._first_new_message = len(self.channel.messages)
        self.transcript.append(f"{self.step:4} {self._actor}: {action}")

    def _note(self, text: str) -> None:
        self.transcript.append(f"       -> {text}")

    def _problem(self, problem: str) -> None:
        self.issues.append(Issue(self.step, self._actor, self._action, problem))
        self.transcript.append(f"       !! {problem.splitlines()[0]}")

    def _on_message(self, message: FakeMessage) -> None:
        for problem in checks.message_problems(message):
            self._problem(f"{problem}: {_summary(message)}")
        for problem in checks.organizer_commands_named(message, self.organizer_commands):
            self._problem(f"{problem}: {_summary(message)}")

    def _after_step(self, shown: Optional[FakeMessage] = None) -> None:
        # Anything else the step posted, like a result announcement or a follow-up.
        for message in self.channel.messages[self._first_new_message:]:
            if message is not shown:
                self._note(_summary(message))
        for user in self.bot.users.values():
            while user.dms:
                self._problem(f"the bot DMed {user.display_name}: {user.dms.pop(0)!r}")
        conn = db.get_connection()
        try:
            for problem in checks.database_problems(conn, GUILD_ID):
                self._problem(problem)
        finally:
            conn.close()


class _CogCollector(FakeBot):
    """A FakeBot that also takes add_cog(), so each cog module's setup() can load it."""

    def __init__(self) -> None:
        super().__init__()
        self.cogs: dict[str, object] = {}

    async def add_cog(self, cog) -> None:
        self.cogs[type(cog).__name__] = cog


def _organizer_only(command: app_commands.Command) -> bool:
    root = command.root_parent
    group_needs_admin = root is not None and root.default_permissions is not None
    return group_needs_admin or bool(command.checks)


def _summary(message: FakeMessage) -> str:
    where = "public" if message.public else "private"
    text = " / ".join(line for line in message.text().splitlines() if line.strip())
    return f"[{where}] {text[:160]}{'…' if len(text) > 160 else ''}"
