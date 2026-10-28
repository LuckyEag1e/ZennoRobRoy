"""Runs commands, button clicks, menu picks and forms through the real cogs, the way
Discord and bot.py would, and records a transcript and any problems along the way."""

from __future__ import annotations

import asyncio
import importlib
import os
import random
import traceback
from collections import Counter
from contextvars import ContextVar
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


class Crowd:
    """Runs several actors' flows at once, the way people on a server overlap: each flow
    pauses before every Discord action, and a seeded random choice decides whose action
    happens next. An action itself runs whole, so its transcript lines and checks stay
    its own, and the same seed always gives the same order."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.members: set[asyncio.Task] = set()
        self.waiting: list[asyncio.Future] = []

    async def run(self, flows: list) -> None:
        async def member(flow) -> None:
            try:
                await flow
            finally:
                self.members.discard(asyncio.current_task())
                self._release_one()

        tasks = [asyncio.ensure_future(member(flow)) for flow in flows]
        self.members.update(tasks)
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        failure = next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
        if failure is not None:
            raise failure

    async def wait_turn(self) -> None:
        turn = asyncio.get_running_loop().create_future()
        self.waiting.append(turn)
        self._release_one()
        await turn

    def _release_one(self) -> None:
        # Choose only once every flow is waiting or done, so the order never depends on timing.
        if self.waiting and len(self.waiting) == len(self.members):
            self.waiting.pop(self.rng.randrange(len(self.waiting))).set_result(None)


@dataclass
class Issue:
    step: int
    actor: str
    action: str
    problem: str

    def __str__(self) -> str:
        where = f"{self.actor}: {self.action}" if self.action else self.actor
        return f"step {self.step} ({where}): {self.problem}"


@dataclass
class Step:
    """One Discord action in progress. When the bot handles several at once, their pieces
    interleave, so each step keeps its own transcript lines and messages and writes them
    out together when it ends."""

    number: int
    actor: str
    action: str
    user_id: Optional[int] = None
    lines: list[str] = field(default_factory=list)
    messages: list[FakeMessage] = field(default_factory=list)


# The step the running task is in. Each asyncio task has its own, so steps that run at the
# same time can't mix up each other's lines.
_current_step: ContextVar[Optional[Step]] = ContextVar("sim_step", default=None)


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
    crowd: Optional[Crowd] = None
    # When set, every Discord call also pauses for the network, so the bot handles several
    # interactions at once and they interleave inside its own code, not only between steps.
    simultaneous: bool = False
    # How many new public messages each member's actions caused, by user id.
    public_posts: Counter = field(default_factory=Counter)
    _undo: list = field(default_factory=list)  # (module, attribute, original value) to put back in close()
    _seen_messages: set = field(default_factory=set)

    @classmethod
    async def create(cls, db_path: Path) -> Simulation:
        """Load the bot's cogs against a new database at `db_path`. This points the db
        module at it, and close() puts everything back."""
        undo = [(db, "DB_PATH", db.DB_PATH), (db, "record_result", db.record_result)]
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

        sim = cls(fake_bot, None, fake_bot.cogs, commands, organizer_commands, _undo=undo)
        sim.channel = FakeChannel(sim._on_message, sim._network)
        fake_bot.channels[sim.channel.id] = sim.channel
        db.record_result = sim._checked_record_result(db.record_result)
        return sim

    def close(self) -> None:
        """Undo what create() changed outside the simulation."""
        for module, attribute, original in reversed(self._undo):
            setattr(module, attribute, original)
        self._undo = []

    # -- people -------------------------------------------------------------------

    def add_user(self, user_id: int, name: str, organizer: bool = False) -> FakeUser:
        user = FakeUser(user_id, name, FakePermissions(manage_guild=organizer))
        self.bot.users[user_id] = user
        return user

    # -- actions --------------------------------------------------------------------

    async def together(self, flows: list, rng: random.Random) -> None:
        """Run several actors' flows at the same time, interleaving their Discord actions."""
        self.crowd = Crowd(rng)
        try:
            await self.crowd.run(flows)
        finally:
            self.crowd = None

    async def narrate(self, text: str) -> None:
        """Note something an actor does that isn't a Discord action, in its place in the order."""
        await self._turn()
        self.transcript.append(f"       ({text})")

    async def _turn(self) -> None:
        if self.crowd is not None and asyncio.current_task() in self.crowd.members:
            await self.crowd.wait_turn()

    async def _network(self) -> None:
        if self.simultaneous:
            await self._turn()

    async def run(self, user: FakeUser, name: str, /, **options) -> FakeInteraction:
        """Run a slash command as `user`. Returns the interaction, whose `sent` is the reply."""
        await self._turn()
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
            self._after_step()
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
        await self._turn()
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
        await self._turn()
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
        await self._turn()
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
        await self._turn()
        text_input = next(item for item in modal.children if isinstance(item, discord.ui.TextInput))
        text = text[:text_input.max_length] if text_input.max_length else text  # the box stops further typing
        self._begin(user, f"sends form {text!r}")
        if text_input.required and not text.strip():
            # Discord trims the text and refuses a required field that ends up empty, with its own vague error.
            self._problem("Discord refused the form: a required field held only spaces")
            self._after_step()
            return None
        text_input._value = text
        interaction = FakeInteraction(bot=self.bot, user=user, channel=self.channel, guild_id=GUILD_ID,
                                      kind=discord.InteractionType.modal_submit)
        await self._dispatch(interaction, lambda: self._submit(modal, interaction))
        return interaction

    async def run_background(self, label: str, work) -> None:
        """Run something the bot does on its own, like the sign-ups loop."""
        await self._turn()
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
        self._after_step(shown=interaction.sent if interaction.sent is not None else interaction.message)

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
        step = Step(self.step, user.display_name if user is not None else "(bot)", action,
                    user.id if user is not None else None)
        step.lines.append(f"{step.number:4} {step.actor}: {action}")
        _current_step.set(step)

    def _note(self, text: str) -> None:
        step = _current_step.get()
        (step.lines if step is not None else self.transcript).append(f"       -> {text}")

    def _problem(self, problem: str) -> None:
        step = _current_step.get()
        if step is None:
            self.issues.append(Issue(self.step, "(simulation)", "", problem))
            self.transcript.append(f"       !! {problem.splitlines()[0]}")
            return
        self.issues.append(Issue(step.number, step.actor, step.action, problem))
        step.lines.append(f"       !! {problem.splitlines()[0]}")

    def _on_message(self, message: FakeMessage) -> None:
        step = _current_step.get()
        if step is not None and message not in step.messages:
            step.messages.append(message)
        if message.id not in self._seen_messages:
            self._seen_messages.add(message.id)
            if message.public and step is not None and step.user_id is not None:
                self.public_posts[step.user_id] += 1
        for problem in checks.message_problems(message):
            self._problem(f"{problem}: {_summary(message)}")
        for problem in checks.organizer_commands_named(message, self.organizer_commands):
            self._problem(f"{problem}: {_summary(message)}")

    def _after_step(self, shown: Optional[FakeMessage] = None) -> None:
        step = _current_step.get()
        # Anything else the step posted or edited, like a result announcement or a follow-up.
        for message in step.messages:
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
        self.transcript.extend(step.lines)
        _current_step.set(None)

    def _checked_record_result(self, record_result):
        """db.record_result, also checking the rule that a match with a result is only
        recorded again through /admin setresult's confirmation, even with the same winner
        (that would announce the result twice). It's checked as the write happens, so it
        can't be blamed on another step running at the same time."""

        def checked(conn, tournament_id, match_id, winner_team_id, recorded_by):
            already_recorded = match_id in db.get_results(conn, tournament_id)
            step = _current_step.get()
            confirmed = step is not None and step.action.startswith("clicks [Confirm]")
            if already_recorded and not confirmed:
                self._problem(f"match {match_id} was recorded again without /admin setresult's confirmation")
            return record_result(conn, tournament_id, match_id, winner_team_id, recorded_by)

        return checked


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
