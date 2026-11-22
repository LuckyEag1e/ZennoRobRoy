"""The SQLite side of 5v5 events: captain choices, lobbies and their teams, the waitlist,
check-in, room codes, finishing, captains' points and results, and the event's public posts. Like
db.py, whose schema and connections it shares, it has no discord.py imports."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import db
import lobbies
from contribution import PlayedLobby

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
SIDES = ("A", "B")


# -- sign-ups -------------------------------------------------------------------

@dataclass(frozen=True)
class Signup:
    team_id: int
    name: str
    user_id: Optional[int]  # None for an entrant with no account behind them
    captain_pref: str
    checked_in: bool
    withdrawn: Optional[str]

    @property
    def can_captain(self) -> bool:
        """Captains give the bot the room code and report the points, which takes an account."""
        return self.user_id is not None and lobbies.is_candidate(self.captain_pref)


def get_signups(conn: sqlite3.Connection, tournament_id: int) -> list[Signup]:
    """Everyone who signed up, in sign-up order. Entrants with no account count as checked
    in, since they can't press anything; an organizer vouches for them."""
    rows = conn.execute(
        """SELECT t.id, t.name, t.withdrawn, tm.user_id, COALESCE(s.captain_pref, 'no') AS captain_pref,
                  s.checked_in_at
           FROM teams t LEFT JOIN team_members tm ON tm.team_id = t.id
           LEFT JOIN lobby_signups s ON s.team_id = t.id
           WHERE t.tournament_id = ? ORDER BY t.id""",
        (tournament_id,),
    ).fetchall()
    return [Signup(r["id"], r["name"], r["user_id"], r["captain_pref"],
                   r["user_id"] is None or r["checked_in_at"] is not None, r["withdrawn"]) for r in rows]


def captain_prefs(signups: list[Signup]) -> dict[int, str]:
    """Each entrant's captain choice as lobbies.py should see it: entrants who can't
    captain (no account) count as not wanting to."""
    return {s.team_id: s.captain_pref if s.user_id is not None else "no" for s in signups}


def set_captain_pref(conn: sqlite3.Connection, tournament_id: int, team_id: int, pref: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO lobby_signups (team_id, tournament_id, captain_pref) VALUES (?, ?, ?)
               ON CONFLICT (team_id) DO UPDATE SET captain_pref = excluded.captain_pref""",
            (team_id, tournament_id, pref),
        )


def rejoin(conn: sqlite3.Connection, tournament_id: int, team_id: int, pref: str) -> Optional[Lobby]:
    """Bring back a player who left after the draw, with a new captain choice, placed like
    any late sign-up. Returns the lobby they joined, or None if they're waiting."""
    with conn:
        conn.execute("UPDATE teams SET withdrawn = NULL, withdrawn_by = NULL, withdrawn_at = NULL WHERE id = ?",
                     (team_id,))
        conn.execute("UPDATE lobby_signups SET checked_in_at = NULL WHERE team_id = ?", (team_id,))
    set_captain_pref(conn, tournament_id, team_id, pref)
    return place_late_signup(conn, tournament_id, team_id)


def place_late_signup(conn: sqlite3.Connection, tournament_id: int, team_id: int) -> Optional[Lobby]:
    """Put a sign-up from after the draw on the smallest team with room (lobbies.open_spot),
    taking the captaincy if that team has none and they can captain (such a team is filled
    first by someone who can), or else at the end of
    the waitlist. Nobody already placed moves. They count as checked in if check-in has
    started. Returns the lobby they joined, or None if they're waiting."""
    with conn:
        playing = [lobby for lobby in get_lobbies(conn, tournament_id) if get_result(conn, lobby.id) is None]
        signup = next(s for s in get_signups(conn, tournament_id) if s.team_id == team_id)
        spot = lobbies.open_spot([lobbies.TeamRoom(lobby.number, side, len(lobby.teams[side].players),
                                                   len(lobby.players), lobby.teams[side].captain is not None)
                                  for lobby in playing for side in SIDES], signup.can_captain)
        if spot is None:
            conn.execute(
                """INSERT INTO lobby_waitlist (team_id, tournament_id, position)
                   SELECT ?, ?, COALESCE(MAX(position), 0) + 1 FROM lobby_waitlist WHERE tournament_id = ?""",
                (team_id, tournament_id, tournament_id),
            )
        else:
            lobby = next(lobby for lobby in playing if lobby.number == spot.lobby_number)
            conn.execute(
                """INSERT INTO lobby_players (lobby_id, side, spot, team_id)
                   SELECT ?, ?, COALESCE(MAX(spot), -1) + 1, ? FROM lobby_players WHERE lobby_id = ? AND side = ?""",
                (lobby.id, spot.side, team_id, lobby.id, spot.side),
            )
            if lobby.teams[spot.side].captain is None and signup.can_captain:
                conn.execute("UPDATE lobby_sides SET captain_team_id = ? WHERE lobby_id = ? AND side = ?",
                             (team_id, lobby.id, spot.side))
        if db.get_tournament(conn, tournament_id)["checkin_ends_at"] is not None:
            conn.execute(f"UPDATE lobby_signups SET checked_in_at = {_NOW} WHERE team_id = ?", (team_id,))
    return find_lobby_of(conn, tournament_id, team_id)



# -- lobbies ----------------------------------------------------------------------

@dataclass
class LobbyTeam:
    side: str
    captain: Optional[int]
    players: list[int] = field(default_factory=list)  # in spot order


@dataclass
class Lobby:
    id: int
    tournament_id: int
    number: int
    room_code: Optional[str]
    host_user_id: Optional[int]
    teams: dict[str, LobbyTeam]

    @property
    def players(self) -> list[int]:
        return [player for side in SIDES for player in self.teams[side].players]

    @property
    def captains(self) -> list[int]:
        return [self.teams[side].captain for side in SIDES if self.teams[side].captain is not None]

    def side_of(self, team_id: int) -> Optional[str]:
        return next((side for side in SIDES if team_id in self.teams[side].players), None)


def _load_lobbies(conn: sqlite3.Connection, where: str, value: int) -> list[Lobby]:
    found = []
    for row in conn.execute(f"SELECT * FROM lobbies WHERE {where} = ? ORDER BY number", (value,)).fetchall():
        teams = {side["side"]: LobbyTeam(side["side"], side["captain_team_id"]) for side in conn.execute(
            "SELECT * FROM lobby_sides WHERE lobby_id = ?", (row["id"],))}
        for player in conn.execute("SELECT * FROM lobby_players WHERE lobby_id = ? ORDER BY side, spot", (row["id"],)):
            teams[player["side"]].players.append(player["team_id"])
        found.append(Lobby(row["id"], row["tournament_id"], row["number"], row["room_code"], row["host_user_id"], teams))
    return found


def get_lobbies(conn: sqlite3.Connection, tournament_id: int) -> list[Lobby]:
    return _load_lobbies(conn, "tournament_id", tournament_id)


def get_lobby(conn: sqlite3.Connection, lobby_id: int) -> Optional[Lobby]:
    found = _load_lobbies(conn, "id", lobby_id)
    return found[0] if found else None


def find_lobby_of(conn: sqlite3.Connection, tournament_id: int, team_id: int) -> Optional[Lobby]:
    return next((lobby for lobby in get_lobbies(conn, tournament_id) if team_id in lobby.players), None)


def get_waitlist(conn: sqlite3.Connection, tournament_id: int) -> list[int]:
    return [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM lobby_waitlist WHERE tournament_id = ? ORDER BY position", (tournament_id,))]


def form_lobbies(conn: sqlite3.Connection, tournament_id: int, formed: list[lobbies.Teams],
                 waitlist: list[int]) -> None:
    """Save the lobbies and the waitlist, without committing, so they land together with
    the close that drew them."""
    for number, teams in enumerate(formed, start=1):
        lobby_id = conn.execute("INSERT INTO lobbies (tournament_id, number) VALUES (?, ?)",
                                (tournament_id, number)).lastrowid
        for side, players, captain in (("A", teams.team_a, teams.captain_a), ("B", teams.team_b, teams.captain_b)):
            conn.execute("INSERT INTO lobby_sides (lobby_id, side, captain_team_id) VALUES (?, ?, ?)",
                         (lobby_id, side, captain))
            conn.executemany("INSERT INTO lobby_players (lobby_id, side, spot, team_id) VALUES (?, ?, ?, ?)",
                             [(lobby_id, side, spot, player) for spot, player in enumerate(players)])
    conn.executemany("INSERT INTO lobby_waitlist (team_id, tournament_id, position) VALUES (?, ?, ?)",
                     [(team_id, tournament_id, position) for position, team_id in enumerate(waitlist, start=1)])


def set_captain(conn: sqlite3.Connection, lobby_id: int, side: str, team_id: int) -> None:
    with conn:
        conn.execute("UPDATE lobby_sides SET captain_team_id = ? WHERE lobby_id = ? AND side = ?",
                     (team_id, lobby_id, side))


# -- leaving and replacements --------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One player leaving: from a lobby (`lobby_id`) or the waitlist (None), and who took
    their spot. `captain_lost` is True when their team was left with no captain."""

    leaving: int
    lobby_id: Optional[int]
    newcomer: Optional[int]
    captain_lost: bool = False


def _replacement_pool(conn: sqlite3.Connection, tournament_id: int) -> list[int]:
    """Who can be brought in: anyone waiting before check-in starts, and after that only
    those who checked in."""
    waitlist = get_waitlist(conn, tournament_id)
    if db.get_tournament(conn, tournament_id)["checkin_ends_at"] is None:
        return waitlist
    checked_in = {s.team_id for s in get_signups(conn, tournament_id) if s.checked_in}
    return [team_id for team_id in waitlist if team_id in checked_in]


def _replace(conn: sqlite3.Connection, tournament_id: int, lobby: Lobby, leaving: int,
             only_if_filled: bool = False) -> Optional[Change]:
    """Fill `leaving`'s spot in place from the waitlist, without committing. With
    `only_if_filled`, nothing changes (and None is returned) when nobody can take it."""
    side = lobby.side_of(leaving)
    team = lobby.teams[side]
    prefs = captain_prefs(get_signups(conn, tournament_id))
    replacement = lobbies.replace_in_place(team.players, team.captain, leaving, prefs,
                                           _replacement_pool(conn, tournament_id))
    if replacement.newcomer is None and only_if_filled:
        return None
    if replacement.newcomer is None:
        conn.execute("DELETE FROM lobby_players WHERE team_id = ?", (leaving,))
    else:
        conn.execute("DELETE FROM lobby_waitlist WHERE team_id = ?", (replacement.newcomer,))
        conn.execute("UPDATE lobby_players SET team_id = ? WHERE team_id = ?", (replacement.newcomer, leaving))
    conn.execute("UPDATE lobby_sides SET captain_team_id = ? WHERE lobby_id = ? AND side = ?",
                 (replacement.captain, lobby.id, side))
    # The players changed, so points entered for them are out of date.
    conn.execute("DELETE FROM lobby_entries WHERE lobby_id = ?", (lobby.id,))
    lost = replacement.captain is None and team.captain is not None
    return Change(leaving, lobby.id, replacement.newcomer, lost)


def leave(conn: sqlite3.Connection, tournament_id: int, team_id: int, reason: str,
          by: Optional[int]) -> Optional[Change]:
    """Take a player out of an event whose lobbies are drawn, marking them withdrawn for
    `reason` (as in db.WITHDRAWAL_REASONS) and filling their spot from the waitlist. None
    if they hold no spot and aren't waiting."""
    with conn:
        lobby = find_lobby_of(conn, tournament_id, team_id)
        if lobby is not None:
            change = _replace(conn, tournament_id, lobby, team_id)
        elif conn.execute("DELETE FROM lobby_waitlist WHERE team_id = ?", (team_id,)).rowcount:
            change = Change(team_id, None, None)
        else:
            return None
        conn.execute(f"UPDATE teams SET withdrawn = ?, withdrawn_by = ?, withdrawn_at = {_NOW} WHERE id = ?",
                     (reason, by, team_id))
    return change


def move_to_other_team(conn: sqlite3.Connection, tournament_id: int, team_id: int) -> Optional[Change]:
    """An organizer moving a player to the other team of their lobby, into a free spot
    there, taking its captaincy if it has none and they can captain. Their old spot is
    filled from the waitlist like anyone leaving it. None if they aren't in a lobby, or
    the other team is full."""
    with conn:
        lobby = find_lobby_of(conn, tournament_id, team_id)
        if lobby is None:
            return None
        other = "B" if lobby.side_of(team_id) == "A" else "A"
        if len(lobby.teams[other].players) >= lobbies.MAX_LOBBY // 2:
            return None
        change = _replace(conn, tournament_id, lobby, team_id)
        conn.execute("DELETE FROM lobby_players WHERE team_id = ?", (team_id,))
        conn.execute(
            """INSERT INTO lobby_players (lobby_id, side, spot, team_id)
               SELECT ?, ?, COALESCE(MAX(spot), -1) + 1, ? FROM lobby_players WHERE lobby_id = ? AND side = ?""",
            (lobby.id, other, team_id, lobby.id, other),
        )
        signup = next(s for s in get_signups(conn, tournament_id) if s.team_id == team_id)
        if lobby.teams[other].captain is None and signup.can_captain:
            conn.execute("UPDATE lobby_sides SET captain_team_id = ? WHERE lobby_id = ? AND side = ?",
                         (team_id, lobby.id, other))
    return change


def bring_in(conn: sqlite3.Connection, tournament_id: int, leaving: int, newcomer: int) -> bool:
    """An organizer swapping a waitlisted player into `leaving`'s spot, whatever the
    waitlist order. The player leaving goes to the front of the waitlist. False if either
    isn't where they should be."""
    with conn:
        lobby = find_lobby_of(conn, tournament_id, leaving)
        if lobby is None or newcomer not in get_waitlist(conn, tournament_id):
            return False
        conn.execute("UPDATE lobby_players SET team_id = ? WHERE team_id = ?", (newcomer, leaving))
        conn.execute("DELETE FROM lobby_waitlist WHERE team_id = ?", (newcomer,))
        conn.execute("UPDATE lobby_waitlist SET position = position + 1 WHERE tournament_id = ?", (tournament_id,))
        conn.execute("INSERT INTO lobby_waitlist (team_id, tournament_id, position) VALUES (?, ?, 1)",
                     (leaving, tournament_id))
        conn.execute("UPDATE lobby_sides SET captain_team_id = NULL WHERE lobby_id = ? AND captain_team_id = ?",
                     (lobby.id, leaving))
        conn.execute("DELETE FROM lobby_entries WHERE lobby_id = ?", (lobby.id,))
    return True


# -- check-in ---------------------------------------------------------------------------

def start_checkin(conn: sqlite3.Connection, tournament_id: int, ends_at: str) -> bool:
    """False if check-in already started, or the lobbies aren't drawn yet."""
    with conn:
        return conn.execute(
            """UPDATE tournaments SET checkin_ends_at = ?
               WHERE id = ? AND format = 'lobbies' AND status = 'in_progress' AND checkin_ends_at IS NULL""",
            (ends_at, tournament_id),
        ).rowcount > 0


def cancel_checkin(conn: sqlite3.Connection, tournament_id: int) -> bool:
    """Call off a check-in that's still open, clearing everyone's check-in so a new one
    starts afresh. False if none is open."""
    with conn:
        cancelled = conn.execute(
            """UPDATE tournaments SET checkin_ends_at = NULL
               WHERE id = ? AND checkin_ends_at IS NOT NULL AND checkin_finished = 0""",
            (tournament_id,),
        ).rowcount > 0
        if cancelled:
            conn.execute("UPDATE lobby_signups SET checked_in_at = NULL WHERE tournament_id = ?", (tournament_id,))
    return cancelled


def check_in(conn: sqlite3.Connection, team_id: int) -> bool:
    """False if they'd already checked in."""
    with conn:
        return conn.execute(f"UPDATE lobby_signups SET checked_in_at = {_NOW} WHERE team_id = ? AND checked_in_at IS NULL",
                            (team_id,)).rowcount > 0


def get_checkins_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """5v5 events whose check-in has run out but hasn't been finished."""
    return conn.execute(
        f"""SELECT * FROM tournaments WHERE format = 'lobbies' AND status = 'in_progress'
            AND checkin_finished = 0 AND checkin_ends_at <= {_NOW}"""
    ).fetchall()


def finish_checkin(conn: sqlite3.Connection, tournament_id: int) -> Optional[list[Change]]:
    """Replace everyone in a lobby who didn't check in, in place, from the checked-in
    waitlist. Captains are replaced first, so waiting captain candidates go where they're
    needed. A no-show nobody suitable can replace keeps their spot: they may only have
    forgotten to press the button. None, with nothing changed, if check-in had already
    been finished, or if a team would still have no captain: check-in then stays open."""
    with conn:
        claimed = conn.execute(
            "UPDATE tournaments SET checkin_finished = 1 WHERE id = ? AND checkin_finished = 0 AND checkin_ends_at IS NOT NULL",
            (tournament_id,),
        ).rowcount
        if not claimed:
            return None
        checked_in = {s.team_id for s in get_signups(conn, tournament_id) if s.checked_in}
        changes = []
        for lobby in get_lobbies(conn, tournament_id):
            missing = [player for player in lobby.players if player not in checked_in]
            for player in sorted(missing, key=lambda player: player not in lobby.captains):
                change = _replace(conn, tournament_id, get_lobby(conn, lobby.id), player, only_if_filled=True)
                if change is not None:
                    changes.append(change)
        if teams_without_captain(conn, tournament_id):
            conn.rollback()  # check-in stays open until every team has a captain
            return None
    return changes


def teams_without_captain(conn: sqlite3.Connection, tournament_id: int) -> list[tuple[Lobby, str]]:
    """(lobby, side) for each team still playing that has no captain."""
    return [(lobby, side) for lobby in get_lobbies(conn, tournament_id) if get_result(conn, lobby.id) is None
            for side in SIDES if lobby.teams[side].captain is None]


def checkin_running(tournament: sqlite3.Row) -> bool:
    return tournament["checkin_ends_at"] is not None and not tournament["checkin_finished"]


# -- room codes ---------------------------------------------------------------------------

def set_room_code(conn: sqlite3.Connection, lobby_id: int, code: str, user_id: int) -> None:
    with conn:
        conn.execute("UPDATE lobbies SET room_code = ?, host_user_id = ? WHERE id = ?", (code, user_id, lobby_id))


# -- finishing, points and results ---------------------------------------------------

def results_post_key(lobby_id: int) -> str:
    return f"results:{lobby_id}"


def get_finished_sides(conn: sqlite3.Connection, lobby_id: int) -> set[str]:
    """The sides whose captain has pressed Finish."""
    return {row["side"] for row in conn.execute("SELECT side FROM lobby_finishes WHERE lobby_id = ?", (lobby_id,))}


def finish(conn: sqlite3.Connection, lobby_id: int, sides: list[str], user_id: int) -> bool:
    """Record `sides` as finished. Once every captain has, the lobby's results post is
    reserved. Returns whether the races are over."""
    with conn:
        conn.executemany("INSERT OR IGNORE INTO lobby_finishes (lobby_id, side, finished_by) VALUES (?, ?, ?)",
                         [(lobby_id, side, user_id) for side in sides])
        lobby = get_lobby(conn, lobby_id)
        over = races_over(lobby, get_finished_sides(conn, lobby_id))
        if over:
            conn.execute(
                """INSERT OR IGNORE INTO lobby_posts (tournament_id, post_key, channel_id)
                   SELECT tournament_id, ?, channel_id FROM lobby_posts WHERE tournament_id = ? AND post_key = ?""",
                (results_post_key(lobby_id), lobby.tournament_id, EVENT_POST))
    return over


def races_over(lobby: Lobby, finished_sides: set[str]) -> bool:
    """Whether every captain has pressed Finish. A team with no captain has nobody to press it."""
    return all(side in finished_sides for side in SIDES if lobby.teams[side].captain is not None) \
        and bool(finished_sides)


@dataclass(frozen=True)
class Entry:
    """One captain's entry of the points in their lobby: everyone's, or so far one team's."""
    lobby_id: int
    side: str
    points: dict[int, int]
    entered_by: int
    changed_at: str


def _entry(row: sqlite3.Row) -> Entry:
    points = {int(team_id): value for team_id, value in json.loads(row["points"]).items()}
    return Entry(row["lobby_id"], row["side"], points, row["entered_by"], row["changed_at"])


def get_entries(conn: sqlite3.Connection, lobby_id: int) -> dict[str, Entry]:
    return {row["side"]: _entry(row) for row in conn.execute("SELECT * FROM lobby_entries WHERE lobby_id = ?",
                                                             (lobby_id,))}


def is_complete(lobby: Lobby, entry: Entry) -> bool:
    """Whether a captain's entry has every player's points. They enter one team's at a time."""
    return all(player in entry.points for player in lobby.players)


def mismatched_players(lobby: Lobby, entries: dict[str, Entry]) -> list[int]:
    """The players the two captains entered different points for, or [] until both have
    entered everyone's."""
    if len(entries) < 2 or not all(is_complete(lobby, entry) for entry in entries.values()):
        return []
    a, b = entries["A"].points, entries["B"].points
    return [player for player in lobby.players if a[player] != b[player]]


def lone_entry(lobby: Lobby, entries: dict[str, Entry]) -> Optional[Entry]:
    """The one captain's complete entry, while the other captain hasn't completed theirs."""
    complete = [entry for entry in entries.values() if is_complete(lobby, entry)]
    return complete[0] if len(complete) == 1 else None


def enter_points(conn: sqlite3.Connection, lobby_id: int, side: str, user_id: int, points: dict[int, int]) -> bool:
    """Save a captain's entry, replacing their earlier one. Once it's complete and matches
    the other captain's, or the other team has no captain to enter one, it's recorded as
    the result. Returns whether it was."""
    with conn:
        conn.execute(
            f"""INSERT INTO lobby_entries (lobby_id, side, points, entered_by, changed_at)
                VALUES (?, ?, ?, ?, {_NOW})
                ON CONFLICT (lobby_id, side) DO UPDATE SET points = excluded.points,
                    entered_by = excluded.entered_by, changed_at = excluded.changed_at""",
            (lobby_id, side, json.dumps(points), user_id),
        )
        lobby = get_lobby(conn, lobby_id)
        entries = get_entries(conn, lobby_id)
        if not is_complete(lobby, entries[side]):
            return False
        other = "B" if side == "A" else "A"
        if lobby.teams[other].captain is not None and (
                other not in entries or not is_complete(lobby, entries[other]) or mismatched_players(lobby, entries)):
            return False
        _record_result(conn, lobby_id, points, user_id)
    return True


def get_lone_entries(conn: sqlite3.Connection) -> list[Entry]:
    """Complete entries the other captain hasn't completed one beside yet, in lobbies with no result."""
    lobby_ids = [row[0] for row in conn.execute(
        """SELECT DISTINCT e.lobby_id FROM lobby_entries e
           WHERE NOT EXISTS (SELECT 1 FROM lobby_results r WHERE r.lobby_id = e.lobby_id)""")]
    found = (lone_entry(get_lobby(conn, lobby_id), get_entries(conn, lobby_id)) for lobby_id in lobby_ids)
    return [entry for entry in found if entry is not None]


def accept_lone_entry(conn: sqlite3.Connection, entry: Entry) -> bool:
    """Record one captain's entry as the result, the other captain not having completed
    theirs in time. Only if it's unchanged and still alone, so a late entry is never overruled."""
    with conn:
        lobby = get_lobby(conn, entry.lobby_id)
        if get_result(conn, entry.lobby_id) is not None or lone_entry(lobby, get_entries(conn, entry.lobby_id)) != entry:
            return False
        _record_result(conn, entry.lobby_id, entry.points, None)
    return True


def record_result(conn: sqlite3.Connection, lobby_id: int, points: dict[int, int], by: int) -> None:
    """An organizer's result, final at once. It replaces any result already there."""
    with conn:
        _record_result(conn, lobby_id, points, by)


def winning_side(lobby: Lobby, points: dict[int, int]) -> Optional[str]:
    """The side with more points, or None on a tie (which a tiebreaker settles before
    anyone enters the points)."""
    totals = {side: sum(points.get(player, 0) for player in lobby.teams[side].players) for side in SIDES}
    if totals["A"] == totals["B"]:
        return None
    return max(SIDES, key=totals.get)


def _record_result(conn: sqlite3.Connection, lobby_id: int, points: dict[int, int], by: Optional[int]) -> None:
    lobby = get_lobby(conn, lobby_id)
    side = winning_side(lobby, points)
    if side is None:
        raise ValueError("A tied result can't be recorded; the tiebreaker decides it first.")
    conn.execute("DELETE FROM lobby_results WHERE lobby_id = ?", (lobby_id,))
    conn.execute("INSERT INTO lobby_results (lobby_id, winning_side, recorded_by) VALUES (?, ?, ?)",
                 (lobby_id, side, by))
    conn.executemany("INSERT INTO lobby_points (lobby_id, team_id, points) VALUES (?, ?, ?)",
                     [(lobby_id, player, points[player]) for player in lobby.players])
    finished = conn.execute(
        """SELECT COUNT(*) = 0 FROM lobbies l WHERE l.tournament_id = ?
           AND NOT EXISTS (SELECT 1 FROM lobby_results r WHERE r.lobby_id = l.id)""",
        (lobby.tournament_id,),
    ).fetchone()[0]
    if finished:
        conn.execute("UPDATE tournaments SET status = 'completed' WHERE id = ? AND status = 'in_progress'",
                     (lobby.tournament_id,))


@dataclass(frozen=True)
class LobbyResult:
    winning_side: str
    points: dict[int, int]


def get_result(conn: sqlite3.Connection, lobby_id: int) -> Optional[LobbyResult]:
    row = conn.execute("SELECT winning_side FROM lobby_results WHERE lobby_id = ?", (lobby_id,)).fetchone()
    if row is None:
        return None
    points = {r["team_id"]: r["points"] for r in conn.execute(
        "SELECT team_id, points FROM lobby_points WHERE lobby_id = ?", (lobby_id,))}
    return LobbyResult(row["winning_side"], points)


def load_lobby_history(conn: sqlite3.Connection, guild_id: int) -> list[PlayedLobby]:
    """Every lobby played in the server's concluded 5v5 events, oldest event first, for
    contribution.contributions."""
    history = []
    for tournament in reversed(db.get_concluded_tournaments(conn, guild_id)):
        if tournament["format"] != "lobbies":
            continue
        user_ids = {team_id: users[0] for team_id, users in db.get_members_by_team(conn, tournament["id"]).items()
                    if users}
        for lobby in get_lobbies(conn, tournament["id"]):
            result = get_result(conn, lobby.id)
            if result is None:
                continue
            winners = lobby.teams[result.winning_side]
            history.append(PlayedLobby(result.points, frozenset(winners.players), winners.captain, user_ids))
    return history


# -- public posts --------------------------------------------------------------------

EVENT_POST = "event"


def add_event_post(conn: sqlite3.Connection, tournament_id: int, channel_id: int) -> None:
    """Reserve the event's post, without committing. It's sent the next time posts are
    brought up to date."""
    conn.execute("INSERT OR IGNORE INTO lobby_posts (tournament_id, post_key, channel_id) VALUES (?, ?, ?)",
                 (tournament_id, EVENT_POST, channel_id))


def get_posts(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM lobby_posts WHERE tournament_id = ? ORDER BY post_key = 'event', post_key",
                        (tournament_id,)).fetchall()


def get_post(conn: sqlite3.Connection, tournament_id: int, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM lobby_posts WHERE tournament_id = ? AND post_key = ?",
                        (tournament_id, key)).fetchone()


def get_tournaments_with_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """5v5 events whose posts are kept up to date: from the draw until they're concluded."""
    return conn.execute(
        "SELECT * FROM tournaments WHERE format = 'lobbies' AND status IN ('in_progress', 'completed')"
    ).fetchall()


def claim_post_send(conn: sqlite3.Connection, tournament_id: int, key: str) -> Optional[str]:
    """Claim the sending of a new message for a post, as db.claim_post_send does for match posts."""
    with conn:
        claim = conn.execute(f"SELECT {_NOW}").fetchone()[0]
        taken = conn.execute(
            """UPDATE lobby_posts SET sending_at = ?
                WHERE tournament_id = ? AND post_key = ?
                AND (sending_at IS NULL OR sending_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 minute'))""",
            (claim, tournament_id, key),
        ).rowcount > 0
        return claim if taken else None


def take_over_post(conn: sqlite3.Connection, tournament_id: int, key: str, claim: str, message_id: int,
                   shown: str, pinged: Optional[str]) -> bool:
    with conn:
        return conn.execute(
            """UPDATE lobby_posts SET message_id = ?, shown = ?, pinged = ?, sending_at = NULL
               WHERE tournament_id = ? AND post_key = ? AND sending_at = ?""",
            (message_id, shown, pinged, tournament_id, key, claim),
        ).rowcount > 0


def release_post_send(conn: sqlite3.Connection, tournament_id: int, key: str) -> None:
    with conn:
        conn.execute("UPDATE lobby_posts SET sending_at = NULL WHERE tournament_id = ? AND post_key = ?",
                     (tournament_id, key))


def set_post_shown(conn: sqlite3.Connection, tournament_id: int, key: str, message_id: int, shown: str) -> None:
    with conn:
        conn.execute("UPDATE lobby_posts SET shown = ? WHERE tournament_id = ? AND post_key = ? AND message_id = ?",
                     (shown, tournament_id, key, message_id))


def mark_post_unsent(conn: sqlite3.Connection, tournament_id: int, key: str, message_id: int) -> None:
    """The post's message was deleted, so it's sent again."""
    with conn:
        conn.execute(
            """UPDATE lobby_posts SET message_id = NULL, shown = NULL, pinged = NULL
               WHERE tournament_id = ? AND post_key = ? AND message_id = ?""",
            (tournament_id, key, message_id),
        )
