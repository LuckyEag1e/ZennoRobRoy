import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ascii_bracket import prediction_marker, real_result_marker, render_tree
from bracket import Match, build_feeder_index, resolve_all_slots

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _line_for(text: str, substring: str) -> str:
    """The raw (ANSI-included) line whose stripped content contains substring."""
    for line in text.splitlines():
        if substring in _strip_ansi(line):
            return line
    raise AssertionError(f"{substring!r} not found in any line")


def _is_bold(line: str) -> bool:
    return bool(re.search(r"\x1b\[1;\d+m", line))


def _is_underline(line: str) -> bool:
    return bool(re.search(r"\x1b\[4;\d+m", line))


def _is_plain_colored(line: str) -> bool:
    return bool(re.search(r"\x1b\[\d+m", line)) and not _is_bold(line) and not _is_underline(line)


def _is_uncolored(line: str) -> bool:
    return "\x1b[" not in line


def _color_code(line: str) -> str:
    m = re.search(r"\x1b\[(?:[14];)?(\d+)m", line)
    assert m, f"no ANSI color found in {line!r}"
    return m.group(1)


def _team(name, seed=None):
    return {"name": name, "seed": seed}


def _4_team_bracket():
    m1 = Match(id=1, round=1, slot_index=0, team_a_id=1, team_b_id=2,
               feeds_into_match_id=3, feeds_into_side="A")
    m2 = Match(id=2, round=1, slot_index=1, team_a_id=3, team_b_id=4,
               feeds_into_match_id=3, feeds_into_side="B")
    m3 = Match(id=3, round=2, slot_index=0)
    matches = {1: m1, 2: m2, 3: m3}
    teams = {1: _team("Alpha"), 2: _team("Beta"), 3: _team("Gamma"), 4: _team("Delta")}
    return matches, teams


def _8_team_bracket_with_byes():
    # round1: m1 (Ash vs Birch), m2 (Cedar bye), m3 (Dogwood vs Elm), m4 (Fir bye)
    m1 = Match(id=1, round=1, slot_index=0, team_a_id=1, team_b_id=2, feeds_into_match_id=5, feeds_into_side="A")
    m2 = Match(id=2, round=1, slot_index=1, team_a_id=3, team_b_id=None, feeds_into_match_id=5, feeds_into_side="B")
    m3 = Match(id=3, round=1, slot_index=2, team_a_id=4, team_b_id=5, feeds_into_match_id=6, feeds_into_side="A")
    m4 = Match(id=4, round=1, slot_index=3, team_a_id=6, team_b_id=None, feeds_into_match_id=6, feeds_into_side="B")
    m5 = Match(id=5, round=2, slot_index=0, feeds_into_match_id=7, feeds_into_side="A")
    m6 = Match(id=6, round=2, slot_index=1, feeds_into_match_id=7, feeds_into_side="B")
    m7 = Match(id=7, round=3, slot_index=0)
    matches = {1: m1, 2: m2, 3: m3, 4: m4, 5: m5, 6: m6, 7: m7}
    teams = {
        1: _team("Ash"), 2: _team("Birch"), 3: _team("Cedar"),
        4: _team("Dogwood"), 5: _team("Elm"), 6: _team("Fir"),
    }
    return matches, teams


def _render(matches, teams, results=None, picks=None, mode="real"):
    feeders = build_feeder_index(list(matches.values()))
    results = results or {}
    if mode == "real":
        slots = resolve_all_slots(matches, lambda mid: results.get(mid), feeders)
        marker = real_result_marker(results)
        winner_of = lambda mid: results.get(mid)  # noqa: E731
    else:
        picks = picks or {}
        slots = resolve_all_slots(matches, lambda mid: picks.get(mid), feeders)
        marker = prediction_marker(results, picks)
        winner_of = lambda mid: picks.get(mid)  # noqa: E731
    return render_tree(matches, feeders, teams, slots, winner_of, marker)


def test_render_tree_contains_all_team_names_and_is_an_ansi_block():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams)
    stripped = _strip_ansi(text)

    assert "Alpha" in stripped
    assert "Beta" in stripped
    assert "Gamma" in stripped
    assert "Delta" in stripped
    assert "```ansi" in text
    assert text.rstrip().endswith("```")


def test_render_tree_draws_a_connector_between_rounds():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams)

    assert "┐" in text
    assert "┘" in text
    assert "├─" in text


def test_render_tree_winner_is_bold_loser_is_underlined_once_decided():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1})  # Alpha beats Beta

    alpha_line = _line_for(text, "+Alpha")
    beta_line = _line_for(text, "-Beta")
    assert _is_bold(alpha_line)
    assert _is_underline(beta_line)


def test_render_tree_undecided_leaf_is_plain_colored_not_bold():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams)

    alpha_line = _line_for(text, "Alpha")
    assert _is_plain_colored(alpha_line)
    assert not _is_bold(alpha_line)
    assert not _is_underline(alpha_line)


def test_render_tree_undecided_merged_result_shows_tbd_uncolored():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams)
    stripped = _strip_ansi(text)
    assert "TBD" in stripped
    tbd_line = _line_for(text, "TBD")
    assert _is_uncolored(tbd_line)


def test_render_tree_round1_and_round2_use_different_colors():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1, 2: 3, 3: 1})  # fully decided

    round1_line = _line_for(text, "+Alpha")
    champion_line = _line_for(text, "├─")
    assert _color_code(round1_line) != _color_code(champion_line)


def test_render_tree_decided_final_shows_champion_name_bold():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1, 2: 3, 3: 1})
    champion_line = _line_for(text, "├─")
    assert "Alpha" in _strip_ansi(champion_line)
    assert _is_bold(champion_line)


def test_render_tree_byes_shown_in_summary_and_as_single_line_in_tree():
    matches, teams = _8_team_bracket_with_byes()
    text = _render(matches, teams)

    assert "Byes (round 1): Cedar, Fir" in text
    tree_body = text.split("```ansi")[1]
    cedar_line = _line_for(tree_body, "Cedar")
    assert "vs" not in _strip_ansi(cedar_line)
    assert _is_bold(cedar_line)  # byes always auto-advance


def test_render_tree_round2_shows_bye_advanced_team_normally():
    matches, teams = _8_team_bracket_with_byes()
    text = _render(matches, teams, results={1: 1, 3: 4})  # Ash beats Birch, Dogwood beats Elm
    assert "Cedar" in _strip_ansi(text)


def test_prediction_marker_colors_only_the_users_pick():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1}, picks={1: 2}, mode="predict")

    beta_line = _line_for(text, "Beta")
    alpha_line = _line_for(text, "Alpha")
    assert "-Beta" in _strip_ansi(beta_line)  # their pick, wrong
    assert _is_underline(beta_line)
    assert _is_plain_colored(alpha_line)  # not their pick -- no bold/underline verdict


def test_prediction_marker_highlights_pick_before_any_result_exists():
    # Before the real tournament has started, every pick should still stand
    # out so the user can see their own bracket path -- there's just nothing
    # to grade as right/wrong yet, so it's bold but not underlined-wrong.
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={}, picks={1: 1}, mode="predict")

    alpha_line = _line_for(text, "Alpha")
    beta_line = _line_for(text, "Beta")
    assert "+Alpha" in _strip_ansi(alpha_line)
    assert _is_bold(alpha_line)
    assert _is_plain_colored(beta_line)  # not picked, stays un-bolded


def test_render_tree_shows_final_matchup_summary_when_not_yet_decided():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1, 2: 3})  # semis decided, final not played
    assert "Final: Alpha vs Gamma" in text
    assert "Champion:" not in text


def test_render_tree_shows_champion_summary_once_final_decided():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={1: 1, 2: 3, 3: 1})  # Alpha wins it all
    assert "Champion: Alpha" in text
    assert "Final:" not in text


def test_render_tree_no_final_summary_before_semifinals_decided():
    matches, teams = _4_team_bracket()
    text = _render(matches, teams, results={})
    assert "Final:" not in text
    assert "Champion:" not in text
