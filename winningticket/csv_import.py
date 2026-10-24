"""CSV parsing/validation and tournament import orchestration."""

from __future__ import annotations

import csv
import io
import sqlite3

import db
from bracket import BracketError, check_matchup_count, generate_tree


class CsvValidationError(ValueError):
    pass


def _parse_int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CsvValidationError(f"'{value}' is not a valid integer for {field}")


def parse_csv(content: str) -> list[dict]:
    """Parse and validate CSV text into a list of row dicts with keys
    team_a, team_b (either may be None for a bye -- not both), seed_a
    (optional), seed_b (optional).
    """
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise CsvValidationError("CSV file is empty or has no header row")

    fieldnames = {f.strip() for f in reader.fieldnames}
    required = {"team_a", "team_b"}
    missing = required - fieldnames
    if missing:
        raise CsvValidationError(
            f"CSV is missing required column(s): {', '.join(sorted(missing))}"
        )
    has_seeds = "seed_a" in fieldnames and "seed_b" in fieldnames

    rows: list[dict] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        team_a = (raw.get("team_a") or "").strip() or None
        team_b = (raw.get("team_b") or "").strip() or None
        if team_a is None and team_b is None:
            raise CsvValidationError(
                f"Row {i}: team_a and team_b can't both be blank "
                f"(leave exactly one blank for a bye, not both)"
            )
        for name in (team_a, team_b):
            if name is None:
                continue
            if name in seen_names:
                raise CsvValidationError(f"Duplicate team name '{name}' found in CSV")
            seen_names.add(name)

        seed_a = seed_b = None
        if has_seeds:
            raw_seed_a = (raw.get("seed_a") or "").strip()
            raw_seed_b = (raw.get("seed_b") or "").strip()
            if raw_seed_a:
                seed_a = _parse_int(raw_seed_a, f"seed_a (row {i})")
            if raw_seed_b:
                seed_b = _parse_int(raw_seed_b, f"seed_b (row {i})")

        rows.append({"team_a": team_a, "team_b": team_b, "seed_a": seed_a, "seed_b": seed_b})

    if not rows:
        raise CsvValidationError("CSV has a header but no data rows")

    try:
        check_matchup_count(len(rows))
    except BracketError as e:
        raise CsvValidationError(str(e))

    return rows


def import_tournament(conn: sqlite3.Connection, guild_id: int, name: str,
                       created_by: int, rows: list[dict]) -> int:
    """Create the tournament, its teams, and the full match tree from parsed CSV rows."""
    tournament_id = db.create_tournament(conn, guild_id, name, created_by)

    team_pairs: list[tuple[int | None, int | None]] = []
    for row in rows:
        team_a_id = (
            db.insert_team(conn, tournament_id, row["team_a"], row["seed_a"])
            if row["team_a"] is not None else None
        )
        team_b_id = (
            db.insert_team(conn, tournament_id, row["team_b"], row["seed_b"])
            if row["team_b"] is not None else None
        )
        team_pairs.append((team_a_id, team_b_id))

    try:
        specs = generate_tree(team_pairs)
    except BracketError as e:
        db.delete_tournament(conn, tournament_id)
        raise CsvValidationError(str(e))

    match_ids_by_round_slot: dict[tuple[int, int], int] = {}
    for spec in specs:
        if spec.round == 1:
            team_a_id, team_b_id = team_pairs[spec.slot_index]
        else:
            team_a_id, team_b_id = None, None
        match_id = db.insert_match_spec(conn, tournament_id, spec, team_a_id, team_b_id)
        match_ids_by_round_slot[(spec.round, spec.slot_index)] = match_id
    conn.commit()

    db.link_feeders(conn, tournament_id, specs, match_ids_by_round_slot)

    return tournament_id
