"""Just enough of Discord for the cogs to run: users, a channel, messages and interactions.

Everything the bot sends is recorded as a FakeMessage, so checks can read what each actor
saw. Behaviour that Discord enforces and the cogs rely on is copied here: an interaction
answers once, follow-ups need an answer first, and private messages are only visible to
the person they were sent to.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

import discord

_ids = itertools.count(10_000)

# Marks an edit() argument that wasn't passed, since None is a real value ("remove the view").
_UNSET: Any = object()


@dataclass
class FakePermissions:
    manage_guild: bool = False


@dataclass(eq=False)
class FakeUser:
    """A server member. `dms` collects anything the bot sends them directly."""

    id: int
    display_name: str
    guild_permissions: FakePermissions = field(default_factory=FakePermissions)
    bot: bool = False
    dms: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.display_name

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def send(self, content: Optional[str] = None, **kwargs) -> None:
        self.dms.append(content)


@dataclass(eq=False)
class FakeMessage:
    id: int
    channel: FakeChannel
    content: Optional[str]
    embeds: list
    view: Optional[discord.ui.View]
    # Who can see it: None for everyone, or the one user a private reply went to.
    visible_to: Optional[int] = None
    deleted: bool = False
    edits: int = 0

    @property
    def public(self) -> bool:
        return self.visible_to is None

    @property
    def embed(self) -> Optional[discord.Embed]:
        return self.embeds[0] if self.embeds else None

    async def edit(self, *, content: Any = _UNSET, embed: Any = _UNSET, embeds: Any = _UNSET, view: Any = _UNSET) -> FakeMessage:
        await self.channel.network()
        if self.deleted:
            raise _not_found("Unknown Message")
        if content is not _UNSET:
            self.content = content
        if embed is not _UNSET:
            self.embeds = [embed] if embed is not None else []
        if embeds is not _UNSET:
            self.embeds = list(embeds)
        if view is not _UNSET:
            self.view = view
        self.edits += 1
        self.channel.on_change(self)
        return self

    async def delete(self) -> None:
        await self.channel.network()
        if self.deleted:
            raise _not_found("Unknown Message")
        self.deleted = True

    def text(self) -> str:
        """Everything a reader sees: the content and every part of every embed."""
        parts = [self.content or ""]
        for embed in self.embeds:
            parts += [embed.title or "", embed.description or "", embed.footer.text or ""]
            for embed_field in embed.fields:
                parts += [embed_field.name or "", embed_field.value or ""]
        return "\n".join(part for part in parts if part)


class FakeChannel:
    def __init__(self, on_change, network) -> None:
        self.id = next(_ids)
        self.messages: list[FakeMessage] = []
        # Called with every message sent or edited, so checks see each one as it happens.
        self.on_change = on_change
        # Awaited before every call to Discord, standing in for the wait on its reply.
        self.network = network

    def post(self, content, embeds, view, visible_to) -> FakeMessage:
        message = FakeMessage(next(_ids), self, content, list(embeds), view, visible_to)
        self.messages.append(message)
        self.on_change(message)
        return message

    async def send(self, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None,
                   view: Optional[discord.ui.View] = None) -> FakeMessage:
        await self.network()
        return self.post(content, [embed] if embed else [], view, None)

    def get_partial_message(self, message_id: int) -> FakeMessage:
        message = next((m for m in self.messages if m.id == message_id), None)
        if message is None:
            raise _not_found("Unknown Message")
        return message


class FakeResponse:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self.kind: Optional[str] = None  # how it was answered: "message", "edit", "modal" or "defer"
        self.modal: Optional[discord.ui.Modal] = None

    def is_done(self) -> bool:
        return self.kind is not None

    def _answer(self, kind: str) -> None:
        if self.kind is not None:
            raise discord.InteractionResponded(self._interaction)
        self.kind = kind

    async def send_message(self, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None,
                           view: Optional[discord.ui.View] = None, ephemeral: bool = False) -> None:
        await self._interaction.channel.network()
        self._answer("message")
        interaction = self._interaction
        interaction.sent = interaction.channel.post(
            content, [embed] if embed else [], view, interaction.user.id if ephemeral else None
        )

    async def edit_message(self, *, content: Any = _UNSET, embed: Any = _UNSET, view: Any = _UNSET) -> None:
        await self._interaction.channel.network()
        self._answer("edit")
        await self._interaction.message.edit(content=content, embed=embed, view=view)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        await self._interaction.channel.network()
        self._answer("modal")
        self.modal = modal

    async def defer(self, **kwargs) -> None:
        await self._interaction.channel.network()
        self._answer("defer")


class FakeFollowup:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction

    async def send(self, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None,
                   view: Optional[discord.ui.View] = None, ephemeral: bool = False) -> FakeMessage:
        interaction = self._interaction
        await interaction.channel.network()
        if not interaction.response.is_done():
            raise _not_found("Unknown Webhook")  # Discord has no follow-up channel until the first answer
        return interaction.channel.post(content, [embed] if embed else [], view,
                                        interaction.user.id if ephemeral else None)


class FakeAttachment:
    """A file attached to a command, like the CSV for /tournament create."""

    def __init__(self, data: bytes, filename: str = "bracket.csv") -> None:
        self.data = data
        self.filename = filename

    def __str__(self) -> str:
        return self.filename

    async def read(self) -> bytes:
        return self.data


class FakeNamespace:
    """The options already filled in, as autocomplete reads them (interaction.namespace.tournament)."""

    def __init__(self, options: dict) -> None:
        self.__dict__.update(options)

    def __getattr__(self, name: str) -> None:
        return None


class FakeInteraction:
    def __init__(self, *, bot: FakeBot, user: FakeUser, channel: FakeChannel, guild_id: int,
                 kind: discord.InteractionType, command=None, message: Optional[FakeMessage] = None,
                 data: Optional[dict] = None, options: Optional[dict] = None) -> None:
        self.id = next(_ids)
        self.client = bot
        self.user = user
        self.channel = channel
        self.channel_id = channel.id
        self.guild_id = guild_id
        self.type = kind
        self.command = command
        self.message = message
        self.data = data or {}
        self.namespace = FakeNamespace(options or {})
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.sent: Optional[FakeMessage] = None

    @property
    def permissions(self) -> FakePermissions:
        return self.user.guild_permissions

    async def original_response(self) -> FakeMessage:
        await self.channel.network()
        if self.sent is None:
            raise _not_found("Unknown Message")
        return self.sent


class FakeBot:
    def __init__(self) -> None:
        self.user = FakeUser(next(_ids), "WinningTicket", bot=True)
        self.emojis: list = []
        self.channels: dict[int, FakeChannel] = {}
        self.users: dict[int, FakeUser] = {}

    def get_channel(self, channel_id: int) -> Optional[FakeChannel]:
        return self.channels.get(channel_id)

    def get_user(self, user_id: int) -> Optional[FakeUser]:
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeUser:
        for channel in self.channels.values():
            await channel.network()  # an HTTP call like any other
            break
        if user_id not in self.users:
            raise _not_found("Unknown User")
        return self.users[user_id]

    async def wait_until_ready(self) -> None:
        pass


def _not_found(text: str) -> discord.NotFound:
    response = type("FakeHTTPResponse", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(response, text)
