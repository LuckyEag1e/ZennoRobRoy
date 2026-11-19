"""The CLI's side of 5v5 events, from sign-ups to the conclude post."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cli
import db
import lobby_db


@pytest.fixture
def repl(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CLI_DB_PATH", tmp_path / "cli.db")
    shell = cli.WinningTicketCLI()
    yield shell
    shell.conn.close()


def test_a_5v5_event_from_the_cli(repl, capsys, monkeypatch):
    repl.onecmd("regopen5v5 Duel --starts-in 24")
    for user_id in range(10, 21):
        db.record_agreement(repl.conn, cli.GUILD_ID, user_id)
        repl.onecmd(f"as {user_id} P{user_id}")
        repl.onecmd(f"register Duel --captain {'want' if user_id < 12 else 'no'}")
    out = capsys.readouterr().out
    assert "You're signed up for Duel" in out

    repl.onecmd("as 1 tester")
    repl.onecmd("regclose Duel --lobbies 9")
    assert "--lobbies 1: 5v5, 1 waitlisted" in capsys.readouterr().out
    repl.onecmd("regclose Duel")
    out = capsys.readouterr().out
    assert "Created the lobbies: 5v5, 1 waitlisted" in out
    assert "(5v5 post notifying" in out and "[Start Check-In]" in out

    repl.onecmd("bracket Duel")
    assert "5v5 event" in capsys.readouterr().out

    repl.onecmd("lobby_startcheckin Duel 5")
    tournament = db.get_tournament_by_name(repl.conn, cli.GUILD_ID, "Duel")
    lobby = lobby_db.get_lobbies(repl.conn, tournament["id"])[0]
    users = {team_id: members[0] for team_id, members in db.get_members_by_team(repl.conn, tournament["id"]).items()}
    for player in lobby.players:
        repl.onecmd(f"as {users[player]} P")
        repl.onecmd("checkin Duel")
    repl.conn.execute("UPDATE tournaments SET checkin_ends_at = ? WHERE id = ?",
                      (db.to_db_time(datetime.now(timezone.utc) - timedelta(seconds=1)), tournament["id"]))
    repl.conn.commit()
    capsys.readouterr()
    captain_a, captain_b = (users[lobby.teams[side].captain] for side in "AB")
    repl.onecmd(f"as {captain_a} CaptainA")
    assert "drafter.uma.guide/host" in capsys.readouterr().out
    repl.onecmd("lobby_roomcode Duel XYZ")
    assert "Saved room code XYZ" in capsys.readouterr().out

    answers = iter(["8" if player in lobby.teams["A"].players else "1" for player in lobby.players])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    repl.onecmd("lobby_report Duel")
    assert "Sent to the other captain" in capsys.readouterr().out
    repl.onecmd(f"as {captain_b} CaptainB")
    repl.onecmd("lobby_confirm Duel")
    out = capsys.readouterr().out
    assert "Confirmed" in out and "wins lobby 1" in out

    repl.onecmd("as 1 tester")
    repl.onecmd("conclude Duel")
    assert "How each lobby went" in capsys.readouterr().out
    repl.onecmd("history")
    assert "Duel: 11 players · 5v5 lobbies" in capsys.readouterr().out
    repl.onecmd("history_bracket Duel")
    assert "Lobby 1: 🏆" in capsys.readouterr().out
    repl.onecmd(f"rating {captain_a}")
    assert "5v5 contribution: 1" in capsys.readouterr().out
