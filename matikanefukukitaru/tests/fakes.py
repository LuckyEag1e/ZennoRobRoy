"""Just enough of Discord to call a cog's slash commands in a test, without a token or server.

Each fake copies a rule Discord enforces that a cog could break: an interaction is
answered exactly once. Add to these as commands need more of Discord, rather than
reaching for mocks in individual tests.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

_ids = itertools.count(10_000)


def new_id() -> int:
    return next(_ids)


@dataclass
class FakePermissions:
    administrator: bool = False


@dataclass
class FakeRole:
    id: int = field(default_factory=new_id)


@dataclass
class FakeUser:
    display_name: str = "Trainer"
    id: int = field(default_factory=new_id)
    roles: list = field(default_factory=list)
    guild_permissions: FakePermissions = field(default_factory=FakePermissions)

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass
class FakeGuild:
    id: int = field(default_factory=new_id)
    owner_id: int = 0


@dataclass
class Reply:
    content: Optional[str]
    embed: Optional[object]
    ephemeral: bool
    view: Optional[object] = None
    # True when the reply replaced the message the clicked button was on (edit_message).
    edited: bool = False


class InteractionAlreadyAnswered(Exception):
    """Discord rejects a second response to one interaction (40060)."""


class FakeResponse:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction

    def is_done(self) -> bool:
        return self._interaction.reply is not None

    async def send_message(self, content: Optional[str] = None, *, embed=None, ephemeral: bool = False,
                           view=None, **_) -> None:
        if self.is_done():
            raise InteractionAlreadyAnswered()
        self._interaction.reply = Reply(content=content, embed=embed, ephemeral=ephemeral, view=view)

    async def edit_message(self, *, content: Optional[str] = None, embed=None, view=None, **_) -> None:
        if self.is_done():
            raise InteractionAlreadyAnswered()
        self._interaction.reply = Reply(content=content, embed=embed, ephemeral=False, view=view, edited=True)


@dataclass
class FakeBot:
    """Stands in for commands.Bot where a cog only reads from it."""

    latency: float = 0.05
    cogs: dict = field(default_factory=dict)

    def get_cog(self, name: str):
        return self.cogs.get(name)


class FakeInteraction:
    def __init__(self, user: Optional[FakeUser] = None, guild: Optional[FakeGuild] = None,
                 client: Optional[FakeBot] = None) -> None:
        self.user = user or FakeUser()
        self.guild = guild or FakeGuild()
        self.client = client or FakeBot()
        self.reply: Optional[Reply] = None
        self.response = FakeResponse(self)

    @property
    def guild_id(self) -> int:
        return self.guild.id


async def invoke(cog, command_name: str, interaction: FakeInteraction, **options):
    """Run one of `cog`'s slash commands the way discord.py would, then return the reply."""
    command = next(c for c in cog.get_app_commands() if c.name == command_name)
    await command.callback(cog, interaction, **options)
    assert interaction.reply is not None, f"/{command_name} never answered the interaction"
    return interaction.reply
