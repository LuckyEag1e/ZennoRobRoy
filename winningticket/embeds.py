"""Embed-rendering helpers. Takes plain data (dicts/rows), builds discord.Embed objects."""

from __future__ import annotations

import sqlite3

import discord

from ascii_bracket import prediction_marker, real_result_marker, render_tree
from bracket import Match, UserScore
from labels import crowd_verdict, pick_share, score_label, team_label


def render_bracket_embed(
    tournament_name: str,
    status: str,
    matches_by_id: dict[int, Match],
    feeders_by_target: dict,
    slots: dict[int, tuple[int | None, int | None]],
    results_by_match: dict[int, int],
    teams_by_id: dict[int, sqlite3.Row],
    title: str = "Bracket",
) -> discord.Embed:
    tree = render_tree(
        matches_by_id, feeders_by_target, teams_by_id, slots,
        results_by_match.get, real_result_marker(results_by_match),
    )
    embed = discord.Embed(
        title=f"{title}: {tournament_name}",
        description=f"Status: {status}\n\n{tree}",
    )
    return embed


def render_leaderboard_embed(tournament_name: str, scores: list[UserScore]) -> discord.Embed:
    embed = discord.Embed(title=f"Leaderboard: {tournament_name}")
    if not scores:
        embed.description = "No predictions have been submitted yet."
        return embed

    # Every user with any prediction rows has a full bracket -- /predict only
    # ever saves once, at the final confirm step, with the complete set of
    # picks -- so this count is exactly "how many users have entered."
    participant_word = "participant" if len(scores) == 1 else "participants"
    embed.description = f"**{len(scores)} {participant_word}**\n"

    lines = []
    for rank, s in enumerate(scores, start=1):
        lines.append(f"**{rank}.** <@{s.user_id}> — {score_label(s)}")
    embed.description += "\n".join(lines)
    return embed


def render_my_picks_embed(
    tournament_name: str,
    user: discord.abc.User,
    matches_by_id: dict[int, Match],
    feeders_by_target: dict,
    slots: dict[int, tuple[int | None, int | None]],
    picks: dict[int, int],
    teams_by_id: dict[int, sqlite3.Row],
    results_by_match: dict[int, int],
    score: UserScore | None,
) -> discord.Embed:
    tree = render_tree(
        matches_by_id, feeders_by_target, teams_by_id, slots,
        picks.get, prediction_marker(results_by_match, picks),
    )
    embed = discord.Embed(title=f"{user.display_name}'s picks: {tournament_name}", description=tree)

    if score is not None:
        embed.set_footer(text=f"Current score: {score_label(score)}")

    return embed


def render_match_stats_embed(
    tournament_name: str,
    match: Match,
    team_a_id: int,
    team_b_id: int,
    counts: dict[int, int],
    teams_by_id: dict[int, sqlite3.Row],
) -> discord.Embed:
    total = sum(counts.values())
    embed = discord.Embed(
        title=f"Round {match.round} pick breakdown: {tournament_name}",
        description=crowd_verdict(counts, team_a_id, team_b_id, teams_by_id),
    )
    for team_id in (team_a_id, team_b_id):
        embed.add_field(name=team_label(teams_by_id, team_id), value=pick_share(counts[team_id], total), inline=True)
    return embed
