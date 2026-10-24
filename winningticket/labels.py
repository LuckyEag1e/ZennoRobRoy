"""Plain-text labels shared by the Discord embeds and the CLI, so both read the same."""

from __future__ import annotations

from typing import Optional

from bracket import Match, UserScore, bye_team_id, is_bye, matches_in_round, rounds


def team_label(teams_by_id: dict, team_id: Optional[int]) -> str:
    if team_id is None:
        return "TBD"
    team = teams_by_id.get(team_id)
    if team is None:
        return "Unknown"
    seed = team["seed"]
    return f"({seed}) {team['name']}" if seed is not None else team["name"]


def matchup_label(match: Match, slots: dict, teams_by_id: dict) -> str:
    team_a, team_b = slots[match.id]
    return f"R{match.round}: {team_label(teams_by_id, team_a)} vs {team_label(teams_by_id, team_b)}"


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
    """One line per match, in bracket order, showing the user's pick (or the bye)."""
    lines = []
    for round_num in rounds(matches_by_id):
        for match in matches_in_round(matches_by_id, round_num):
            if is_bye(match):
                lines.append(f"Round {round_num}: {team_label(teams_by_id, bye_team_id(match))} (bye)")
            else:
                lines.append(f"Round {round_num}: {team_label(teams_by_id, picks.get(match.id))}")
    return lines
