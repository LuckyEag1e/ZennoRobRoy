"""Embed-rendering helpers. Takes plain data (dicts/rows), builds discord.Embed objects."""

from __future__ import annotations

import sqlite3

import discord

from ascii_bracket import prediction_marker, real_result_marker, render_tree
from bracket import Match, Podium, UserScore, ranked_places
from labels import crowd_verdict, join_within, pick_share, pickem_podium_lines, podium_lines, score_label, team_label

# Discord rejects an embed field value or description longer than these.
EMBED_FIELD_LIMIT = 1024
EMBED_DESCRIPTION_LIMIT = 4096


def mention(user_id: int) -> str:
    return f"<@{user_id}>"


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
    for place, s in ranked_places(scores):
        lines.append(f"**{place}.** {mention(s.user_id)} — {score_label(s)}")
    embed.description += join_within(lines, EMBED_DESCRIPTION_LIMIT - len(embed.description))
    return embed


def render_final_results_embed(
    tournament_name: str, result: Podium, teams_by_id: dict[int, sqlite3.Row], scores: list[UserScore],
) -> discord.Embed:
    embed = discord.Embed(title=f"🏆 {tournament_name}: final results")
    embed.add_field(name="Podium", value="\n".join(podium_lines(result, teams_by_id)), inline=False)
    pickem = pickem_podium_lines(scores, mention)
    embed.add_field(name="Pick'em leaders", value=join_within(pickem, EMBED_FIELD_LIMIT), inline=False)
    if scores:
        player_word = "player" if len(scores) == 1 else "players"
        embed.set_footer(text=f"{len(scores)} {player_word} made predictions. Thanks for playing!")
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
    score: UserScore,
) -> discord.Embed:
    tree = render_tree(
        matches_by_id, feeders_by_target, teams_by_id, slots,
        picks.get, prediction_marker(results_by_match, picks),
    )
    embed = discord.Embed(title=f"{user.display_name}'s picks: {tournament_name}", description=tree)

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
        title=f"Round {match.played_round} pick breakdown: {tournament_name}",
        description=crowd_verdict(counts, team_a_id, team_b_id, teams_by_id),
    )
    for team_id in (team_a_id, team_b_id):
        embed.add_field(name=team_label(teams_by_id, team_id), value=pick_share(counts[team_id], total), inline=True)
    return embed
