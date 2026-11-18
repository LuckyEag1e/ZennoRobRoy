"""Players reporting their own results by vote: which match a player can report on, and
when a vote the other side hasn't answered gets a public warning or wins. Shared by the
bot's background loop and the CLI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Optional

import db
from bracket import Match, resolvable_matches, resolve_all_slots, resolve_slot
from labels import advance_text, called_off_line, report_timed_out, result_line, team_label, walkover_line

# A vote claiming a win is warned about publicly after this long without an answer, and
# given once the other side has had this long to answer the warning.
WARNING_AFTER = timedelta(minutes=5)
ANSWER_WITHIN = timedelta(minutes=5)
# Nobody loses by a vote for the other side, so it gets no warning and just waits as long.
CONCESSION_AFTER = WARNING_AFTER + ANSWER_WITHIN


@dataclass
class DueStep:
    kind: str  # "warn" or "give"
    vote: sqlite3.Row  # from db.get_open_reports


def due_steps(conn: sqlite3.Connection, now: datetime) -> list[DueStep]:
    """The unanswered votes whose warning or win is due by `now`."""
    steps = []
    for match_id, votes in groupby(db.get_open_reports(conn), key=lambda vote: vote["match_id"]):
        votes = list(votes)
        if len(votes) != 1:
            continue
        vote = votes[0]
        post = db.get_match_post(conn, match_id)
        warned = post is not None and post["warned_at"] is not None
        if not warned and vote["winner_team_id"] == vote["team_id"]:
            if now >= db.from_db_time(vote["reported_at"]) + WARNING_AFTER:
                steps.append(DueStep("warn", vote))
        elif (not warned or warning_sent(post)) and now >= win_due_at(vote, post):
            steps.append(DueStep("give", vote))
    return steps


def win_due_at(vote: sqlite3.Row, post: Optional[sqlite3.Row]) -> datetime:
    """When the win an unanswered `vote` names is given, as things stand, where `post` is
    the match's post, if it has one. Once a warning is up, the other side has a set time
    from the votes' last change. Before then, a claim waits to be warned about first."""
    if post is not None and post["warned_at"] is not None:
        return answer_deadline(post)
    wait = CONCESSION_AFTER if vote["winner_team_id"] != vote["team_id"] else WARNING_AFTER + ANSWER_WITHIN
    return db.from_db_time(vote["reported_at"]) + wait


def warning_sent(post: sqlite3.Row) -> bool:
    """Whether the post's warning has gone out. A coordinated match's post already had a
    message before the warning, so having one isn't enough."""
    return post["message_id"] is not None and (post["pinged"] or "").startswith("warning:")


def answer_deadline(post: sqlite3.Row) -> datetime:
    """When the other side's time to answer a warned vote runs out."""
    return db.from_db_time(post["warned_at"]) + ANSWER_WITHIN


def warn(conn: sqlite3.Connection, vote: sqlite3.Row) -> None:
    """Start the warning about an unanswered vote, which the match's post then shows."""
    teams = db.match_now(conn, vote["tournament_id"], vote["match_id"]).teams
    db.claim_match_post(conn, vote["match_id"], vote["channel_id"], teams)
    db.warn_on_match_post(conn, vote["match_id"])


def opponent(teams: tuple[Optional[int], Optional[int]], team_id: int) -> Optional[int]:
    return teams[1] if teams[0] == team_id else teams[0]


def ending_text(conn: sqlite3.Connection, tournament: sqlite3.Row, match: Match) -> Optional[str]:
    """The public line for how `match` ended, or None if it hasn't."""
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    outcomes = db.get_outcomes(conn, tournament["id"])
    teams = resolve_slot(match, matches_by_id, outcomes.winners.get, feeders)
    winner = outcomes.winners.get(match.id)
    if winner is not None:
        advance = advance_text(match, winner, matches_by_id, feeders, outcomes, teams_by_id, tournament["name"])
        if match.id in outcomes.walkovers:
            return walkover_line(match, winner, teams_by_id, advance)
        return result_line(match, winner, opponent(teams, winner), teams_by_id, advance)
    if match.id in outcomes.skipped:
        return called_off_line(match, teams, teams_by_id)
    return None


def give_win(conn: sqlite3.Connection, vote: sqlite3.Row) -> Optional[str]:
    """Give the win an unanswered vote names. Returns the public line saying so, or None
    if the votes changed meanwhile and nothing was recorded."""
    if not db.give_reported_win(conn, vote["tournament_id"], vote):
        return None
    tournament = db.get_tournament(conn, vote["tournament_id"])
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    match = matches_by_id[vote["match_id"]]
    if vote["winner_team_id"] != vote["team_id"]:
        return ending_text(conn, tournament, match)  # the reporter conceded, so it's an ordinary result
    outcomes = db.get_outcomes(conn, tournament["id"])
    winner = vote["winner_team_id"]
    loser = opponent(resolve_slot(match, matches_by_id, outcomes.winners.get, feeders), winner)
    advance = advance_text(match, winner, matches_by_id, feeders, outcomes, teams_by_id, tournament["name"])
    return report_timed_out(match, winner, loser, teams_by_id, advance)


@dataclass
class ReportableMatch:
    """The match a player can report on: their side, the matchup, and their side's vote."""

    match: Match
    team_id: int
    teams: tuple[int, int]
    vote: Optional[sqlite3.Row]


def find_reportable_match(conn: sqlite3.Connection, tournament: sqlite3.Row, user_id: int) -> ReportableMatch | str:
    """The match waiting for a result that `user_id` plays in, or why they can't report one."""
    name = tournament["name"]
    if tournament["status"] != "in_progress":
        return f"**{name}** isn't taking results right now."
    team_id = db.get_entrant_team_id(conn, tournament["id"], user_id)
    team = db.get_teams(conn, tournament["id"]).get(team_id)
    if team is None:
        return f"You aren't an entrant in **{name}**."
    if team["withdrawn"]:
        return f"You've left **{name}**."
    matches_by_id, feeders, teams_by_id = db.load_bracket(conn, tournament["id"])
    outcomes = db.get_outcomes(conn, tournament["id"])
    ready = resolvable_matches(matches_by_id, feeders, outcomes.winners.get, outcomes.decided)
    slots = resolve_all_slots(matches_by_id, outcomes.winners.get, feeders)
    match = next((m for m in ready if team_id in slots[m.id]), None)
    if match is None:
        return f"You don't have a match waiting for a result in **{name}**."
    other = opponent(slots[match.id], team_id)
    if not db.team_member_ids(conn, other):
        # Their side could never answer a vote, so the timeout would hand over every such match.
        return f"**{team_label(teams_by_id, other)}** has no Discord account behind them, so an organizer records this match."
    call = db.get_call(conn, match.id)
    if tournament["match_slots"] is not None and (call is None or call["started_at"] is None):
        return (f"Your match in **{name}** hasn't started yet. Once it's called, both sides ready up, "
                f"and then you can report it.")
    vote = next((v for v in db.get_reports(conn, match.id) if v["team_id"] == team_id), None)
    return ReportableMatch(match, team_id, slots[match.id], vote)


def vote_reply(conn: sqlite3.Connection, outcome: str, found: ReportableMatch, winner_id: int,
               teams_by_id: dict) -> str:
    """What the voter is told privately, given what db.submit_report returned."""
    other = team_label(teams_by_id, opponent(found.teams, found.team_id))
    if outcome == "waiting":
        [vote] = db.get_reports(conn, found.match.id)
        # Discord shows this as "in 7 minutes", and keeps counting down.
        due = f"<t:{int(win_due_at(vote, db.get_match_post(conn, found.match.id)).timestamp())}:R>"
        if winner_id == found.team_id:
            reply = f"Saved. If **{other}** doesn't report, the win is yours {due}."
        else:
            reply = f"Saved. It's recorded once **{other}** agrees, or {due}."
        return reply + " Until then, you can change it or take it back with `/match report`."
    if outcome == "agreed":
        return f"✅ Both sides agree: **{team_label(teams_by_id, winner_id)}** won. It's recorded."
    if outcome == "skipped":
        return ("Both sides reported that the other won, so the match is called off with no winner. "
                "An organizer can still record the real result.")
    return "Your opponent reported the other way, so an organizer will record this one."
