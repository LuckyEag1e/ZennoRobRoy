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

from bracket import Match, WinnerLookup, bye_team_id, final_match, is_bye
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


# A rendered subtree: its lines with their visible widths, plus the column its
# connectors end at. Widths are tracked explicitly because the ANSI escape bytes
# would throw off len().
Rendered = tuple[list[tuple[str, int]], int]


def _render_team(team_id: Optional[int], match: Match, teams_by_id: dict, marks: dict) -> Rendered:
    """One entrant, marked by the result of `match`, the first game they play."""
    mark = marks.get(team_id, " ")
    plain = f"{mark}{_fit(team_label(teams_by_id, team_id))}"
    return [(_colorize(plain, match.played_round, mark), len(plain))], len(plain)


def _render_match(
    match: Match,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
) -> Rendered:
    """A real (non-bye) match: its two sides joined by a connector to its winner, or TBD."""
    team_a, team_b = slots[match.id]
    marks = marker_fn(match, team_a, team_b)

    def render_side(side: str, team_id: Optional[int]) -> Rendered:
        feeder = feeders_by_target.get(match.id, {}).get(side)
        if feeder is None or is_bye(feeder):
            return _render_team(team_id, match, teams_by_id, marks)
        return _render_match(feeder, feeders_by_target, teams_by_id, slots, winner_of, marker_fn)

    winner = winner_of(match.id)
    if winner is not None:
        mark = marks.get(winner, " ")
        plain_own = f"{mark}{_fit(team_label(teams_by_id, winner))}"
        own = (_colorize(plain_own, match.played_round + 1, mark), len(plain_own))
    else:
        plain_own = f" {_fit('TBD')}"
        own = (plain_own, len(plain_own))  # nothing decided yet -- no color

    return _join(render_side("A", team_a), render_side("B", team_b), own)


def _join(top: Rendered, bottom: Rendered, own: tuple[str, int]) -> Rendered:
    """Stack two subtrees with `own` between them, connected from the top subtree's last
    line and the bottom one's first. Connecting from those outer lines, not from each
    subtree's own winner line, lets winner names run into the empty space on their right,
    so each round only adds the connector's width and the tree stays narrow enough for
    Discord. The narrower side's line is extended with dashes so both connectors meet."""
    top_lines, top_width = top
    bottom_lines, bottom_width = bottom
    width = max(top_width, bottom_width)

    def extend(line: tuple[str, int], corner: str) -> tuple[str, int]:
        text, w = line
        return text + "─" * (width - w) + corner, width + 2

    own_text, own_width = own
    lines = top_lines[:-1] + [extend(top_lines[-1], "─┐")]
    lines.append((" " * (width + 1) + "├─" + own_text, width + 3 + own_width))
    lines += [extend(bottom_lines[0], "─┘")] + bottom_lines[1:]
    return lines, width + 2


def render_tree(
    matches_by_id: dict,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
) -> str:
    """Render the full connected bracket tree as a ```ansi fenced block, one
    color per round. A team with a bye is drawn as a single line joining the
    match where they play their first game, with no fake "vs" pairing. Byes
    that skip a round are also listed above the diagram so they're easy to
    spot at a glance.
    """
    # A bye straight into a team's first game (played round 0) skips nothing, so it isn't listed.
    byes = sorted(
        (m for m in matches_by_id.values() if is_bye(m) and m.played_round >= 1), key=lambda m: m.slot_index
    )
    bye_names = ", ".join(team_label(teams_by_id, bye_team_id(m)) for m in byes)

    root = final_match(matches_by_id)
    if is_bye(root):
        tree_lines, _ = _render_team(bye_team_id(root), root, teams_by_id, {})
    else:
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
    out_lines.extend(text for text, _ in tree_lines)
    out_lines.append("```")
    return "\n".join(out_lines)
