"""The CLI's side of match coordination: rooms, calls and readying up, as printed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import cli
import db


@pytest.fixture
def repl(tmp_path, monkeypatch):
    """A CLI on its own database, with a locked tournament "Cup" (Ace vs Blaze, then Cyan
    vs Dune) whose matches are called one at a time into one room, every entrant linked."""
    monkeypatch.setattr(cli, "CLI_DB_PATH", tmp_path / "cli.db")
    csv_path = tmp_path / "cup.csv"
    csv_path.write_text("team_a,team_b\nAce,Blaze\nCyan,Dune\n")
    shell = cli.WinningTicketCLI()
    shell.onecmd("admin_matchrooms 1")
    shell.onecmd(f"create Cup {csv_path} --match-slots 1")
    tournament = db.get_tournament_by_name(shell.conn, cli.GUILD_ID, "Cup")
    for user_id, (team_id, _) in zip(range(10, 14), sorted(db.get_teams(shell.conn, tournament["id"]).items())):
        db.record_agreement(shell.conn, cli.GUILD_ID, user_id)
        db.link_team_member(shell.conn, tournament["id"], team_id, user_id)
    shell.onecmd("open Cup")
    shell.onecmd("lock Cup")
    yield shell
    shell.conn.close()


def test_readying_up_from_the_cli_starts_the_match_in_a_room(repl, capsys):
    capsys.readouterr()
    repl.onecmd("as 10 Ace")
    out = capsys.readouterr().out
    assert "(match post) 🎮 Round 1: **Ace** vs **Blaze** is up!" in out and "[Ready Up]" in out

    repl.onecmd("ready Cup")
    assert "You're ready. Waiting for your opponent." in capsys.readouterr().out
    repl.onecmd("as 11 Blaze")
    repl.onecmd("ready Cup")
    out = capsys.readouterr().out
    assert "The match is starting." in out
    assert "▶️ Round 1: **Ace** vs **Blaze**." in out

    repl.onecmd("as 12 Cyan")
    repl.onecmd("ready Cup")
    assert "You don't have a called match waiting for you to ready up." in capsys.readouterr().out


def test_the_cli_refuses_more_places_than_rooms(repl, capsys):
    capsys.readouterr()
    repl.onecmd("admin_matchslots Cup 2")
    assert "There are only 1 room(s), so at most 1 matches can be called at once." in capsys.readouterr().out
