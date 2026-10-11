"""nimbus-db CLI: a sqlite3-style shell.

Usage::

    python -m nimbus_db [DATABASE]        # interactive REPL
    echo "SELECT 1;" | python -m nimbus_db DATABASE

Dot commands: .tables  .schema [table]  .indexes [table]  .databases
              .stats  .help  .quit / .exit
"""
from __future__ import annotations

import os
import sys

from .db import Database
from .errors import NimbusError
from .executor import Result

HELP = """\
.dot commands:
  .tables              list tables
  .schema [TABLE]      show CREATE statements
  .indexes [TABLE]     list indexes
  .databases           show database file
  .stats               page statistics
  .help                this help
  .quit | .exit        leave the shell
Everything else is SQL; statements end with ';'."""


def _fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def print_result(res: Result) -> None:
    if res.command == "SELECT" or res.columns:
        if res.columns:
            widths = [len(c) for c in res.columns]
            str_rows = []
            for row in res.rows:
                cells = [_fmt(v) for v in row]
                for i, c in enumerate(cells):
                    widths[i] = max(widths[i], len(c))
                str_rows.append(cells)
            sep = "+".join("-" * (w + 2) for w in widths)
            head = "|".join(f" {c:<{w}} " for c, w in zip(res.columns, widths))
            print(sep)
            print(head)
            print(sep)
            for cells in str_rows:
                print("|".join(f" {c:<{w}} " for c, w in zip(cells, widths)))
            print(sep)
            n = len(res.rows)
            print(f"{'row' if n == 1 else 'rows'}: {n}")
        else:
            print("ok")
        return
    if res.message:
        print(res.message)
    else:
        print("ok")


class Shell:
    def __init__(self, db: Database) -> None:
        self.db = db

    def dot(self, line: str) -> bool:
        parts = line.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        if cmd in (".quit", ".exit"):
            return False
        if cmd == ".help":
            print(HELP)
        elif cmd == ".tables":
            res = self.db.execute(
                "SELECT name FROM nimbus_master WHERE type = 'table' "
                "ORDER BY name")
            for row in res.rows:
                print(row[0])
        elif cmd == ".schema":
            sql = ("SELECT sql FROM nimbus_master WHERE sql != '' "
                   "ORDER BY name")
            res = self.db.execute(sql)
            for row in res.rows:
                if arg is None or arg.lower() in str(row[0]).lower():
                    print(str(row[0]) + ";")
        elif cmd == ".indexes":
            sql = ("SELECT name, tbl_name FROM nimbus_master "
                   "WHERE type = 'index' ORDER BY name")
            res = self.db.execute(sql)
            for row in res.rows:
                if arg is None or arg.lower() == str(row[1]).lower():
                    print(row[0])
        elif cmd == ".databases":
            print(self.db.pager.path)
        elif cmd == ".stats":
            p = self.db.pager
            print(f"pages: {p.num_pages}  page_size: {p.page_size}  "
                  f"in_txn: {p.in_txn}")
        else:
            print(f"unknown command: {cmd} (try .help)")
        return True

    def run_line(self, sql: str) -> None:
        res = self.db.execute(sql)
        print_result(res)


def repl(db: Database) -> int:
    shell = Shell(db)
    print(f"nimbus-db v{__import__('nimbus_db').__version__} "
          f"-- enter '.help' for help")
    buffer: list = []
    while True:
        prompt = "nimbus> " if not buffer else "  ...> "
        try:
            line = input(prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            buffer = []
            continue
        stripped = line.strip()
        if not buffer and stripped.startswith("."):
            if not shell.dot(stripped):
                break
            continue
        if not stripped and not buffer:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            sql = "\n".join(buffer)
            buffer = []
            try:
                shell.run_line(sql)
            except NimbusError as e:
                print(f"Error: {e}")
            except BrokenPipeError:
                break
    return 0


def one_shot(db: Database) -> int:
    sql = sys.stdin.read()
    if not sql.strip():
        return 0
    try:
        from .parser import parse_script
        for stmt in parse_script(sql):
            print_result(db.execute_statement(stmt))
        return 0
    except NimbusError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    path = argv[0] if argv else ":memory:"
    db = Database(path)
    try:
        if sys.stdin.isatty():
            return repl(db)
        return one_shot(db)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
