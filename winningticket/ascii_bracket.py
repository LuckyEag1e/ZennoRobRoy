"""Plain-text bracket tree rendering, for Discord ANSI-coloring and the CLI.

No discord.py dependency -- just builds strings, so it's usable from both the
bot and cli.py, and unit-testable like bracket.py.

Colors use Discord's ```ansi fenced code block support (real ANSI SGR escape
codes), giving each round its own distinct color, plus bold for a winner and
underline for a confirmed-wrong pick -- the literal +/- markers stay in the
text too as a fallback in case ANSI doesn't render on some client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from bracket import (
    Match,
    WinnerLookup,
    bye_team_id,
    byes_in_round,
    effective_winner,
    final_match,
    is_bye,
    rounds,
)
from labels import team_label, team_name

# (match, team_a_id, team_b_id) -> {team_id: '+' or '-'} for whichever teams
# in this match should be marked; teams not present render unmarked.
LineMarker = Callable[[Match, Optional[int], Optional[int]], dict]

# Every name slot is padded or truncated to this width. Each round repeats
# names from the round before, so uncapped long names widen every later
# round until Discord wraps the lines and breaks the diagram.
NAME_SLOT_WIDTH = 10
# A winner named on a junction adds their name's width to every later round, so those
# names are cut much shorter than the entrants listed down the left, and leave out the
# seed, which the entrant's own line already shows.
JUNCTION_NAME_WIDTH = 5

# Writing each winner where their line joins reads best, but it adds a name's width per
# round. A bracket of up to 16 lines stays within about 44 characters wide and 1500 long
# even with every name at full width; a bigger one would wrap on a phone or overflow an
# embed, so it lists winners in a narrow column on the right instead. Going by the
# bracket's size, not its current names, keeps the layout the same all tournament.
INLINE_MAX_ROUNDS = 4

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


def _fit(text: str, width: int = NAME_SLOT_WIDTH) -> str:
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


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


@dataclass
class _Row:
    text: str
    width: int  # visible width; the ANSI escape bytes in `text` would throw off len()
    label: str = ""  # a game's winner, when winners are shown in a column right of the tree


# A rendered subtree: its rows, the row its parent's connector attaches to, and the
# width of that row.
Rendered = tuple[list[_Row], int, int]


def _render_team(team_id: Optional[int], match: Match, teams_by_id: dict, marks: dict) -> Rendered:
    """One entrant, marked and coloured by `match`, the first game they play."""
    mark = marks.get(team_id, " ")
    plain = f"{mark}{_fit(team_label(teams_by_id, team_id))}"
    return [_Row(_colorize(plain, match.played_round, mark), len(plain))], 0, len(plain)


def _render_match(
    match: Match,
    matches_by_id: dict,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
    inline: bool,
) -> Rendered:
    """A real (non-bye) game: its two sides joined at a junction row for its winner."""
    team_a, team_b = slots[match.id]
    marks = marker_fn(match, team_a, team_b)

    def render_side(side: str, team_id: Optional[int]) -> Rendered:
        feeder = feeders_by_target.get(match.id, {}).get(side)
        if feeder is None or is_bye(feeder):
            return _render_team(team_id, match, teams_by_id, marks)
        return _render_match(feeder, matches_by_id, feeders_by_target, teams_by_id, slots, winner_of, marker_fn, inline)

    winner = winner_of(match.id)
    if winner is None:
        label, label_width = " TBD", 4  # nothing decided yet -- no color
    else:
        # Coloured by the round the winner plays next, like the teams in that game.
        next_match = matches_by_id.get(match.feeds_into_match_id)
        color_round = next_match.played_round if next_match is not None else match.played_round + 1
        mark = marks.get(winner, " ")
        # Truncated like team names but not padded: the connector to the next game, or the
        # end of the line, follows straight on.
        name = _fit(team_name(teams_by_id, winner), JUNCTION_NAME_WIDTH) if inline else _fit(team_label(teams_by_id, winner))
        plain = f"{mark}{name.rstrip()}"
        label, label_width = _colorize(plain, color_round, mark), len(plain)

    return _join(render_side("A", team_a), render_side("B", team_b), label, label_width, inline)


def _join(top: Rendered, bottom: Rendered, label: str, label_width: int, inline: bool) -> Rendered:
    """Stack two subtrees with a junction row between them, and draw connectors from each
    subtree's attachment row to the junction. `inline` writes the winner on the junction
    row, and the parent game's connector carries on from the end of their name; otherwise
    the winner is kept on the row for the column to the right of the tree."""
    top_rows, top_anchor, top_width = top
    bottom_rows, bottom_anchor, bottom_width = bottom
    column = max(top_width, bottom_width) + 1

    def connect(rows: list[_Row], anchor: int, corner: str, rows_needing_bar: range) -> list[_Row]:
        joined = []
        for i, row in enumerate(rows):
            if i == anchor:
                joined.append(_Row(row.text + "─" * (column - row.width) + corner, column + 1, row.label))
            elif i in rows_needing_bar:
                joined.append(_Row(row.text + " " * (column - row.width) + "│", column + 1, row.label))
            else:
                joined.append(row)
        return joined

    rows = connect(top_rows, top_anchor, "┐", range(top_anchor + 1, len(top_rows)))
    anchor = len(rows)
    junction = _Row(" " * column + "├" + label, column + 1 + label_width) if inline else _Row(" " * column + "├", column + 1, label)
    rows.append(junction)
    rows += connect(bottom_rows, bottom_anchor, "┘", range(bottom_anchor))
    return rows, anchor, junction.width


def _bye_summary(matches_by_id: dict, teams_by_id: dict, slots: dict, winner_of: WinnerLookup) -> list[str]:
    """One line per round that has byes, naming whoever sits it out, or TBD until that's known."""
    lines = []
    for round_num in rounds(matches_by_id):
        names = []
        for match in byes_in_round(matches_by_id, round_num):
            team = effective_winner(match, winner_of)
            team_a, team_b = slots[match.id]
            if team is not None:
                names.append(team_label(teams_by_id, team))
            elif team_a is not None and team_b is not None:
                names.append(f"winner of {team_label(teams_by_id, team_a)} vs {team_label(teams_by_id, team_b)}")
            else:
                names.append("TBD")
        if names:
            lines.append(f"Byes (round {round_num}): {', '.join(names)}")
    return lines


def render_tree(
    matches_by_id: dict,
    feeders_by_target: dict,
    teams_by_id: dict,
    slots: dict,
    winner_of: WinnerLookup,
    marker_fn: LineMarker,
) -> str:
    """Render the full connected bracket tree as a ```ansi fenced block, one color per
    round. Each game is a junction row naming its winner, whose line carries on from there
    to their next game; a bracket too big for that lists winners in a column on the right.
    A team with a bye is a single line joining the game where they first play. Byes are
    also listed above the diagram so they're easy to spot at a glance.
    """
    root = final_match(matches_by_id)

    def tree_rows(inline: bool) -> list[_Row]:
        if is_bye(root):
            return _render_team(bye_team_id(root), root, teams_by_id, {})[0]
        return _render_match(root, matches_by_id, feeders_by_target, teams_by_id, slots, winner_of, marker_fn, inline)[0]

    if root.round <= INLINE_MAX_ROUNDS:
        lines = [row.text for row in tree_rows(inline=True)]
    else:
        rows = tree_rows(inline=False)
        label_column = max(row.width for row in rows) + 1
        lines = [row.text + " " * (label_column - row.width) + row.label if row.label else row.text for row in rows]

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

    out_lines = _bye_summary(matches_by_id, teams_by_id, slots, winner_of)
    if final_line:
        out_lines.append(final_line)
    if out_lines:
        out_lines.append("")
    out_lines.append("```ansi")
    out_lines.extend(lines)
    out_lines.append("```")
    return "\n".join(out_lines)
