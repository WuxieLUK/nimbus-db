"""Catalog: table & index metadata, persisted in the ``nimbus_schema``
table (a regular B-tree whose root lives in the database header).

Schema rows (same shape as sqlite_master plus an ``auto`` flag)::

    [type TEXT, name TEXT, tbl_name TEXT, rootpage INT, sql TEXT, auto INT]

``Catalog.schema_rows`` keeps the raw rows for the ``nimbus_master``
pseudo-table and the ``.schema`` command.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .ast_nodes import CreateTable
from .btree import BTree
from .errors import SchemaError
from .pager import Pager
from .values import affinity_of, deserialize_row, serialize_row

ROWID_NAMES = ("rowid", "_rowid_", "oid")


@dataclass
class ColumnMeta:
    name: str
    type_decl: str
    affinity: str
    not_null: bool = False
    pk: bool = False
    unique: bool = False
    default_expr: object = None      # AST expression or None


@dataclass
class TableSchema:
    name: str
    root: int
    columns: List[ColumnMeta]
    pk_cols: List[str] = field(default_factory=list)
    unique_sets: List[List[str]] = field(default_factory=list)

    def col_index(self, name: str) -> int:
        low = name.lower()
        for i, c in enumerate(self.columns):
            if c.name.lower() == low:
                return i
        raise SchemaError(f"no such column: {name}")

    def has_column(self, name: str) -> bool:
        low = name.lower()
        return any(c.name.lower() == low for c in self.columns)

    @property
    def rowid_alias(self) -> Optional[str]:
        """A single-column INTEGER PRIMARY KEY aliases the rowid."""
        if len(self.pk_cols) == 1:
            col = self.columns[self.col_index(self.pk_cols[0])]
            if col.affinity == "INTEGER":
                return col.name
        return None


@dataclass
class IndexSchema:
    name: str
    table: str
    columns: List[str]
    root: int
    unique: bool = False
    auto: bool = False


class Catalog:
    def __init__(self, pager: Pager) -> None:
        self.pager = pager
        self.tables: Dict[str, TableSchema] = {}
        self.indexes: Dict[str, IndexSchema] = {}
        self.schema_rows: List[List] = []      # [type,name,tbl_name,root,sql,auto]
        self._next_auto_seq: Dict[str, int] = {}
        self._snapshot = None
        self.load()

    # ------------------------------------------------------------------ #
    # transaction support: the pager restores pages on ROLLBACK, so the
    # in-memory catalog must be restored the same way
    # ------------------------------------------------------------------ #

    def snapshot(self) -> None:
        self._snapshot = copy.deepcopy(
            (self.tables, self.indexes, self.schema_rows, self._next_auto_seq))

    def restore(self) -> None:
        if self._snapshot is not None:
            self.tables, self.indexes, self.schema_rows, self._next_auto_seq \
                = copy.deepcopy(self._snapshot)

    # ------------------------------------------------------------------ #

    def load(self) -> None:
        if not self.pager.schema_root:
            return
        tree = BTree(self.pager, self.pager.schema_root, "table")
        for _rowid, payload in tree.iter_table():
            row = deserialize_row(payload)
            if len(row) < 5:
                continue
            self.schema_rows.append(row)
            kind, name, tbl_name, root = row[0], row[1], row[2], row[3]
            auto = bool(row[5]) if len(row) > 5 else False
            if kind == "table":
                self._load_table_from_sql(name, root, row[4])
            elif kind == "index":
                self.indexes[name.lower()] = IndexSchema(
                    name=name, table=tbl_name, columns=self._index_cols(row[4]),
                    root=root, unique=bool(row[6]) if len(row) > 6 else False,
                    auto=auto)

    def _load_table_from_sql(self, name: str, root: int, sql: str) -> None:
        # tables are stored with their full DDL; re-parse to rebuild metadata
        from .parser import parse
        stmt = parse(sql)
        ts = table_schema_from_ddl(stmt, root)
        self.tables[ts.name.lower()] = ts

    @staticmethod
    def _index_cols(sql: str) -> List[str]:
        # stored CREATE INDEX sql: "... ON table (col1, col2)"
        inner = sql[sql.rfind("(") + 1: sql.rfind(")")]
        return [c.strip().strip('"`') for c in inner.split(",") if c.strip()]

    # ------------------------------------------------------------------ #

    def table(self, name: str) -> TableSchema:
        t = self.tables.get(name.lower())
        if t is None:
            raise SchemaError(f"no such table: {name}")
        return t

    def table_or_none(self, name: str) -> Optional[TableSchema]:
        return self.tables.get(name.lower())

    def table_indexes(self, table: str) -> List[IndexSchema]:
        low = table.lower()
        return [ix for ix in self.indexes.values() if ix.table.lower() == low]

    def index(self, name: str) -> IndexSchema:
        ix = self.indexes.get(name.lower())
        if ix is None:
            raise SchemaError(f"no such index: {name}")
        return ix

    # ------------------------------------------------------------------ #
    # schema-row persistence
    # ------------------------------------------------------------------ #

    def _schema_tree(self) -> BTree:
        if not self.pager.schema_root:
            tree = BTree.create_root(self.pager, "table")
            self.pager.set_schema_root(tree.root)
            return tree
        return BTree(self.pager, self.pager.schema_root, "table")

    def _sync_schema_root(self, tree: BTree) -> None:
        if tree.root != self.pager.schema_root:
            self.pager.set_schema_root(tree.root)

    def write_schema_row(self, row: List) -> int:
        tree = self._schema_tree()
        rowid = tree.max_rowid() + 1
        tree.insert(rowid, serialize_row(row))
        self._sync_schema_root(tree)
        self.schema_rows.append(row)
        return rowid

    def delete_schema_row(self, kind: str, name: str) -> None:
        tree = self._schema_tree()
        removed = None
        for rowid, payload in list(tree.iter_table()):
            row = deserialize_row(payload)
            if row[0] == kind and str(row[1]).lower() == name.lower():
                tree.delete(rowid)
                removed = row
                break
        self._sync_schema_root(tree)
        if removed is None:
            raise SchemaError(f"schema row for {kind} '{name}' not found")
        self.schema_rows = [r for r in self.schema_rows
                            if not (r[0] == kind
                                    and str(r[1]).lower() == name.lower())]

    def update_root(self, kind: str, name: str, new_root: int) -> None:
        """Persist a new root page for a table/index after a split or
        collapse, and refresh the in-memory metadata."""
        tree = self._schema_tree()
        low = name.lower()
        for rowid, payload in list(tree.iter_table()):
            row = deserialize_row(payload)
            if row[0] == kind and str(row[1]).lower() == low:
                row[3] = new_root
                tree.delete(rowid)
                tree.insert(rowid, serialize_row(row))
                break
        else:
            raise SchemaError(f"schema row for {kind} '{name}' not found")
        self._sync_schema_root(tree)
        for r in self.schema_rows:
            if r[0] == kind and str(r[1]).lower() == low:
                r[3] = new_root
        if kind == "table":
            ts = self.tables.get(low)
            if ts is not None:
                ts.root = new_root
        else:
            ix = self.indexes.get(low)
            if ix is not None:
                ix.root = new_root

    # ------------------------------------------------------------------ #

    def register_table(self, ts: TableSchema, sql: str) -> None:
        self.tables[ts.name.lower()] = ts
        self.write_schema_row(["table", ts.name, ts.name, ts.root, sql, 0])

    def drop_table(self, name: str) -> None:
        self.tables.pop(name.lower(), None)
        self.delete_schema_row("table", name)
        for ix in self.table_indexes(name):
            self.indexes.pop(ix.name.lower(), None)
            self.delete_schema_row("index", ix.name)
        self._next_auto_seq.pop(name.lower(), None)

    def register_index(self, ix: IndexSchema, sql: Optional[str]) -> None:
        self.indexes[ix.name.lower()] = ix
        self.write_schema_row(
            ["index", ix.name, ix.table, ix.root, sql or "",
             1 if ix.auto else 0, 1 if ix.unique else 0])

    def drop_index(self, name: str) -> None:
        self.indexes.pop(name.lower(), None)
        self.delete_schema_row("index", name)

    def next_auto_index_name(self, table: str) -> str:
        low = table.lower()
        seq = self._next_auto_seq.get(low, 0) + 1
        # account for indexes loaded from disk
        existing = sum(1 for ix in self.indexes.values()
                       if ix.table.lower() == low and ix.auto)
        seq = max(seq, existing + 1)
        self._next_auto_seq[low] = seq
        return f"nimbus_auto_{table}_{seq}"


def table_schema_from_ddl(stmt: CreateTable, root: int) -> TableSchema:
    columns: List[ColumnMeta] = []
    pk_cols: List[str] = []
    unique_sets: List[List[str]] = []

    for col in stmt.columns:
        pk = col.pk
        if pk:
            pk_cols.append(col.name)
        columns.append(ColumnMeta(
            name=col.name, type_decl=col.type_decl,
            affinity=affinity_of(col.type_decl), not_null=col.not_null,
            pk=pk, unique=col.unique, default_expr=col.default))

    for con in stmt.constraints:
        if con.kind == "PK":
            if pk_cols:
                raise SchemaError(
                    f"table '{stmt.name}' has more than one PRIMARY KEY")
            for c in con.columns:
                if not any(c.lower() == x.name.lower() for x in columns):
                    raise SchemaError(
                        f"unknown column '{c}' in PRIMARY KEY")
            pk_cols = list(con.columns)
            for c in pk_cols:
                for meta in columns:
                    if meta.name.lower() == c.lower():
                        meta.pk = True
        elif con.kind == "UNIQUE":
            for c in con.columns:
                if not any(c.lower() == x.name.lower() for x in columns):
                    raise SchemaError(f"unknown column '{c}' in UNIQUE")
            unique_sets.append(list(con.columns))

    # single-column column-level UNIQUE folds into unique_sets
    folded: List[ColumnMeta] = []
    for meta in columns:
        if meta.unique and not meta.pk:
            folded.append(meta)
    if folded:
        for meta in folded:
            unique_sets.append([meta.name])
            meta.unique = False

    # PK columns are implicitly NOT NULL
    for c in pk_cols:
        for meta in columns:
            if meta.name.lower() == c.lower():
                meta.not_null = True

    return TableSchema(name=stmt.name, root=root, columns=columns,
                       pk_cols=pk_cols, unique_sets=unique_sets)
