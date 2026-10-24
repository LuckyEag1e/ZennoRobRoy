"""Plain-text bracket tree rendering, for Discord ANSI-coloring and the CLI.

No discord.py dependency -- just builds strings, so it's usable from both the
bot and cli.py, and unit-testable like bracket.py.

Colors use Discord's ```ansi fenced code block support (real ANSI SGR escape
codes), giving each round its own distinct color, plus bold for a winner and
underline for a confirmed-wrong pick -- the literal +/- markers stay in the
text too as a fallback in case ANSI doesn't render on some client.
"""

from __future__ import annotations

from typing import Callable, Optional

from bracket import Match, WinnerLookup, bye_team_id, is_bye
from labels import team_label

# (match, team_a_id, team_b_id) -> {team_id: '+' or '-'} for whichever teams
# in this match should be marked; teams not present render unmarked.
LineMarker = Callable[[Match, Optional[int], Optional[int]], dict]

# Every name slot is padded or truncated to this width. Each round repeats
# names from the round before, so uncapped long names widen every later
# round until Discord wraps the lines and breaks the diagram.
NAME_SLOT_WIDTH = 10

_ESC = "\x1b"
_RESET = f"{_ESC}[0m"
# Discord's ```ansi support covers: 0 reset, 1 bold, 4 underline, and the
# standard 30-37 foreground colors. Cycles if a bracket has more than 5
# rounds (a 64-team bracket would need a 6th round).
_ROUND_COLORS = [36, 33, 34, 35, 37]  # cyan, yellow, blue, magenta, white


def _round_color(round_num: int) -> int:
    return _ROUND_COLORS[(round_num - 1) % len(_ROUND_COLORS)]


def _colorize(text: str, round_num: int, mark: str) -> str:
    """mark: '+' (winner -> bold), '-' (confirmed wrong -> underline), or
    ' ' (no verdict yet -> plain color)."""
    style = "1;" if mark == "+" else "4;" if mark == "-" else ""
    return f"{_ESC}[{style}{_round_color(round_num)}m{text}{_RESET}"


def _fit(text: str) -> str:
    if len(text) > NAME_SLOT_WIDTH:
        return text[: NAME_SLOT_WIDTH - 1] + "…"
    return text.ljust(NAME_SLOT_WIDTH)


def real_result_marker(results_by_match: dict) -> LineMarker:
    """Winner marked '+', loser marked '-', once a match has a recorded result."""

    def marker(match: Match, team_a: Optional[int], team_b: Optional[int]) -> dict:
        winner = results_by_match.get(match.id)
        if winner is None:
            return {}
        marks = {}
        if team_a is not None:
            marks[team_a] = "+" if team_a == winner else "-"
        if team_b is not None:
            marks[team_b] = "+" if team_b == winner else "-"
        return marks

    return marker


def prediction_marker(results_by_match: dict, picks: dict) -> LineMarker:
    """Always mark the user's own pick in a matchup -- '+' if there's no
    real result yet (it just means "this is what you picked") or if it
    matched the real result, '-' if the real result came in and it didn't.
    The other team in the matchup stays unmarked either way."""

    def marker(match: Match, team_a: Optional[int], team_b: Optional[int]) -> dict:
        pick = picks.get(match.id)
        if pick is None:
            return {}
        real_winner = results_by_match.get(match.id)
        if real_winner is None:
            return {pick: "+"}
        return {pick: "+" if pick == real_winner else "-"}

    return marker


def _render_match(
    match: Match,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
) -> tuple[list[str], int]:
    """Returns (lines, visible_width). visible_width is tracked explicitly,
    not derived from len(), since embedded ANSI escape bytes would otherwise
    throw off alignment/padding math."""
    if match.round == 1:
        if is_bye(match):
            sole = bye_team_id(match)
            plain = f"+{_fit(team_label(teams_by_id, sole))}"
            return [_colorize(plain, 1, "+")], len(plain)
        team_a, team_b = match.team_a_id, match.team_b_id
        marks = marker_fn(match, team_a, team_b)
        plain_a = f"{marks.get(team_a, ' ')}{_fit(team_label(teams_by_id, team_a))}"
        plain_b = f"{marks.get(team_b, ' ')}{_fit(team_label(teams_by_id, team_b))}"
        return [
            _colorize(plain_a, 1, marks.get(team_a, " ")),
            _colorize(plain_b, 1, marks.get(team_b, " ")),
        ], len(plain_a)

    def render_feeder(side: str) -> tuple[list[str], int]:
        feeder = feeders_by_target.get(match.id, {}).get(side)
        if feeder is None:
            plain = f" {_fit('TBD')}"
            return [plain], len(plain)
        return _render_match(feeder, feeders_by_target, teams_by_id, slots, winner_of, marker_fn)

    top_lines, top_width = render_feeder("A")
    bottom_lines, _ = render_feeder("B")

    team_a, team_b = slots[match.id]
    marks = marker_fn(match, team_a, team_b)
    winner = winner_of(match.id)
    if winner is not None:
        plain_own = f"{marks.get(winner, ' ')}{_fit(team_label(teams_by_id, winner))}"
        own_colored = _colorize(plain_own, match.round, marks.get(winner, " "))
    else:
        plain_own = f" {_fit('TBD')}"
        own_colored = plain_own  # nothing decided yet -- no color

    lines = _merge(top_lines, top_width, bottom_lines, own_colored)
    return lines, top_width + 2


def _merge(top_lines: list[str], width: int, bottom_lines: list[str], own_colored: str) -> list[str]:
    # The bracket is a perfect binary tree, so both feeders are the same width.
    result = []
    for i, line in enumerate(top_lines):
        result.append(line + ("─┐" if i == len(top_lines) - 1 else "  "))
    result.append(" " * width + "├─" + own_colored)
    for i, line in enumerate(bottom_lines):
        result.append(line + ("─┘" if i == 0 else "  "))
    return result


def render_tree(
    matches_by_id: dict,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
) -> str:
    """Render the full connected bracket tree as a ```ansi fenced block, one
    color per round. Round-1 byes still occupy their correct position in the
    tree (needed so the connecting lines into round 2 are right) but render
    as a single line, no fake "vs" pairing, and are also called out in a
    summary line above the diagram so they're easy to spot at a glance.
    """
    byes = sorted(
        (m for m in matches_by_id.values() if is_bye(m)), key=lambda m: m.slot_index
    )
    bye_names = ", ".join(team_label(teams_by_id, bye_team_id(m)) for m in byes)

    root = next(m for m in matches_by_id.values() if m.feeds_into_match_id is None)
    tree_lines, _ = _render_match(root, feeders_by_target, teams_by_id, slots, winner_of, marker_fn)

    # Tracing which two branches converge at the final through a deep tree is
    # hard even with connector lines, so call it out explicitly up top too.
    final_a, final_b = slots[root.id]
    champion = winner_of(root.id)
    if champion is not None:
        final_line = f"Champion: {team_label(teams_by_id, champion)}"
    elif final_a is not None and final_b is not None:
        final_line = f"Final: {team_label(teams_by_id, final_a)} vs {team_label(teams_by_id, final_b)}"
    else:
        final_line = None

    out_lines: list[str] = []
    if bye_names:
        out_lines.append(f"Byes (round 1): {bye_names}")
    if final_line:
        out_lines.append(final_line)
    if out_lines:
        out_lines.append("")
    out_lines.append("```ansi")
    out_lines.extend(tree_lines)
    out_lines.append("```")
    return "\n".join(out_lines)
