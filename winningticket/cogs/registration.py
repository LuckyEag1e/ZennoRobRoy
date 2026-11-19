from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import lobbies
import lobby_db
import lobby_flow
from bracket import BracketError
from common import (
    BotView,
    bot_plays_for_testing,
    edit_signups_message,
    load_bracket_embed,
    refuse_taken_name,
    registration_name_autocomplete,
    signup_name_autocomplete,
    requires_manage_server,
    resolve_tournament,
    signups_message,
    team_name_autocomplete,
    tournament_name_autocomplete,
)
from embeds import EMBED_DESCRIPTION_LIMIT, mention
from labels import CONSENT_NOTICE, OPTED_OUT_NOTE, draw_description, entrant_lines, join_within, name_held_message
from lobby_flow import split_label
from lobby_posting import CaptainChoice, event_view, leave_event, refresh_signups, start_signup, sync_posts
from posting import MATCH_SLOTS_HELP, match_slots_problem
from tournament_builder import (
    MAX_CLOSES_IN_HOURS,
    MIN_CLOSES_IN_HOURS,
    RegistrationClosedError,
    close_lobby_registration,
    close_overdue_registration,
    close_registration,
    lobby_split_options,
)

log = logging.getLogger("winningticket")

# Shown as-is in the option once picked, so the names stay short; the option's description
# explains them.
SEEDING_CHOICES = [app_commands.Choice(name=mode, value=mode) for mode in db.SEEDING_MODES]
SEEDING_OPTION_HELP = "standard (default): top seeds play the lowest. slide: top seeds play mid-ranked. random: no seeds"
SPREAD_NAMES = {"tier": "by skill tier", "balanced": "balanced", "random": "random"}
SPREAD_CHOICES = [app_commands.Choice(name=name, value=value) for value, name in SPREAD_NAMES.items()]
SPREAD_OPTION_HELP = ("5v5: tier puts the strongest together, balanced (default) mixes every lobby, random draws them; "
                      "each lobby's teams are balanced either way")
CAPTAIN_CHOICES = [app_commands.Choice(name=name, value=value) for value, name in lobby_flow.CAPTAIN_CHOICES.items()]
# Discord's limit on options in one select menu.
MAX_SPLIT_OPTIONS = 25


class AddCaptainChoice(BotView):
    """Asks the organizer adding someone to a 5v5 event whether they'll captain, then adds
    them. A choice that can't stand is refused and the menu stays, as it does for /register."""

    def __init__(self, cog: RegistrationCog, tournament: str, user: discord.Member | None, name: str | None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.tournament = tournament
        self.user = user
        self.name = name
        self.add_item(CaptainChoice())

    async def choose(self, interaction: discord.Interaction, pref: str) -> None:
        await self.cog.add_entrant(interaction, self.tournament, self.user, self.name, pref, menu=self)


class LobbyClosePreview(BotView):
    """What closing a 5v5 event's sign-ups would draw, shown privately to the organizer,
    who can choose another split or spread before creating the lobbies."""

    def __init__(self, cog: RegistrationCog, tournament: sqlite3.Row, proposed: lobbies.Split,
                 options: list[lobbies.Split]) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.tournament_id = tournament["id"]
        self.options = options[:MAX_SPLIT_OPTIONS]
        self.split = proposed
        self.spread = tournament["lobby_spread"] or "balanced"
        split_menu = discord.ui.Select(placeholder="Lobbies", options=[
            discord.SelectOption(label=split_label(option), value=str(index), default=option == proposed)
            for index, option in enumerate(self.options)])
        split_menu.callback = self._pick_split
        spread_menu = discord.ui.Select(placeholder="Spread", options=[
            discord.SelectOption(label=f"Spread {name}", value=value, default=value == self.spread)
            for value, name in SPREAD_NAMES.items()])
        spread_menu.callback = self._pick_spread
        create = discord.ui.Button(label="Create Lobbies", style=discord.ButtonStyle.success)
        create.callback = self._create
        for item in (split_menu, spread_menu, create):
            self.add_item(item)
        self.split_menu, self.spread_menu = split_menu, spread_menu

    def describe(self, tournament: sqlite3.Row) -> str:
        text = (f"Closing sign-ups for **{tournament['name']}** draws **{split_label(self.split)}**, spread "
                f"{SPREAD_NAMES[self.spread]}. Pick another split or spread below, then press **Create Lobbies**. "
                f"Teams are balanced and captains picked automatically, and everyone is pinged with their lobby or "
                f"their place on the waitlist.")
        starts = tournament["starts_at"]
        if starts is not None and db.from_db_time(starts) - datetime.now(timezone.utc) < timedelta(days=1):
            text += ("\nThe event is less than a day away. Closing earlier lets the waitlist know where they stand "
                     "and plan ahead.")
        return text

    async def _pick_split(self, interaction: discord.Interaction) -> None:
        self.split = self.options[int(interaction.data["values"][0])]
        await self._redraw(interaction)

    async def _pick_spread(self, interaction: discord.Interaction) -> None:
        self.spread = interaction.data["values"][0]
        await self._redraw(interaction)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        for option in self.split_menu.options:
            option.default = self.options[int(option.value)] == self.split
        for option in self.spread_menu.options:
            option.default = option.value == self.spread
        conn = db.get_connection()
        try:
            tournament = db.get_tournament(conn, self.tournament_id)
        finally:
            conn.close()
        await interaction.response.edit_message(content=self.describe(tournament), view=self)

    async def _create(self, interaction: discord.Interaction) -> None:
        conn = db.get_connection()
        try:
            try:
                close_lobby_registration(conn, self.tournament_id, self.split, self.spread)
            except RegistrationClosedError:
                await interaction.response.edit_message(content="Sign-ups for that event have already closed.", view=None)
                return
            except lobbies.LobbyError as e:
                # Sign-ups may have changed since the preview was opened.
                await interaction.response.edit_message(content=f"Couldn't create the lobbies: {e} Close again to "
                                                                f"see the splits that fit now.", view=None)
                return
            tournament = db.get_tournament(conn, self.tournament_id)
            await interaction.response.edit_message(
                content=(f"✅ Created the lobbies for **{tournament['name']}**. When it's nearly time to play, press "
                         f"**Start Check-In** on the event's post (or run `/lobby startcheckin`)."), view=None)
            await refresh_signups(interaction.client, conn, self.tournament_id)
            await sync_posts(interaction.client, conn, self.tournament_id)
        finally:
            conn.close()


def _close_and_describe(conn: sqlite3.Connection, tournament: sqlite3.Row, seeding_mode: Optional[str] = None,
                        close: Callable[..., int] = close_registration,
                        ) -> tuple[bool, str, discord.Embed | None]:
    """Close registration with `close` and return (closed, announcement, bracket). If too
    few signed up, the tournament stays open and the announcement says why. The
    announcement is for everyone, so it doesn't mention organizer commands."""
    name = tournament["name"]
    seeding_mode = seeding_mode or tournament["seeding_mode"]
    try:
        count = close(conn, tournament["id"], seeding_mode=seeding_mode)
    except BracketError as e:
        return False, f"Registration for **{name}** couldn't close: {e}", None
    content = f"Registration for **{name}** is closed, and {count} entrants were {draw_description(seeding_mode)}."
    try:
        embed = load_bracket_embed(conn, db.get_tournament(conn, tournament["id"]))
    except Exception:
        # The close is already saved, so still announce it; /tournament bracket shows the draw.
        log.exception("Couldn't draw the bracket for tournament %s", tournament["id"])
        embed = None
    return True, content, embed


async def _require_registration(interaction: discord.Interaction, tournament: sqlite3.Row) -> bool:
    """True if the tournament is taking sign-ups; otherwise says so and returns False."""
    if tournament["status"] == "registration":
        return True
    await interaction.response.send_message(
        f"**{tournament['name']}** isn't taking sign-ups (status: {tournament['status']}).", ephemeral=True
    )
    return False


class ConsentView(BotView):
    """Agree or Opt Out, shown privately to a member signing up who hasn't agreed yet."""

    def __init__(self, cog: RegistrationCog, tournament_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.tournament_id = tournament_id

    @discord.ui.button(label="Agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._sign_up(interaction, linked=True)

    @discord.ui.button(label="Opt Out", style=discord.ButtonStyle.secondary)
    async def opt_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._sign_up(interaction, linked=False)

    async def _sign_up(self, interaction: discord.Interaction, linked: bool) -> None:
        user = interaction.user
        conn = db.get_connection()
        try:
            # The notice can sit open while sign-ups close or a double-click signs them up.
            tournament = db.get_tournament(conn, self.tournament_id)
            if tournament is None or tournament["status"] != "registration":
                await interaction.response.edit_message(content="Sign-ups for that tournament have closed.", view=None)
                return
            name = tournament["name"]
            if db.get_entrant_team_id(conn, self.tournament_id, user.id) is not None:
                await interaction.response.edit_message(content=f"You're already signed up for **{name}**.", view=None)
                return
            if linked:
                db.record_agreement(conn, interaction.guild_id, user.id)
                if db.register_entrant(conn, self.tournament_id, user.display_name, user.id) is None:
                    await interaction.response.edit_message(content=name_held_message(name, user.display_name), view=None)
                    return
                content = (f"✅ You're signed up for **{name}** ({db.entrant_count(conn, self.tournament_id)} entered "
                           f"so far). If you change your mind, `/optout` unlinks your data.")
            else:
                db.opt_out(conn, interaction.guild_id, user.id)  # unlinks entries from before consent was asked
                if db.register_entrant(conn, self.tournament_id, user.display_name, None) is None:
                    await interaction.response.edit_message(
                        content=f"Someone is already signed up for **{name}** as {user.display_name}. If that's you, "
                                f"you're in. If not, ask an organizer to add you under another name.",
                        view=None,
                    )
                    return
                content = (f"✅ You're signed up for **{name}** as {user.display_name} "
                           f"({db.entrant_count(conn, self.tournament_id)} entered so far). {OPTED_OUT_NOTE}")
            await interaction.response.edit_message(content=content, view=None)
            await self.cog._refresh_signups_message(conn, self.tournament_id)
        finally:
            conn.close()


class RegistrationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="registration", description="Run a tournament that players sign up for", guild_only=True
    )

    async def cog_load(self) -> None:
        self.close_due_registrations.start()

    async def cog_unload(self) -> None:
        self.close_due_registrations.cancel()

    # Often enough that sign-ups close within seconds of the time players were shown.
    @tasks.loop(seconds=5)
    async def close_due_registrations(self) -> None:
        # An exception escaping a tasks.loop stops it until the bot restarts, so every
        # error is logged here, and one tournament failing doesn't hold up the others.
        try:
            conn = db.get_connection()
            try:
                for tournament in db.get_tournaments_due_to_close(conn):
                    try:
                        await self._close_when_due(conn, tournament)
                    except Exception:
                        log.exception("Couldn't close registration for tournament %s", tournament["id"])
            finally:
                conn.close()
        except Exception:
            log.exception("Couldn't check for registrations to close")

    async def _close_when_due(self, conn: sqlite3.Connection, tournament: sqlite3.Row) -> None:
        if tournament["format"] == "lobbies":
            await self._close_lobbies_when_due(conn, tournament)
            return
        try:
            closed, content, embed = _close_and_describe(conn, tournament, close=close_overdue_registration)
        except RegistrationClosedError:
            return  # an organizer closed it by hand since the list of due tournaments was fetched
        if not closed:
            content += " Sign-ups stay open until an organizer closes them."
        await self._refresh_signups_message(conn, tournament["id"])
        await self._announce(tournament["channel_id"], content, embed)

    async def _close_lobbies_when_due(self, conn: sqlite3.Connection, tournament: sqlite3.Row) -> None:
        """Draw a 5v5 event's lobbies at its sign-up deadline with the proposed split. If
        there aren't enough players, the deadline is dropped and sign-ups stay open."""
        try:
            close_lobby_registration(conn, tournament["id"])
        except RegistrationClosedError:
            return
        except lobbies.LobbyError as e:
            db.clear_registration_deadline(conn, tournament["id"])
            await self._announce(tournament["channel_id"], f"Sign-ups for **{tournament['name']}** couldn't close: {e} "
                                                           f"Sign-ups stay open until an organizer closes them.", None)
            return
        await refresh_signups(self.bot, conn, tournament["id"])
        await sync_posts(self.bot, conn, tournament["id"])

    async def _refresh_signups_message(self, conn: sqlite3.Connection, tournament_id: int) -> None:
        tournament = db.get_tournament(conn, tournament_id)
        await edit_signups_message(self.bot, tournament, signups_message(tournament, db.entrant_count(conn, tournament_id)))

    @close_due_registrations.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _announce(self, channel_id: int | None, content: str, embed: discord.Embed | None) -> None:
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None:
            log.warning("Can't find channel %s to post: %s", channel_id, content)
            return
        try:
            await channel.send(content, embed=embed)
        except discord.HTTPException:
            log.exception("Couldn't post to channel %s", channel_id)

    @group.command(name="open", description="Create a bracket tournament that players sign up for with /register")
    @app_commands.describe(
        name="Tournament name",
        closes_in_hours="Close sign-ups automatically after this many hours (leave blank to close by hand)",
        seeding=SEEDING_OPTION_HELP,
        match_slots=MATCH_SLOTS_HELP,
    )
    @app_commands.choices(seeding=SEEDING_CHOICES)
    @requires_manage_server
    async def open_signups(self, interaction: discord.Interaction, name: str,
                   closes_in_hours: app_commands.Range[float, MIN_CLOSES_IN_HOURS, MAX_CLOSES_IN_HOURS] | None = None,
                   seeding: str = "standard", match_slots: app_commands.Range[int, 1] | None = None) -> None:
        await self._open(interaction, name, closes_in_hours, seeding=seeding, match_slots=match_slots)

    @group.command(name="open5v5", description="Create a 5v5 event, played on the drafter site, that players sign up for")
    @app_commands.describe(
        name="Event name",
        starts_in_hours="When play starts, in hours from now (shown to players)",
        closes_in_hours="Draw the lobbies automatically after this many hours (leave blank to close by hand)",
        spread=SPREAD_OPTION_HELP,
    )
    @app_commands.choices(spread=SPREAD_CHOICES)
    @requires_manage_server
    async def open_5v5(self, interaction: discord.Interaction, name: str,
                       starts_in_hours: app_commands.Range[float, MIN_CLOSES_IN_HOURS, MAX_CLOSES_IN_HOURS],
                       closes_in_hours: app_commands.Range[float, MIN_CLOSES_IN_HOURS, MAX_CLOSES_IN_HOURS] | None = None,
                       spread: str = "balanced") -> None:
        await self._open(interaction, name, closes_in_hours, starts_in_hours=starts_in_hours, spread=spread)

    async def _open(self, interaction: discord.Interaction, name: str, closes_in_hours: float | None,
                    seeding: str = "standard", match_slots: int | None = None,
                    starts_in_hours: float | None = None, spread: str | None = None) -> None:
        """Open sign-ups: a 5v5 event when `starts_in_hours` is given, a bracket otherwise."""
        now = datetime.now(timezone.utc)
        closes_at = now + timedelta(hours=closes_in_hours) if closes_in_hours is not None else None
        five_v_five = starts_in_hours is not None
        format = "lobbies" if five_v_five else "single_elim"
        starts_at = now + timedelta(hours=starts_in_hours) if five_v_five else None
        if five_v_five and closes_at is not None and closes_at >= starts_at:
            await interaction.response.send_message(
                "Sign-ups have to close before play starts, ideally a day before, so the waitlist knows where it "
                "stands.", ephemeral=True)
            return
        conn = db.get_connection()
        try:
            if await refuse_taken_name(interaction, conn, name):
                return
            problem = match_slots_problem(self.bot, interaction.guild_id, conn, match_slots) if match_slots else None
            if problem is not None:
                await interaction.response.send_message(problem, ephemeral=True)
                return
            tournament_id = db.open_registration(
                conn, interaction.guild_id, name, interaction.user.id, interaction.channel_id,
                db.to_db_time(closes_at) if closes_at is not None else None, seeding, format,
                db.to_db_time(starts_at) if starts_at is not None else None, spread,
            )
            db.set_match_slots(conn, tournament_id, match_slots)
            announcement = signups_message(db.get_tournament(conn, tournament_id), 0)
            if five_v_five:
                await interaction.response.send_message(announcement,
                                                        view=event_view(tournament_id, ["register", "unregister"]))
            else:
                await interaction.response.send_message(announcement)
            message = await interaction.original_response()
            db.set_announcement_message(conn, tournament_id, message.id)
            if five_v_five:
                await interaction.followup.send(
                    f"When sign-ups are in, run `/registration close name:{name}` to draw the lobbies, ideally a day "
                    f"before play starts so the waitlist knows where it stands.", ephemeral=True)
        finally:
            conn.close()

    @group.command(name="close", description="Close sign-ups and draw the bracket or the 5v5 lobbies")
    @app_commands.describe(name="Tournament name",
                           seeding="Brackets only: override the seeding chosen at open (standard, slide or random)")
    @app_commands.autocomplete(name=registration_name_autocomplete)
    @app_commands.choices(seeding=SEEDING_CHOICES)
    @requires_manage_server
    async def close_signups(self, interaction: discord.Interaction, name: str, seeding: str | None = None) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name, needs_bracket=False, formats=db.FORMATS)
            if tournament is None or not await _require_registration(interaction, tournament):
                return
            if tournament["format"] == "lobbies" and seeding is not None:
                await interaction.response.send_message(
                    "5v5 events aren't seeded: leave out `seeding`. The preview lets you pick how players are spread "
                    "across the lobbies.", ephemeral=True)
                return
            if tournament["format"] == "lobbies":
                proposed, options = lobby_split_options(conn, tournament["id"])
                if proposed is None:
                    await interaction.response.send_message(
                        f"**{name}** can't be drawn yet: a lobby needs two players who can captain (with a linked "
                        f"account).", ephemeral=True)
                    return
                preview = LobbyClosePreview(self, tournament, proposed, options)
                await interaction.response.send_message(preview.describe(tournament), view=preview, ephemeral=True)
                return
            try:
                closed, content, embed = _close_and_describe(conn, tournament, seeding)
            except RegistrationClosedError:
                await interaction.response.send_message(f"Sign-ups for **{name}** have already closed.", ephemeral=True)
                return
            if not closed:
                await interaction.response.send_message(content, ephemeral=True)
                return
            await interaction.response.send_message(content, embed=embed)
            await interaction.followup.send(
                f"Run `/tournament open name:{name}` when you're ready for predictions to begin.", ephemeral=True
            )
            await self._refresh_signups_message(conn, tournament["id"])
        finally:
            conn.close()

    @group.command(name="entrants", description="See who has signed up for a tournament")
    @app_commands.describe(name="Tournament name")
    @app_commands.autocomplete(name=tournament_name_autocomplete)
    async def entrants(self, interaction: discord.Interaction, name: str) -> None:
        conn = db.get_connection()
        try:
            tournament = await resolve_tournament(interaction, conn, name, needs_bracket=False, formats=db.FORMATS)
            if tournament is None:
                return
            entrants = db.get_entrants(conn, tournament["id"])
            lines = (lobby_flow.entrant_lines(conn, tournament, mention) if tournament["format"] == "lobbies"
                     else entrant_lines(entrants, mention))
        finally:
            conn.close()
        embed = discord.Embed(title=f"Entrants: {name}")
        if not entrants:
            embed.description = "Nobody has signed up yet."
        else:
            embed.description = join_within(lines, EMBED_DESCRIPTION_LIMIT)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="add", description="Sign up a member, or an entrant with no Discord account")
    @app_commands.describe(
        tournament="Tournament name",
        user="Member to sign up",
        name="Entrant name (defaults to the member's server name; needed when no member is given)",
        captain="5v5 events: whether they'll captain (only members with a linked account can)",
    )
    @app_commands.autocomplete(tournament=signup_name_autocomplete)
    @app_commands.choices(captain=CAPTAIN_CHOICES)
    @requires_manage_server
    async def add(self, interaction: discord.Interaction, tournament: str,
                  user: discord.Member | None = None, name: str | None = None, captain: str | None = None) -> None:
        if user is None and name is None:
            await interaction.response.send_message(
                "Give a member to sign up, or a name for an entrant with no Discord account.", ephemeral=True
            )
            return
        await self.add_entrant(interaction, tournament, user, name, captain)

    async def add_entrant(self, interaction: discord.Interaction, tournament: str, user: discord.Member | None,
                          name: str | None, captain: str | None, menu: Optional[AddCaptainChoice] = None) -> None:
        """Sign up a member or a name-only entrant. A 5v5 event needs their captain choice,
        so without one the organizer is asked for it (`menu`, whose pick comes back here)."""

        async def reply(content: str, keep_menu: bool = False) -> None:
            if menu is None:
                await interaction.response.send_message(content, view=menu, ephemeral=True)
            else:
                await interaction.response.edit_message(content=content, view=menu if keep_menu else None)

        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False, formats=db.FORMATS)
            if tournament_row is None:
                return
            late = tournament_row["format"] == "lobbies" and lobby_flow.takes_late_signups(tournament_row)
            if not late and not await _require_registration(interaction, tournament_row):
                return
            if user is not None and db.get_entrant_team_id(conn, tournament_row["id"], user.id) is not None:
                await reply(f"{user.display_name} is already signed up for **{tournament}**.")
                return
            # A member's entry is only linked to them once they've agreed to it themselves. The
            # bot can't agree to anything, so it's only linked while it plays for testing.
            playing_bot = user is not None and user.id == interaction.client.user.id and bot_plays_for_testing()
            linked = user is not None and (playing_bot or db.has_agreed(conn, interaction.guild_id, user.id))
            five_v_five = tournament_row["format"] == "lobbies"
            if five_v_five and captain is None:
                question = f"Will **{name or user.display_name}** captain? {lobby_flow.ADD_CAPTAIN_QUESTION}"
                await interaction.response.send_message(
                    question, view=AddCaptainChoice(self, tournament, user, name), ephemeral=True)
                return
            problem = lobby_flow.captain_problem(captain, linked, by_organizer=True) if five_v_five else None
            if problem is not None:
                await reply(problem, keep_menu=True)
                return
            entrant = db.register_entrant(
                conn, tournament_row["id"], name or user.display_name, user.id if linked else None
            )
            if entrant is None:
                link_hint = ", or link them to it with `/admin linkplayer`" if linked else ""
                await reply(f"**{tournament}** already has an entrant named '{name or user.display_name}'. "
                            f"Pick another name{link_hint}.")
                return
            team_id, entrant_name = entrant
            note = (" They haven't agreed to link their data to their account, so they're entered by name only."
                    if user is not None and not linked else "")
            if five_v_five:
                lobby_db.set_captain_pref(conn, tournament_row["id"], team_id, captain)
                note += f" Captain choice: {lobby_flow.CAPTAIN_CHOICES[captain]}."
            if late:
                lobby_db.place_late_signup(conn, tournament_row["id"], team_id)
                note = f" {lobby_flow.late_signup_line(conn, tournament_row['id'], team_id, False)}{note}"
            who = f"{entrant_name} ({user.mention})" if linked else entrant_name
            await reply(f"✅ Signed up {who} for **{tournament}** ({db.entrant_count(conn, tournament_row['id'])} "
                        f"entered so far).{note}")
            await self._refresh_signups_message(conn, tournament_row["id"])
            if late:
                await sync_posts(interaction.client, conn, tournament_row["id"])
        finally:
            conn.close()

    @group.command(name="remove", description="Withdraw an entrant from a tournament")
    @app_commands.describe(tournament="Tournament name", entrant="Entrant to withdraw")
    @app_commands.autocomplete(tournament=registration_name_autocomplete, entrant=team_name_autocomplete)
    @requires_manage_server
    async def remove(self, interaction: discord.Interaction, tournament: str, entrant: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False, formats=db.FORMATS)
            if tournament_row is None or not await _require_registration(interaction, tournament_row):
                return
            team = db.get_team_by_name(conn, tournament_row["id"], entrant)
            if team is None:
                await interaction.response.send_message(
                    f"No entrant named '{entrant}' in **{tournament}**.", ephemeral=True
                )
                return
            db.delete_team(conn, team["id"])
            await interaction.response.send_message(f"Withdrew {entrant} from **{tournament}**.", ephemeral=True)
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()

    @app_commands.command(name="register", description="Sign up for a tournament")
    @app_commands.describe(tournament="Tournament name",
                           captain="5v5 events: whether you'll captain (asked if you leave it out)")
    @app_commands.autocomplete(tournament=signup_name_autocomplete)
    @app_commands.choices(captain=CAPTAIN_CHOICES)
    @app_commands.guild_only()
    async def register(self, interaction: discord.Interaction, tournament: str, captain: str | None = None) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False, formats=db.FORMATS)
            if tournament_row is None:
                return
            if tournament_row["format"] == "lobbies":
                await start_signup(interaction, conn, tournament_row, captain)
                return
            if not await _require_registration(interaction, tournament_row):
                return
            if db.get_entrant_team_id(conn, tournament_row["id"], interaction.user.id) is not None:
                await interaction.response.send_message(
                    f"You're already signed up for **{tournament}**.", ephemeral=True
                )
                return
            if not db.has_agreed(conn, interaction.guild_id, interaction.user.id):
                await interaction.response.send_message(
                    CONSENT_NOTICE, view=ConsentView(self, tournament_row["id"]), ephemeral=True
                )
                return
            if db.register_entrant(conn, tournament_row["id"], interaction.user.display_name, interaction.user.id) is None:
                await interaction.response.send_message(
                    name_held_message(tournament, interaction.user.display_name), ephemeral=True
                )
                return
            await interaction.response.send_message(
                f"✅ You're signed up for **{tournament}** ({db.entrant_count(conn, tournament_row['id'])} entered so far).",
                ephemeral=True,
            )
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()

    @app_commands.command(name="unregister", description="Withdraw from a tournament you signed up for")
    @app_commands.describe(tournament="Tournament name")
    @app_commands.autocomplete(tournament=signup_name_autocomplete)
    @app_commands.guild_only()
    async def unregister(self, interaction: discord.Interaction, tournament: str) -> None:
        conn = db.get_connection()
        try:
            tournament_row = await resolve_tournament(interaction, conn, tournament, needs_bracket=False, formats=db.FORMATS)
            if tournament_row is None:
                return
            if tournament_row["format"] == "lobbies" and tournament_row["status"] != "registration":
                await leave_event(interaction, conn, tournament_row)
                return
            if not await _require_registration(interaction, tournament_row):
                return
            if not db.unregister_entrant(conn, tournament_row["id"], interaction.user.id):
                await interaction.response.send_message(
                    f"You aren't signed up for **{tournament}** under your account. If you opted out when "
                    f"signing up, ask an organizer to withdraw you.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(f"You withdrew from **{tournament}**.", ephemeral=True)
            await self._refresh_signups_message(conn, tournament_row["id"])
        finally:
            conn.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RegistrationCog(bot))
