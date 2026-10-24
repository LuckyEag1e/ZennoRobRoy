import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from csv_import import CsvValidationError, parse_csv


def test_parse_valid_csv_4_teams():
    content = "team_a,team_b\nTeam A,Team B\nTeam C,Team D\n"
    rows = parse_csv(content)
    assert len(rows) == 2
    assert rows[0]["team_a"] == "Team A"
    assert rows[0]["team_b"] == "Team B"
    assert rows[0]["seed_a"] is None


def test_parse_valid_csv_with_seeds():
    content = "team_a,seed_a,team_b,seed_b\nTeam A,1,Team B,8\nTeam C,4,Team D,5\n"
    rows = parse_csv(content)
    assert rows[0]["seed_a"] == 1
    assert rows[0]["seed_b"] == 8


def test_parse_rejects_non_power_of_two_row_count():
    content = "team_a,team_b\n" + "\n".join(f"T{i}a,T{i}b" for i in range(3))
    with pytest.raises(CsvValidationError):
        parse_csv(content)


def test_parse_allows_blank_team_b_as_a_bye():
    content = "team_a,team_b\nTeam A,\nTeam C,Team D\n"
    rows = parse_csv(content)
    assert rows[0]["team_a"] == "Team A"
    assert rows[0]["team_b"] is None


def test_parse_allows_blank_team_a_as_a_bye():
    content = "team_a,team_b\n,Team B\nTeam C,Team D\n"
    rows = parse_csv(content)
    assert rows[0]["team_a"] is None
    assert rows[0]["team_b"] == "Team B"


def test_parse_rejects_both_sides_blank():
    content = "team_a,team_b\n,\nTeam C,Team D\n"
    with pytest.raises(CsvValidationError):
        parse_csv(content)


def test_parse_rejects_duplicate_team_names():
    content = "team_a,team_b\nTeam A,Team B\nTeam A,Team D\n"
    with pytest.raises(CsvValidationError):
        parse_csv(content)


def test_parse_rejects_missing_required_columns():
    content = "home,away\nTeam A,Team B\n"
    with pytest.raises(CsvValidationError):
        parse_csv(content)


def test_parse_rejects_empty_file():
    with pytest.raises(CsvValidationError):
        parse_csv("")


def test_parse_rejects_header_only_no_rows():
    with pytest.raises(CsvValidationError):
        parse_csv("team_a,team_b\n")
