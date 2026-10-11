import io
import sys

import pytest

from nimbus_db import Database
from nimbus_db.cli import main, print_result, Shell
from nimbus_db.executor import Result


class TestPrintResult:
    def test_select_table(self, capsys):
        print_result(Result(columns=["a", "b"], rows=[[1, "x"], [None, None]]))
        out = capsys.readouterr().out
        assert "a" in out and "b" in out
        assert "NULL" in out
        assert "rows: 2" in out

    def test_message(self, capsys):
        print_result(Result(command="INSERT", message="inserted 1 row(s)"))
        assert "inserted 1 row(s)" in capsys.readouterr().out


class TestShell:
    def test_dot_tables(self, capsys):
        db = Database(":memory:")
        db.execute("CREATE TABLE alpha (a INT)")
        db.execute("CREATE TABLE beta (b INT)")
        shell = Shell(db)
        assert shell.dot(".tables")
        out = capsys.readouterr().out
        assert "alpha" in out and "beta" in out
        db.close()

    def test_dot_schema(self, capsys):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (a INT PRIMARY KEY)")
        shell = Shell(db)
        shell.dot(".schema t")
        out = capsys.readouterr().out
        assert "CREATE TABLE t" in out
        db.close()

    def test_run_line(self, capsys):
        db = Database(":memory:")
        shell = Shell(db)
        shell.run_line("SELECT 1 + 1 AS two")
        assert "two" in capsys.readouterr().out
        db.close()


class TestMain:
    def test_one_shot(self, tmp_path, monkeypatch, capsys):
        path = str(tmp_path / "cli.ndb")
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            "CREATE TABLE t (v INT); INSERT INTO t VALUES (42);"))
        assert main([path]) == 0
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            "SELECT v FROM t"))
        assert main([path]) == 0
        assert "42" in capsys.readouterr().out

    def test_one_shot_error_exit_code(self, tmp_path, monkeypatch):
        path = str(tmp_path / "cli2.ndb")
        monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT * FROM nope"))
        assert main([path]) == 1
