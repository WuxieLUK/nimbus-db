"""The ``Database`` facade: open/create a database file, run SQL
statements, manage transactions.

Transaction model
-----------------
Autocommit: every statement runs in its own transaction (BEGIN, work,
WAL fsync, commit).  ``BEGIN`` starts an explicit transaction that
spans statements until ``COMMIT`` / ``ROLLBACK``.  A failed statement
in autocommit mode rolls back automatically; inside an explicit
transaction the transaction stays open so the client can ROLLBACK.

``Database(":memory:")`` opens a temporary file-backed database that is
deleted on close (durability without clutter).
"""
from __future__ import annotations
import os
import threading
from typing import List, Optional

from . import ast_nodes as A
from .catalog import Catalog
from .errors import NimbusError, OperationalError
from .executor import (Result, exec_create_index, exec_create_table,
                       exec_delete, exec_drop_index, exec_drop_table,
                       exec_insert, exec_select, exec_update,
                       explain_select)
from .parser import parse
from .pager import Pager


class Database:
    def __init__(self, path: str, page_size: int = 4096) -> None:
        self._temp_file: Optional[str] = None
        if path == ":memory:":
            import tempfile
            fd, self._temp_file = tempfile.mkstemp(prefix="nimbus_",
                                                   suffix=".ndb")
            os.close(fd)
            os.unlink(self._temp_file)
            path = self._temp_file
        self.path = path
        self.pager = Pager(path, page_size=page_size)
        self.catalog = Catalog(self.pager)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.pager.close()
        if self._temp_file and os.path.exists(self._temp_file):
            try:
                os.unlink(self._temp_file)
            except OSError:
                pass
            self._temp_file = None

    # ------------------------------------------------------------------ #

    def execute(self, sql: str) -> Result:
        stmt = parse(sql)
        return self.execute_statement(stmt)

    def execute_statement(self, stmt: object) -> Result:
        with self._lock:
            from .executor import _DB_STACK
            _DB_STACK.append(self)
            try:
                return self._dispatch(stmt)
            finally:
                _DB_STACK.pop()

    def _dispatch(self, stmt: object) -> Result:
        if isinstance(stmt, A.Begin):
            self.catalog.snapshot()
            self.pager.begin()
            return Result(command="BEGIN", message="transaction started")
        if isinstance(stmt, A.Commit):
            self.pager.commit()
            self.catalog._snapshot = None
            return Result(command="COMMIT", message="transaction committed")
        if isinstance(stmt, A.Rollback):
            self.pager.rollback()
            self.catalog.restore()
            return Result(command="ROLLBACK",
                          message="transaction rolled back")
        if isinstance(stmt, A.Explain):
            lines = self._explain(stmt.stmt)
            return Result(columns=["plan"], rows=[[l] for l in lines],
                          command="EXPLAIN")

        writes = not isinstance(stmt, A.Select)
        auto = not self.pager.in_txn
        if auto:
            self.catalog.snapshot()
            self.pager.begin()
        try:
            result = self._run(stmt)
        except BaseException:
            if auto:
                self.pager.rollback()
                self.catalog.restore()
            raise
        if auto:
            self.pager.commit()
            self.catalog._snapshot = None
        return result

    def _run(self, stmt: object) -> Result:
        if isinstance(stmt, A.Select):
            return exec_select(self, stmt)
        if isinstance(stmt, A.CreateTable):
            return exec_create_table(self, stmt)
        if isinstance(stmt, A.DropTable):
            return exec_drop_table(self, stmt)
        if isinstance(stmt, A.CreateIndex):
            return exec_create_index(self, stmt)
        if isinstance(stmt, A.DropIndex):
            return exec_drop_index(self, stmt)
        if isinstance(stmt, A.Insert):
            return exec_insert(self, stmt)
        if isinstance(stmt, A.Update):
            return exec_update(self, stmt)
        if isinstance(stmt, A.Delete):
            return exec_delete(self, stmt)
        raise OperationalError(
            f"unsupported statement: {type(stmt).__name__}")

    def _explain(self, stmt: object) -> List[str]:
        if isinstance(stmt, A.Select):
            return explain_select(self, stmt)
        if isinstance(stmt, A.Insert):
            return [f"INSERT INTO {stmt.table} ({len(stmt.rows)} row(s))"]
        if isinstance(stmt, A.Update):
            return [f"SCAN {stmt.table} + UPDATE ({len(stmt.sets)} set(s))"]
        if isinstance(stmt, A.Delete):
            return [f"SCAN {stmt.table} + DELETE"]
        return ["(statement has no plan)"]

    # ------------------------------------------------------------------ #

    @property
    def in_transaction(self) -> bool:
        return self.pager.in_txn

    def checkpoint(self) -> None:
        self.pager.checkpoint()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Database({self.pager.path!r})"
