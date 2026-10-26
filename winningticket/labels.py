"""Plain-text labels shared by the Discord embeds and the CLI, so both read the same."""

from __future__ import annotations

from typing import Callable, Optional

from bracket import Match, Podium, UserScore, byes_in_round, effective_winner, matches_in_round, rounds, top_places

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def team_name(teams_by_id: dict, team_id: Optional[int]) -> str:
    if team_id is None:
        return "TBD"
    team = teams_by_id.get(team_id)
    return team["name"] if team is not None else "Unknown"


def team_label(teams_by_id: dict, team_id: Optional[int]) -> str:
    """The team's name, prefixed with its seed when it has one."""
    team = teams_by_id.get(team_id) if team_id is not None else None
    if team is None or team["seed"] is None:
        return team_name(teams_by_id, team_id)
    return f"({team['seed']}) {team['name']}"


def matchup_label(match: Match, slots: dict, teams_by_id: dict) -> str:
    team_a, team_b = slots[match.id]
    return f"R{match.played_round}: {team_label(teams_by_id, team_a)} vs {team_label(teams_by_id, team_b)}"


def score_label(score: UserScore) -> str:
    return f"{score.points} pts ({score.correct_picks}/{score.possible_picks} correct)"


def pick_share(count: int, total: int) -> str:
    return f"{count} picks ({count / total * 100:.0f}%)" if total else f"{count} picks (0%)"


def crowd_verdict(counts: dict[int, int], team_a: int, team_b: int, teams_by_id: dict) -> str:
    """One line saying which side of a matchup the pool favours."""
    if sum(counts.values()) == 0:
        return "No predictions reference this round yet."
    if counts[team_a] == counts[team_b]:
        return "It's a dead heat, no clear favorite."
    favorite = team_a if counts[team_a] > counts[team_b] else team_b
    return f"Favorite: {team_label(teams_by_id, favorite)}"


def dropped_results_note(dropped: list[int]) -> str:
    """Suffix telling an admin that changing a result also cleared later ones."""
    if not dropped:
        return ""
    return f" Also cleared {len(dropped)} later result(s) that no longer matched the bracket; re-record them."


def pick_review_lines(matches_by_id: dict[int, Match], picks: dict[int, int], teams_by_id: dict) -> list[str]:
    """One line per game showing the user's pick, then one per team sitting that round out, round by round."""
    lines = []
    for round_num in rounds(matches_by_id):
        for match in matches_in_round(matches_by_id, round_num):
            lines.append(f"Round {round_num}: {team_label(teams_by_id, picks.get(match.id))}")
        for match in byes_in_round(matches_by_id, round_num):
            lines.append(f"Round {round_num}: {team_label(teams_by_id, effective_winner(match, picks.get))} (bye)")
    return lines


def podium_lines(result: Podium, teams_by_id: dict) -> list[str]:
    """Names only: once the tournament is over, seeds don't say anything about the result."""
    lines = [f"{MEDALS[1]} {team_name(teams_by_id, result.champion)}",
             f"{MEDALS[2]} {team_name(teams_by_id, result.runner_up)}"]
    if result.third_places:
        lines.append(f"{MEDALS[3]} " + " & ".join(team_name(teams_by_id, t) for t in result.third_places))
    return lines


def pickem_podium_lines(scores: list[UserScore], player_label: Callable[[int], str]) -> list[str]:
    """The top three pick'em scorers, with `player_label` naming each by user id."""
    if not scores:
        return ["Nobody made predictions."]
    placed = top_places(scores)
    if not placed:
        return ["Nobody scored any points."]
    return [f"{MEDALS[place]} {player_label(s.user_id)} — {score_label(s)}" for place, s in placed]


def entrant_lines(entrants: list, player_label: Callable[[int], str]) -> list[str]:
    """A numbered sign-up list, with `player_label` naming each entrant's user by id."""
    lines = []
    for number, entrant in enumerate(entrants, start=1):
        account = "no Discord account" if entrant["user_id"] is None else player_label(entrant["user_id"])
        lines.append(f"{number}. {entrant['name']} ({account})")
    return lines


def ban_lines(bans: list, player_label: Callable[[int], str]) -> list[str]:
    """One line per banned user, with `player_label` naming users by id."""
    lines = []
    for ban in bans:
        # banned_at is stored as ISO text, so its first ten characters are the date.
        line = f"{player_label(ban['user_id'])} (banned {ban['banned_at'][:10]} by {player_label(ban['banned_by'])})"
        lines.append(line + (f": {ban['reason']}" if ban["reason"] else ""))
    return lines


def join_within(lines: list[str], limit: int) -> str:
    """Join lines with newlines, dropping lines from the end, with a note saying how many,
    so the result fits in `limit` characters."""
    for shown in range(len(lines), 0, -1):
        hidden = len(lines) - shown
        text = "\n".join(lines[:shown] + ([f"…and {hidden} more"] if hidden else []))
        if len(text) <= limit:
            return text
    return f"…and {len(lines)} more"


# What /features (and the CLI's 'features') says the bot is for. Move a line from planned
# to current once it ships.
CURRENT_FEATURES = [
    "**Bracket prediction pool:** predict every match before a tournament starts. Correct "
    "picks score more in later rounds, and a live leaderboard tracks everyone.",
    "**Sign-ups:** players register themselves, and the bracket is drawn automatically when "
    "sign-ups close. Organizers can still build a bracket from a CSV instead.",
    "**Live results:** organizers record results as games finish, the bot announces who "
    "advances, and anyone can check the bracket or see how the pool picked each matchup.",
    "**Final results:** concluding a tournament posts the podium and the top predictors, "
    "and keeps the results for the future.",
    "**Anonymous feedback** for the organizers.",
]
PLANNED_FEATURES = [
    "**Automatic seeding** from each player's past results.",
    "**Self-reported results:** both players confirm a match result, and organizers only "
    "step in when they disagree.",
    "**More formats:** double elimination and round robin.",
    "**Team tournaments** with two or more players per entrant.",
]

FEEDBACK_PAGE_SIZE = 5


def feedback_page(entries: list, page: int) -> tuple[list, int, int]:
    """(entries on the page, the page shown, how many pages there are). Pages count from 1,
    and a page number out of range shows the nearest real page."""
    page_count = max(1, -(-len(entries) // FEEDBACK_PAGE_SIZE))
    page = min(max(page, 1), page_count)
    start = (page - 1) * FEEDBACK_PAGE_SIZE
    return entries[start:start + FEEDBACK_PAGE_SIZE], page, page_count
