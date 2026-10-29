"""Plain-text labels shared by the Discord embeds and the CLI, so both read the same."""

from __future__ import annotations

from typing import Callable, Optional

from bracket import Match, Podium, UserScore, byes_in_round, effective_winner, matches_in_round, rounds, top_places
from ratings import Ratings

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


def name_taken_message(existing) -> str:
    """Why a new tournament can't have `existing`'s name. A concluded tournament keeps its
    name, so the history commands never mix two tournaments up."""
    name = existing["name"]
    if existing["status"] == "concluded":
        return f"'{name}' was the name of a concluded tournament, and names aren't reused. Pick another name."
    return f"A tournament named '{name}' already exists in this server."


# Shown privately at a member's first sign-up, before anything is tied to their account.
CONSENT_NOTICE = (
    "Before you're signed up: WinningTicket can link your tournament entries, results and Elo rating to "
    "your Discord account, so your history and seeding follow you from one tournament to the next. That "
    "also covers anything else it keeps about you as a player in future. All data collected will not be "
    "used for any purpose other than providing a fair and competitive experience for participants in "
    "hosted events.\n"
    "**Agree** to link them. The bot remembers your choice. You may opt out at any time with the "
    "`/optout` command, which will also unlink all data previously associated with your account, so "
    "your past entries only show your name.\n"
    "**Opt out** to enter by name only. Nothing is linked to your account, and you're seeded at the "
    "default rating."
)
# Prediction picks are always saved with the member's account, since the pool scores them.
OPTED_OUT_NOTE = (
    "Your entry isn't linked to your account, so ask an organizer if you need to withdraw. If you use "
    "`/predict`, your picks are still saved with your Discord account and appear in this tournament's history."
)
OPT_OUT_CONFIRMATION = "opt out"


def opted_out_message(unlinked: int) -> str:
    """What /optout says once it's done."""
    if not unlinked:
        done = "Nothing of yours is linked to your account now."
    else:
        entry_word = "entry is" if unlinked == 1 else "entries are"
        done = f"{unlinked} of your {entry_word} no longer linked to your account. They keep your name, and your rating is back to the default."
    return (f"{done} You'll be asked again the next time you sign up. Your `/predict` picks stay with your "
            f"account in each tournament's history.")


FORMAT_NAMES = {"single_elim": "Single elimination"}


def history_line(summary) -> str:
    """One concluded tournament in /history list: its name, champion, size, format, whether
    it was played in teams, and the day its final was recorded."""
    entrant_word = "entrant" if summary.entrants == 1 else "entrants"
    finished = summary.finished_at[:10] if summary.finished_at else "unfinished"
    return (f"{summary.name}: 🏆 {summary.champion or 'no champion'} · {summary.entrants} {entrant_word} · "
            f"{FORMAT_NAMES[summary.format]} · {'Teams' if summary.has_teams else 'Singles'} · {finished}")


def seeding_lines(tournament, teams_by_id: dict) -> list[str]:
    """How a tournament's bracket was seeded, then its entrants in seed order. Seeds can
    also come from a CSV or /admin editteam, so only seeds from closing sign-ups by rating
    are described as rating seeds."""
    seeded = sorted((team for team in teams_by_id.values() if team["seed"] is not None), key=lambda team: team["seed"])
    unseeded = sorted(team["name"] for team in teams_by_id.values() if team["seed"] is None)
    if not seeded:
        return ["This bracket wasn't seeded."]
    if tournament["seeding_mode"] in ("standard", "slide"):
        lines = [f"Entrants were {draw_description(tournament['seeding_mode'])}."]
    else:
        lines = ["Seeds were set by the organizers."]
    lines += [f"{team['seed']}. {team['name']}" for team in seeded]
    if unseeded:
        lines.append("Unseeded: " + ", ".join(unseeded))
    return lines


def draw_description(seeding_mode: str) -> str:
    """How closing registration put the entrants into the bracket."""
    if seeding_mode == "random":
        return "drawn into the bracket at random"
    if seeding_mode == "slide":
        return "seeded into the bracket by rating, with the top seeds playing mid-ranked seeds first"
    return "seeded into the bracket by rating"


def entrant_lines(entrants: list, player_label: Callable[[int], str]) -> list[str]:
    """A numbered sign-up list, with `player_label` naming each entrant's user by id."""
    lines = []
    for number, entrant in enumerate(entrants, start=1):
        account = "no Discord account" if entrant["user_id"] is None else player_label(entrant["user_id"])
        lines.append(f"{number}. {entrant['name']} ({account})")
    return lines


def rating_lines(user_id: int, ratings: Ratings) -> list[str]:
    """A member's singles and team Elo, saying which ones they haven't earned by playing yet."""
    singles = f"Singles: **{ratings.singles_rating(user_id):.0f}**"
    if user_id not in ratings.singles:
        singles += " (provisional: no singles games yet)"
    team = f"Team: **{ratings.team_rating(user_id):.0f}**"
    if user_id not in ratings.team:
        team += " (provisional: no team games yet" + (", so it starts from singles)" if user_id in ratings.singles else ")")
    return [singles, team]


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
    "**Automatic seeding:** each player's Elo rating from past tournaments decides their "
    "seed, so the strongest players don't meet early. `/rating` shows yours.",
    "**Live results:** organizers record results as games finish, the bot announces who "
    "advances, and anyone can check the bracket or see how the pool picked each matchup.",
    "**Final results:** concluding a tournament posts the podium and the top predictors, "
    "and keeps the results for the future.",
    "**Anonymous feedback** for the organizers.",
]
PLANNED_FEATURES = [
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
