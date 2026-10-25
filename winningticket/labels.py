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
    return [f"{MEDALS[place]} {player_label(s.user_id)} — {score_label(s)}" for place, s in top_places(scores)]
