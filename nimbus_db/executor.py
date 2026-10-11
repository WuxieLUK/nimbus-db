"""Statement execution: FROM construction, joins, index selection,
grouping, sorting, and the DML/DDL writers that keep B-trees and their
indexes consistent inside one transaction.
"""
from __future__ import annotations

import functools
from typing import Callable, Dict, List, Optional, Tuple

from . import ast_nodes as A
from .btree import BTree, cmp_index_key
from .catalog import Catalog, ColumnMeta, IndexSchema, TableSchema, \
    table_schema_from_ddl
from .errors import (ConstraintError, OperationalError, SchemaError,
                     UnsupportedError)
from .planner import (AGGREGATES, CAgg, CExpr, Scope, _arith, _text_to_num,
                      _to_text, compile_expr, is_aggregate, truthy)
from .values import (deserialize_row, group_key, serialize_row, sql_compare)

_ROWID_EQ = ("=", "==")
_RANGE_OPS = {"<", "<=", ">", ">="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


class Result:
    def __init__(self, columns: Optional[List[str]] = None,
                 rows: Optional[List[List]] = None,
                 rows_affected: int = 0, command: str = "",
                 message: str = "") -> None:
        self.columns = columns or []
        self.rows = rows or []
        self.rows_affected = rows_affected
        self.command = command
        self.message = message

    def __repr__(self) -> str:
        return (f"Result(command={self.command!r}, rows={len(self.rows)}, "
                f"columns={self.columns!r})")


# ========================================================================== #
# row sources
# ========================================================================== #

def _table_reader(db, ts: TableSchema, tree: Optional[BTree] = None
                  ) -> Tuple[List[List], List]:
    tree = tree or BTree(db.pager, ts.root, "table")
    alias_idx = ts.col_index(ts.rowid_alias) if ts.rowid_alias else None
    rows: List[List] = []
    rowids: List = []
    for rid, payload in tree.iter_table():
        vals = deserialize_row(payload)
        if alias_idx is not None and alias_idx < len(vals):
            vals[alias_idx] = rid
        rows.append(vals)
        rowids.append(rid)
    return rows, rowids


def _fetch_by_rowids(db, ts: TableSchema, rids: List[int]
                     ) -> Tuple[List[List], List]:
    tree = BTree(db.pager, ts.root, "table")
    alias_idx = ts.col_index(ts.rowid_alias) if ts.rowid_alias else None
    rows, rowids = [], []
    for rid in rids:
        payload = tree.lookup(rid)
        if payload is None:
            continue
        vals = deserialize_row(payload)
        if alias_idx is not None and alias_idx < len(vals):
            vals[alias_idx] = rid
        rows.append(vals)
        rowids.append(rid)
    return rows, rowids


# --------------------------------------------------------------------------- #
# index planning (top-level AND conjuncts of the WHERE clause)
# --------------------------------------------------------------------------- #

def _flatten_and(expr: Optional[A.Expr]) -> List[A.Expr]:
    if expr is None:
        return []
    if isinstance(expr, A.Binary) and expr.op == "AND":
        return _flatten_and(expr.left) + _flatten_and(expr.right)
    return [expr]


def const_value(expr: A.Expr) -> Tuple[bool, object]:
    """Evaluate an expression with no row context (for index bounds)."""
    if isinstance(expr, A.Literal):
        return True, expr.value
    if isinstance(expr, A.Unary) and expr.op in ("-", "+"):
        ok, v = const_value(expr.operand)
        if not ok or v is None:
            return ok, v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return True, (-v if expr.op == "-" else v)
        return False, None
    if isinstance(expr, A.Binary) and expr.op in ("+", "-", "*", "/"):
        ok1, a = const_value(expr.left)
        ok2, b = const_value(expr.right)
        if ok1 and ok2:
            try:
                return True, _arith(expr.op, a, b)
            except Exception:
                return False, None
    if isinstance(expr, A.ScalarSubquery):
        try:
            return True, run_scalar_subquery(expr.select, None)
        except Exception:
            return False, None
    return False, None


class _IndexPlan:
    __slots__ = ("index", "eq", "lo_val", "lo_incl", "hi_val", "hi_incl",
                 "n_eq")

    def __init__(self, index: IndexSchema, eq: List, lo_val, lo_incl: bool,
                 hi_val, hi_incl: bool, n_eq: int) -> None:
        self.index = index
        self.eq = eq                  # equality values for the prefix columns
        self.lo_val = lo_val          # raw bound on the column after prefix
        self.lo_incl = lo_incl
        self.hi_val = hi_val
        self.hi_incl = hi_incl
        self.n_eq = n_eq


class _RowidPointPlan:
    __slots__ = ("rowid",)

    def __init__(self, rowid: int) -> None:
        self.rowid = rowid


class _RowidRangePlan:
    __slots__ = ("lo", "lo_incl", "hi", "hi_incl")

    def __init__(self, lo, lo_incl, hi, hi_incl) -> None:
        self.lo, self.lo_incl = lo, lo_incl
        self.hi, self.hi_incl = hi, hi_incl


def plan_index_for_where(db, ts: TableSchema, where: Optional[A.Expr]
                         ) -> Tuple[Optional[object], bool]:
    """Return (plan, full_scan).  plan is None when nothing applies."""
    eq_by_col: Dict[str, List[object]] = {}
    range_by_col: Dict[str, List[Tuple[str, object]]] = {}

    for c in _flatten_and(where):
        if not isinstance(c, A.Binary):
            continue
        op, left, right = c.op, c.left, c.right
        if isinstance(right, A.ColumnRef) and op in _RANGE_OPS | set(_ROWID_EQ):
            left, op = right, _FLIP.get(op, op)
        if not isinstance(left, A.ColumnRef):
            continue
        ok, val = const_value(right)
        if not ok:
            continue
        col = left.name.lower()
        if op in _ROWID_EQ:
            eq_by_col.setdefault(col, []).append(val)
        elif op in _RANGE_OPS:
            range_by_col.setdefault(col, []).append((op, val))

    alias_low = ts.rowid_alias.lower() if ts.rowid_alias else None

    # rowid equality beats everything
    for name in ("rowid", alias_low):
        if name and eq_by_col.get(name):
            v = eq_by_col[name][0]
            if isinstance(v, int) and not isinstance(v, bool):
                return _RowidPointPlan(v), False

    # rowid range scan on the table B-tree
    for name in ("rowid", alias_low):
        if name and range_by_col.get(name):
            lo = lo_incl = hi = hi_incl = None
            for op, val in range_by_col[name]:
                if op in (">", ">="):
                    if lo is None or sql_compare(val, lo) > 0 or \
                            (sql_compare(val, lo) == 0 and op == ">="):
                        lo, lo_incl = val, op == ">="
                else:
                    if hi is None or sql_compare(val, hi) < 0 or \
                            (sql_compare(val, hi) == 0 and op == "<="):
                        hi, hi_incl = val, op == "<="
            return _RowidRangePlan(lo, lo_incl, hi, hi_incl), False

    best: Optional[_IndexPlan] = None
    best_score = (-1, -1)
    for ix in db.catalog.table_indexes(ts.name):
        cols = [c.lower() for c in ix.columns]
        eq: List[object] = []
        p = 0
        while p < len(cols) and eq_by_col.get(cols[p]):
            eq.append(eq_by_col[cols[p]][0])
            p += 1
        lo_val = lo_incl = hi_val = hi_incl = None
        if p < len(cols) and range_by_col.get(cols[p]):
            for op, val in range_by_col[cols[p]]:
                if op in (">", ">="):
                    if lo_val is None or sql_compare(val, lo_val) > 0 or \
                            (sql_compare(val, lo_val) == 0 and op == ">="):
                        lo_val, lo_incl = val, op == ">="
                else:
                    if hi_val is None or sql_compare(val, hi_val) < 0 or \
                            (sql_compare(val, hi_val) == 0 and op == "<="):
                        hi_val, hi_incl = val, op == "<="
            p += 1
        score = (len(eq), p)
        if score > best_score:
            best_score = score
            best = _IndexPlan(ix, eq, lo_val, bool(lo_incl), hi_val,
                              bool(hi_incl), len(eq))
    if best is None:
        return None, True
    return best, False


def scan_table(db, ts: TableSchema, where: Optional[A.Expr]
               ) -> Tuple[List[List], List]:
    """Rows of *ts* matching the index-usable conjuncts of *where* (a
    superset of the full match when only some conjuncts are indexed)."""
    if where is None:
        return _table_reader(db, ts)

    plan, full_scan = plan_index_for_where(db, ts, where)
    if full_scan or plan is None:
        return _table_reader(db, ts)

    if isinstance(plan, _RowidPointPlan):
        return _fetch_by_rowids(db, ts, [plan.rowid])

    if isinstance(plan, _RowidRangePlan):
        tree = BTree(db.pager, ts.root, "table")
        rows, rowids = [], []
        lo = plan.lo if plan.lo is not None else -2 ** 63
        hi = plan.hi
        for rid, payload in tree.iter_table_range(lo, hi, plan.hi_incl,
                                                  plan.lo_incl):
            if plan.lo is not None and not plan.lo_incl and rid == plan.lo:
                continue
            rows.append(deserialize_row(payload))
            rowids.append(rid)
        return rows, rowids

    # generic index plan: eq prefix + optional range on the next column
    ix = plan.index
    tree = BTree(db.pager, ix.root, "index")
    p = plan.n_eq
    eq = tuple(plan.eq)

    def accept(key: Tuple) -> Optional[bool]:
        """True: yield; False: skip; None: stop."""
        for a, b in zip(key[:p], eq):
            if sql_compare(a, b) != 0:
                return None
        if plan.lo_val is not None:
            c = sql_compare(key[p], plan.lo_val)
            if c < 0 or (c == 0 and not plan.lo_incl):
                return False
        if plan.hi_val is not None:
            c = sql_compare(key[p], plan.hi_val)
            if c > 0 or (c == 0 and not plan.hi_incl):
                return None
        return True

    lo_start: Optional[Tuple] = None
    if eq or plan.lo_val is not None:
        lo_start = eq + ((plan.lo_val,) if plan.lo_val is not None else ())

    rids: List[int] = []
    for key in tree.iter_index_from(lo_start, lambda k: accept(k) is None):
        if accept(key):
            rids.append(key[-1])
    return _fetch_by_rowids(db, ts, rids)


# --------------------------------------------------------------------------- #
# FROM construction
# --------------------------------------------------------------------------- #

class _Source:
    """Materialized row source for one FROM slot."""
    __slots__ = ("alias", "ts", "rows", "rowids")

    def __init__(self, alias: str, ts: TableSchema, rows: List[List],
                 rowids: List) -> None:
        self.alias = alias
        self.ts = ts
        self.rows = rows
        self.rowids = rowids


def _master_schema() -> TableSchema:
    cols = ["type", "name", "tbl_name", "rootpage", "sql", "auto", "unique"]
    return TableSchema(name="nimbus_master", root=0,
                       columns=[ColumnMeta(c, "", "TEXT") for c in cols])


def _pseudo_schema(name: str, columns: List[str]) -> TableSchema:
    return TableSchema(name=name, root=0,
                       columns=[ColumnMeta(c, "", "BLOB") for c in columns])


def _build_source(db, ref: A.TableRef, outer: Optional[Scope],
                  where: Optional[A.Expr]) -> _Source:
    if ref.subquery is not None:
        res = exec_select(db, ref.subquery, outer)
        ts = _pseudo_schema(ref.name, res.columns)
        rows = [list(r) for r in res.rows]
        return _Source(ref.name, ts, rows, [None] * len(rows))
    low = ref.name.lower()
    if low in ("nimbus_master", "nimbus_schema"):
        rows = [list(r) + [None] * (7 - len(r)) for r in db.catalog.schema_rows]
        return _Source(ref.name, _master_schema(), [r[:7] for r in rows],
                       [None] * len(rows))
    ts = db.catalog.table(ref.name)
    if where is not None:
        rows, rowids = scan_table(db, ts, where)
    else:
        rows, rowids = _table_reader(db, ts)
    return _Source(ref.alias or ref.name, ts, rows, rowids)


def _null_slot(ts: TableSchema) -> List:
    return [None] * len(ts.columns)


def build_from(db, sel: A.Select, outer: Optional[Scope],
               where: Optional[A.Expr] = None):
    """-> (scope, combined_rows, combined_rowids); a combined row is a
    list of per-slot value lists."""
    if sel.from_ is None:
        return Scope(), [[]], [None]

    first_where = where if not sel.joins else None
    first = _build_source(db, sel.from_, outer, first_where)
    scope = Scope()
    scope.add(first.alias, first.ts)
    combined: List[List] = [[r] for r in first.rows]
    combined_rids: List = [[r] for r in first.rowids]

    for join in sel.joins:
        right = _build_source(db, join.table, outer, None)
        scope.add(right.alias, right.ts)
        right_slot = len(scope.slots) - 1

        new_rows: List[List] = []
        new_rids: List = []

        hj = _hash_join_key(join.on, scope, right_slot) \
            if join.kind in ("INNER", "CROSS") else None
        if hj is not None:
            lsi, lci, rci = hj
            index: Dict[Tuple, List[int]] = {}
            for j, rrow in enumerate(right.rows):
                index.setdefault(group_key(rrow[rci]), []).append(j)
            for lrow, lrid in zip(combined, combined_rids):
                for j in index.get(group_key(lrow[lsi][lci]), ()):
                    new_rows.append(lrow + [right.rows[j]])
                    new_rids.append(lrid + [right.rowids[j]])
            combined, combined_rids = new_rows, new_rids
            continue

        on_c = compile_expr(join.on, scope, outer) if join.on else None
        for lrow, lrid in zip(combined, combined_rids):
            matched = False
            for j, rrow in enumerate(right.rows):
                row = lrow + [rrow]
                rid = lrid + [right.rowids[j]]
                if on_c is None or truthy(on_c.eval(row, rid)):
                    matched = True
                    new_rows.append(row)
                    new_rids.append(rid)
            if join.kind == "LEFT" and not matched:
                new_rows.append(lrow + [_null_slot(right.ts)])
                new_rids.append(lrid + [None])
        combined, combined_rids = new_rows, new_rids

    return scope, combined, combined_rids


def _hash_join_key(on: Optional[A.Expr], scope: Scope, right_slot: int
                   ) -> Optional[Tuple[int, int, int]]:
    """Detect ``left.col = right.col`` usable for a hash join ->
    (left_slot, left_col, right_col)."""
    if not isinstance(on, A.Binary) or on.op not in _ROWID_EQ:
        return None
    l, r = on.left, on.right
    if not (isinstance(l, A.ColumnRef) and isinstance(r, A.ColumnRef)):
        return None
    try:
        lsi, lci = scope.resolve(l.name, l.table)
        rsi, rci = scope.resolve(r.name, r.table)
    except Exception:
        return None
    if rsi == right_slot and lsi < right_slot and lci != -1 and rci != -1:
        return (lsi, lci, rci)
    if lsi == right_slot and rsi < right_slot and lci != -1 and rci != -1:
        return (rsi, rci, lci)
    return None


# ========================================================================== #
# SELECT
# ========================================================================== #

def _label_for(expr: A.Expr) -> str:
    if isinstance(expr, A.ColumnRef):
        return expr.name
    if isinstance(expr, A.Star):
        return "*"
    if isinstance(expr, A.FuncCall):
        if expr.star:
            return f"{expr.name.upper()}(*)"
        inner = ", ".join(_label_for(a) for a in expr.args)
        if expr.distinct:
            inner = "DISTINCT " + inner
        return f"{expr.name.upper()}({inner})"
    if isinstance(expr, A.Literal):
        v = expr.value
        if isinstance(v, str):
            return f"'{v}'"
        if v is None:
            return "NULL"
        return str(v)
    if isinstance(expr, A.Cast):
        return f"CAST({_label_for(expr.expr)} AS {expr.type_name})"
    if isinstance(expr, A.Case):
        return "CASE"
    if isinstance(expr, A.Binary):
        return f"{_label_for(expr.left)} {expr.op} {_label_for(expr.right)}"
    if isinstance(expr, A.Unary):
        return f"{expr.op} {_label_for(expr.operand)}"
    if isinstance(expr, A.IsNull):
        return f"{_label_for(expr.expr)} IS NULL"
    if isinstance(expr, A.Between):
        return f"{_label_for(expr.expr)} BETWEEN"
    if isinstance(expr, A.InList):
        return f"{_label_for(expr.expr)} IN (...)"
    if isinstance(expr, A.Like):
        return f"{_label_for(expr.expr)} LIKE {_label_for(expr.pattern)}"
    if isinstance(expr, (A.ScalarSubquery, A.InSelect)):
        return "(subquery)"
    return "expr"


def _collect_aggregates(expr: Optional[A.Expr], out: List) -> None:
    if expr is None:
        return
    if isinstance(expr, A.FuncCall) and expr.name.upper() in AGGREGATES:
        out.append(expr)
        return
    for child in _children(expr):
        _collect_aggregates(child, out)


def _children(expr: A.Expr) -> List[A.Expr]:
    if isinstance(expr, A.FuncCall):
        return list(expr.args)
    if isinstance(expr, A.Binary):
        return [expr.left, expr.right]
    if isinstance(expr, A.Unary):
        return [expr.operand]
    if isinstance(expr, A.IsNull):
        return [expr.expr]
    if isinstance(expr, A.Between):
        return [expr.expr, expr.lo, expr.hi]
    if isinstance(expr, A.InList):
        return [expr.expr] + list(expr.items)
    if isinstance(expr, A.Like):
        return [expr.expr, expr.pattern]
    if isinstance(expr, A.Case):
        out = []
        if expr.operand:
            out.append(expr.operand)
        for c, r in expr.whens:
            out.extend([c, r])
        if expr.else_:
            out.append(expr.else_)
        return out
    if isinstance(expr, A.Cast):
        return [expr.expr]
    return []


class _AggRun:
    """Accumulator for one aggregate call over one group."""

    def __init__(self, call: A.FuncCall, scope: Scope, outer) -> None:
        self.name = call.name.upper()
        self.star = call.star
        self.distinct = call.distinct
        self.arg = None
        self.sep = ","
        if not call.star:
            if not call.args:
                raise SchemaError(f"{self.name}() requires an argument")
            self.arg = compile_expr(call.args[0], scope, outer)
            if len(call.args) > 1:
                if self.name != "GROUP_CONCAT":
                    raise SchemaError(
                        f"{self.name}() takes exactly one argument")
                sep_c = compile_expr(call.args[1], scope, outer)
                if sep_c.has_column():
                    raise SchemaError(
                        "GROUP_CONCAT separator must be a constant")
                sep = sep_c.eval([], [])
                self.sep = sep if isinstance(sep, str) and sep else ","
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.total = 0.0
        self.int_total = 0
        self.is_float = False
        self.minv = None
        self.maxv = None
        self.seen: set = set()
        self.parts: List[str] = []

    def step(self, row: List, rowids: List) -> None:
        if self.star:
            self.count += 1
            return
        v = self.arg.eval(row, rowids)
        if v is None:
            return
        if self.distinct:
            k = group_key(v)
            if k in self.seen:
                return
            self.seen.add(k)
        self.count += 1
        if self.name in ("SUM", "TOTAL", "AVG"):
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, (int, float)):
                if isinstance(v, float):
                    self.is_float = True
                self.total += float(v)
                self.int_total += int(v)
            else:
                num = _text_to_num(v) if isinstance(v, str) else None
                if num is not None:
                    if isinstance(num, float):
                        self.is_float = True
                    self.total += float(num)
                    self.int_total += int(num)
        elif self.name == "MIN":
            if self.minv is None or sql_compare(v, self.minv) < 0:
                self.minv = v
        elif self.name == "MAX":
            if self.maxv is None or sql_compare(v, self.maxv) > 0:
                self.maxv = v
        elif self.name == "GROUP_CONCAT":
            self.parts.append(v if isinstance(v, str) else _to_text(v))

    def finish(self) -> object:
        n = self.name
        if n == "COUNT":
            return self.count
        if n == "SUM":
            return None if self.count == 0 else (
                self.total if self.is_float else self.int_total)
        if n == "TOTAL":
            return float(self.total)
        if n == "AVG":
            return None if self.count == 0 else self.total / self.count
        if n == "MIN":
            return self.minv
        if n == "MAX":
            return self.maxv
        if n == "GROUP_CONCAT":
            return self.sep.join(self.parts) if self.parts else None
        raise AssertionError(n)


def exec_select(db, sel: A.Select, outer: Optional[Scope] = None) -> Result:
    if sel.union_op and sel.union_other is not None:
        left = _exec_select_single(db, sel, outer, defer_tail=True)
        right = _exec_select_single(db, sel.union_other, outer,
                                    defer_tail=True)
        if len(right.columns) != len(left.columns):
            raise OperationalError(
                "SELECTs to the left and right of UNION do not have the "
                "same number of result columns")
        if sel.union_op == "UNION ALL":
            rows = left.rows + right.rows
        else:
            seen = set()
            rows = []
            for r in left.rows + right.rows:
                k = tuple(group_key(v) for v in r)
                if k not in seen:
                    seen.add(k)
                    rows.append(r)
        res = Result(columns=left.columns, rows=rows, command="SELECT")
        _apply_compound_tail(db, sel, res)
        return res
    return _exec_select_single(db, sel, outer)


def _apply_compound_tail(db, sel: A.Select, res: Result) -> None:
    """ORDER BY / LIMIT on a compound SELECT references output columns
    by position or alias only."""
    rows = res.rows
    if sel.order_by:
        specs = []
        for oi in sel.order_by:
            e = oi.expr
            pos = None
            if isinstance(e, A.Literal) and isinstance(e.value, int) \
                    and not isinstance(e.value, bool):
                pos = e.value - 1
            elif isinstance(e, A.ColumnRef) and e.table is None:
                low = e.name.lower()
                hits = [i for i, n in enumerate(res.columns)
                        if n.lower() == low]
                if hits:
                    pos = hits[0]
            if pos is None or not 0 <= pos < len(res.columns):
                raise UnsupportedError(
                    "ORDER BY on a compound SELECT must reference output "
                    "columns by name or position")
            specs.append((pos, oi.desc))

        def cmp_rows(a, b) -> int:
            for pos, desc in specs:
                c = sql_compare(a[pos], b[pos])
                if c:
                    return -c if desc else c
            return 0

        rows = sorted(rows, key=functools.cmp_to_key(cmp_rows))
    rows = _limit_rows(db, sel, rows)
    res.rows = rows


def _limit_rows(db, sel: A.Select, rows: List[List]) -> List[List]:
    if sel.limit is None:
        return rows
    ok, limit = const_value(sel.limit)
    if not ok or limit is None or not isinstance(limit, (int, float)):
        raise SchemaError("LIMIT must be a constant integer")
    limit = int(limit)
    offset = 0
    if sel.offset is not None:
        ok, offset = const_value(sel.offset)
        if not ok or offset is None or not isinstance(offset, (int, float)):
            raise SchemaError("OFFSET must be a constant integer")
        offset = max(int(offset), 0)
    return rows[offset:] if limit < 0 else rows[offset:offset + limit]


def _exec_select_single(db, sel: A.Select, outer: Optional[Scope],
                        defer_tail: bool = False) -> Result:
    scope, combined, combined_rids = build_from(db, sel, outer, sel.where)

    # ---- WHERE (full filter; scan_table only pre-narrows) -------------- #
    if sel.where is not None:
        wc = compile_expr(sel.where, scope, outer)
        rows, rids = [], []
        for row, rid in zip(combined, combined_rids):
            if truthy(wc.eval(row, rid)):
                rows.append(row)
                rids.append(rid)
        combined, combined_rids = rows, rids

    # ---- select list --------------------------------------------------- #
    out_names: List[str] = []
    item_exprs: List[Optional[A.Expr]] = []
    for item in sel.items:
        if isinstance(item.expr, A.Star):
            st: A.Star = item.expr
            if st.table is None:
                slots = list(range(len(scope.slots)))
            else:
                low_t = st.table.lower()
                slots = [i for i, (a, _t) in enumerate(scope.slots)
                         if a.lower() == low_t]
                if not slots:
                    raise SchemaError(f"no such table: {st.table}")
            for si in slots:
                alias, ts2 = scope.slots[si]
                for col in ts2.columns:
                    out_names.append(col.name)
                    item_exprs.append(A.ColumnRef(alias, col.name))
            continue
        out_names.append(item.alias or _label_for(item.expr))
        item_exprs.append(item.expr)

    # ---- aggregation --------------------------------------------------- #
    agg_calls: List[A.FuncCall] = []
    _collect_aggregates(sel.having, agg_calls)
    for e in item_exprs:
        _collect_aggregates(e, agg_calls)
    for oi in sel.order_by:
        if not isinstance(oi.expr, A.Literal):
            _collect_aggregates(oi.expr, agg_calls)
    grouped = bool(sel.group_by) or bool(agg_calls)

    emit_rows: List[List] = []
    src_meta: List[Tuple[List, List, Optional[List]]] = []
    agg_slots_map: Dict[int, int] = {}

    if not grouped:
        exprs = [compile_expr(e, scope, outer) if e is not None else None
                 for e in item_exprs]
        for row, rid in zip(combined, combined_rids):
            emit_rows.append([e.eval(row, rid) if e is not None else None
                              for e in exprs])
            src_meta.append((row, rid, None))
    else:
        uniq: Dict[int, A.FuncCall] = {}
        for c in agg_calls:
            uniq.setdefault(id(c), c)
        agg_calls = list(uniq.values())
        agg_slots_map = {id(c): i for i, c in enumerate(agg_calls)}

        if sel.group_by:
            g_exprs = [compile_expr(g, scope, outer) for g in sel.group_by]
            groups: Dict[Tuple, Tuple[List, List, List]] = {}
            order: List[Tuple] = []
            for row, rid in zip(combined, combined_rids):
                key = tuple(group_key(g.eval(row, rid)) for g in g_exprs)
                entry = groups.get(key)
                if entry is None:
                    runs = [_AggRun(c, scope, outer) for c in agg_calls]
                    groups[key] = (row, rid, runs)
                    order.append(key)
                for run in groups[key][2]:
                    run.step(row, rid)
            for key in order:
                row, rid, runs = groups[key]
                agg_vals = [r.finish() for r in runs]
                emit_rows.append(_project_grouped(
                    item_exprs, scope, outer, agg_slots_map, row, rid,
                    agg_vals))
                src_meta.append((row, rid, agg_vals))
        else:
            runs = [_AggRun(c, scope, outer) for c in agg_calls]
            for row, rid in zip(combined, combined_rids):
                for run in runs:
                    run.step(row, rid)
            agg_vals = [r.finish() for r in runs]
            rep_row = combined[0] if combined else \
                ([[] for _ in scope.slots] if scope.slots else [])
            rep_rid = combined_rids[0] if combined_rids else \
                ([None] * len(scope.slots) if scope.slots else [None])
            emit_rows.append(_project_grouped(
                item_exprs, scope, outer, agg_slots_map, rep_row, rep_rid,
                agg_vals))
            src_meta.append((rep_row, rep_rid, agg_vals))

        if sel.having is not None:
            hc = compile_expr(sel.having, scope, outer, dict(agg_slots_map))
            kept = []
            for out_row, meta in zip(emit_rows, src_meta):
                row, rid, agg_vals = meta
                if truthy(_eval_cexpr(hc, row, rid, agg_vals)):
                    kept.append((out_row, meta))
            emit_rows = [r for r, _ in kept]
            src_meta = [m for _, m in kept]

    # ---- ORDER BY ------------------------------------------------------ #
    if sel.order_by:
        order_specs = []
        for oi in sel.order_by:
            spec = None
            e = oi.expr
            if isinstance(e, A.Literal) and isinstance(e.value, int) \
                    and not isinstance(e.value, bool):
                pos = e.value - 1
                if not 0 <= pos < len(out_names):
                    raise SchemaError(
                        f"ORDER BY position {e.value} out of range")
                spec = ("proj", pos)
            elif isinstance(e, A.ColumnRef) and e.table is None:
                low = e.name.lower()
                hits = [i for i, n in enumerate(out_names)
                        if n.lower() == low]
                if hits:
                    spec = ("proj", hits[0])
                else:
                    si, ci = scope.resolve(e.name, None)
                    spec = ("rowid", si) if ci == -1 else \
                        ("expr", compile_expr(
                            e, scope, outer,
                            dict(agg_slots_map) if grouped else None))
            else:
                spec = ("expr", compile_expr(
                    e, scope, outer, dict(agg_slots_map) if grouped else None))
            order_specs.append((spec, oi.desc))

        def cmp_rows(a, b) -> int:
            (out_a, meta_a), (out_b, meta_b) = a, b
            for (kind, arg), desc in order_specs:
                if kind == "proj":
                    va, vb = out_a[arg], out_b[arg]
                elif kind == "rowid":
                    va, vb = meta_a[1][arg], meta_b[1][arg]
                else:
                    va = _eval_cexpr(arg, meta_a[0], meta_a[1], meta_a[2])
                    vb = _eval_cexpr(arg, meta_b[0], meta_b[1], meta_b[2])
                c = sql_compare(va, vb)
                if c:
                    return -c if desc else c
            return 0

        pairs = list(zip(emit_rows, src_meta))
        pairs.sort(key=functools.cmp_to_key(cmp_rows))
        emit_rows = [r for r, _ in pairs]
        src_meta = [m for _, m in pairs]

    # ---- DISTINCT ------------------------------------------------------ #
    if sel.distinct:
        seen = set()
        rows, metas = [], []
        for r, m in zip(emit_rows, src_meta):
            k = tuple(group_key(v) for v in r)
            if k not in seen:
                seen.add(k)
                rows.append(r)
                metas.append(m)
        emit_rows, src_meta = rows, metas

    # ---- LIMIT / OFFSET ------------------------------------------------ #
    if not defer_tail:
        emit_rows = _limit_rows(db, sel, emit_rows)

    return Result(columns=out_names, rows=emit_rows, command="SELECT")


def _bind_aggs(node: object, agg_vals: List) -> None:
    """Push precomputed aggregate values into every CAgg placeholder
    reachable from *node* (aggregates may be nested inside scalar
    functions or CASE branches, e.g. ROUND(AVG(x), 0))."""
    if isinstance(node, CAgg):
        node.value = (agg_vals or [None] * (node.agg_index + 1))[node.agg_index]
        return
    for v in vars(node).values():
        if isinstance(v, CExpr):
            _bind_aggs(v, agg_vals)
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, CExpr):
                    _bind_aggs(x, agg_vals)
                elif isinstance(x, (list, tuple)):
                    for y in x:
                        if isinstance(y, CExpr):
                            _bind_aggs(y, agg_vals)


def _eval_cexpr(expr: CExpr, row, rids, agg_vals) -> object:
    if agg_vals is not None:
        _bind_aggs(expr, agg_vals)
    return expr.eval(row, rids)


def _project_grouped(item_exprs, scope: Scope, outer,
                     agg_slots: Dict[int, int], row: List, rid: List,
                     agg_vals: List) -> List:
    out = []
    for e in item_exprs:
        if e is None:
            out.append(None)
            continue
        ce = compile_expr(e, scope, outer, dict(agg_slots))
        out.append(_eval_cexpr(ce, row, rid, agg_vals))
    return out


# ---- subquery entry points used by planner --------------------------- #

def run_scalar_subquery(sel: A.Select, outer: Optional[Scope]) -> object:
    res = exec_select(_current_db(), sel, outer)
    if not res.rows:
        return None
    return res.rows[0][0] if res.rows[0] else None


def run_subquery_values(sel: A.Select, outer: Optional[Scope]) -> List:
    res = exec_select(_current_db(), sel, outer)
    return [r[0] if r else None for r in res.rows]


_DB_STACK: List = []


def _current_db():
    if not _DB_STACK:
        raise OperationalError("no database context for subquery")
    return _DB_STACK[-1]


# ========================================================================== #
# DML
# ========================================================================== #

def _index_key(ts: TableSchema, ix: IndexSchema, row: List,
               rowid: int) -> Tuple:
    vals = [row[ts.col_index(c)] for c in ix.columns]
    vals.append(rowid)
    return tuple(vals)


def exec_insert(db, stmt: A.Insert) -> Result:
    ts = db.catalog.table(stmt.table)
    if stmt.columns is not None:
        cols = stmt.columns
        seen = set()
        for c in cols:
            if c.lower() in seen:
                raise SchemaError(f"duplicate column name: {c}")
            seen.add(c.lower())
            ts.col_index(c)
    else:
        cols = [c.name for c in ts.columns]

    empty_scope = Scope()
    alias_idx = ts.col_index(ts.rowid_alias) if ts.rowid_alias else None
    count = 0

    for value_exprs in stmt.rows:
        if len(value_exprs) != len(cols):
            raise OperationalError(
                f"table {ts.name} has {len(cols)} columns but "
                f"{len(value_exprs)} values were supplied")
        row: List = [None] * len(ts.columns)
        provided = set()
        for cname, vexpr in zip(cols, value_exprs):
            ci = ts.col_index(cname)
            meta = ts.columns[ci]
            val = compile_expr(vexpr, empty_scope).eval([], [])
            row[ci] = coerce_value(val, meta)
            provided.add(ci)
        for ci, meta in enumerate(ts.columns):
            if ci in provided:
                continue
            if meta.default_expr is not None:
                val = compile_expr(meta.default_expr,
                                   empty_scope).eval([], [])
                row[ci] = coerce_value(val, meta)

        for ci, meta in enumerate(ts.columns):
            if meta.not_null and row[ci] is None and ci != alias_idx:
                raise ConstraintError(
                    f"NOT NULL constraint failed: {ts.name}.{meta.name}")

        if alias_idx is not None:
            rowid = row[alias_idx]
            if rowid is None:
                rowid = BTree(db.pager, ts.root, "table").max_rowid() + 1
            row[alias_idx] = None
        else:
            rowid = BTree(db.pager, ts.root, "table").max_rowid() + 1

        _insert_row(db, ts, rowid, row)
        count += 1

    return Result(rows_affected=count, command="INSERT",
                  message=f"inserted {count} row(s)")


def coerce_value(val: object, meta: ColumnMeta) -> object:
    from .values import coerce_for_column
    return coerce_for_column(val, meta.affinity, meta.name)


def _sync_table_root(db, ts: TableSchema, tree: BTree) -> None:
    if tree.root != ts.root:
        db.catalog.update_root("table", ts.name, tree.root)


def _sync_index_root(db, ix: IndexSchema, tree: BTree) -> None:
    if tree.root != ix.root:
        db.catalog.update_root("index", ix.name, tree.root)


def _insert_row(db, ts: TableSchema, rowid: int, row: List) -> None:
    if not isinstance(rowid, int) or isinstance(rowid, bool):
        raise ConstraintError(
            f"PRIMARY KEY must be an integer: {ts.name}.{ts.rowid_alias}")
    tree = BTree(db.pager, ts.root, "table")
    if tree.lookup(rowid) is not None:
        raise ConstraintError(
            f"UNIQUE constraint failed: {ts.name}.{ts.rowid_alias}")
    for ix in db.catalog.table_indexes(ts.name):
        if not ix.unique:
            continue
        bt = BTree(db.pager, ix.root, "index")
        found = bt.seek_prefix(_index_key(ts, ix, row, rowid)[:-1])
        if found is not None:
            raise ConstraintError(
                f"UNIQUE constraint failed: {ts.name}.{', '.join(ix.columns)}")
    tree.insert(rowid, serialize_row(row))
    _sync_table_root(db, ts, tree)
    for ix in db.catalog.table_indexes(ts.name):
        bt = BTree(db.pager, ix.root, "index")
        bt.insert(_index_key(ts, ix, row, rowid), b"")
        _sync_index_root(db, ix, bt)


def exec_update(db, stmt: A.Update) -> Result:
    ts = db.catalog.table(stmt.table)
    scope = Scope([(ts.name, ts)])
    alias_idx = ts.col_index(ts.rowid_alias) if ts.rowid_alias else None

    set_pairs = []
    for col, expr in stmt.sets:
        ci = ts.col_index(col)
        if alias_idx is not None and ci == alias_idx:
            raise UnsupportedError(
                "UPDATE of an INTEGER PRIMARY KEY column is not supported; "
                "DELETE and re-INSERT instead")
        set_pairs.append((ci, compile_expr(expr, scope)))
    where_c = compile_expr(stmt.where, scope) if stmt.where else None

    rows, rowids = scan_table(db, ts, stmt.where)
    table_tree = BTree(db.pager, ts.root, "table")
    index_trees = {ix.name: BTree(db.pager, ix.root, "index")
                   for ix in db.catalog.table_indexes(ts.name)}
    count = 0
    for row, rid in zip(rows, rowids):
        if where_c is not None and not truthy(where_c.eval([row], [rid])):
            continue
        new_row = list(row)
        for ci, ce in set_pairs:
            meta = ts.columns[ci]
            new_row[ci] = coerce_value(ce.eval([row], [rid]), meta)
        for ci, meta in enumerate(ts.columns):
            if meta.not_null and new_row[ci] is None and ci != alias_idx:
                raise ConstraintError(
                    f"NOT NULL constraint failed: {ts.name}.{meta.name}")
        for ix in db.catalog.table_indexes(ts.name):
            if not ix.unique:
                continue
            bt = index_trees[ix.name]
            found = bt.seek_prefix(_index_key(ts, ix, new_row, rid)[:-1])
            if found is not None and found[-1] != rid:
                raise ConstraintError(
                    f"UNIQUE constraint failed: {ts.name}.{', '.join(ix.columns)}")

        table_tree.delete(rid)
        table_tree.insert(rid, serialize_row(new_row))
        for ix in db.catalog.table_indexes(ts.name):
            bt = index_trees[ix.name]
            bt.delete(_index_key(ts, ix, row, rid))
            bt.insert(_index_key(ts, ix, new_row, rid), b"")
        count += 1

    _sync_table_root(db, ts, table_tree)
    for name, bt in index_trees.items():
        _sync_index_root(db, db.catalog.index(name), bt)

    return Result(rows_affected=count, command="UPDATE",
                  message=f"updated {count} row(s)")


def exec_delete(db, stmt: A.Delete) -> Result:
    ts = db.catalog.table(stmt.table)
    scope = Scope([(ts.name, ts)])
    where_c = compile_expr(stmt.where, scope) if stmt.where else None
    rows, rowids = scan_table(db, ts, stmt.where)

    table_tree = BTree(db.pager, ts.root, "table")
    index_trees = {ix.name: BTree(db.pager, ix.root, "index")
                   for ix in db.catalog.table_indexes(ts.name)}
    count = 0
    for row, rid in zip(rows, rowids):
        if where_c is not None and not truthy(where_c.eval([row], [rid])):
            continue
        table_tree.delete(rid)
        for ix in db.catalog.table_indexes(ts.name):
            index_trees[ix.name].delete(_index_key(ts, ix, row, rid))
        count += 1

    _sync_table_root(db, ts, table_tree)
    for name, bt in index_trees.items():
        _sync_index_root(db, db.catalog.index(name), bt)

    return Result(rows_affected=count, command="DELETE",
                  message=f"deleted {count} row(s)")


# ========================================================================== #
# DDL
# ========================================================================== #

def render_create_table(stmt: A.CreateTable) -> str:
    parts = []
    for c in stmt.columns:
        seg = f"{c.name} {c.type_decl}".strip()
        if c.pk:
            seg += " PRIMARY KEY"
        if c.not_null:
            seg += " NOT NULL"
        if c.unique:
            seg += " UNIQUE"
        if c.default is not None:
            seg += f" DEFAULT {_render_expr(c.default)}"
        parts.append(seg)
    for con in stmt.constraints:
        if con.kind == "PK":
            parts.append(f"PRIMARY KEY ({', '.join(con.columns)})")
        elif con.kind == "UNIQUE":
            parts.append(f"UNIQUE ({', '.join(con.columns)})")
    return f"CREATE TABLE {stmt.name} ({', '.join(parts)})"


def _render_expr(e: A.Expr) -> str:
    if isinstance(e, A.Literal):
        if isinstance(e.value, str):
            return f"'{e.value}'"
        if e.value is None:
            return "NULL"
        return str(e.value)
    if isinstance(e, A.Unary):
        return f"{e.op}{_render_expr(e.operand)}"
    if isinstance(e, A.Binary):
        return f"{_render_expr(e.left)} {e.op} {_render_expr(e.right)}"
    return "?"


def exec_create_table(db, stmt: A.CreateTable) -> Result:
    if db.catalog.table_or_none(stmt.name) is not None:
        if stmt.if_not_exists:
            return Result(command="CREATE TABLE",
                          message=f"table {stmt.name} exists")
        raise SchemaError(f"table {stmt.name} already exists")
    ts = table_schema_from_ddl(stmt, 0)
    ts.root = BTree.create_root(db.pager, "table").root
    db.catalog.register_table(ts, render_create_table(stmt))

    alias = ts.rowid_alias
    auto_targets: List[List[str]] = []
    if ts.pk_cols and (len(ts.pk_cols) > 1 or alias is None):
        auto_targets.append(list(ts.pk_cols))
    for uset in ts.unique_sets:
        auto_targets.append(list(uset))
    seen_targets = set()
    for cols in auto_targets:
        k = tuple(c.lower() for c in cols)
        if k in seen_targets:
            continue
        seen_targets.add(k)
        name = db.catalog.next_auto_index_name(ts.name)
        _create_index(db, name, ts, cols, unique=True, auto=True, sql=None)

    return Result(command="CREATE TABLE",
                  message=f"created table {stmt.name}")


def _create_index(db, name: str, ts: TableSchema, cols: List[str],
                  unique: bool, auto: bool, sql: Optional[str]) -> None:
    bt = BTree.create_root(db.pager, "index")
    rows, rowids = _table_reader(db, ts)
    entries = []
    probe = IndexSchema(name=name, table=ts.name, columns=cols, root=0)
    for row, rid in zip(rows, rowids):
        entries.append(_index_key(ts, probe, row, rid))
    entries.sort(key=functools.cmp_to_key(cmp_index_key))
    for key in entries:
        if unique and bt.seek_prefix(key[:-1]) is not None:
            raise ConstraintError(
                f"UNIQUE constraint failed: {ts.name}.{', '.join(cols)}")
        bt.insert(key, b"")
    ix = IndexSchema(name=name, table=ts.name, columns=cols,
                     root=bt.root, unique=unique, auto=auto)
    db.catalog.register_index(ix, sql)


def exec_create_index(db, stmt: A.CreateIndex) -> Result:
    if db.catalog.indexes.get(stmt.name.lower()) is not None:
        if stmt.if_not_exists:
            return Result(command="CREATE INDEX", message="index exists")
        raise SchemaError(f"index {stmt.name} already exists")
    ts = db.catalog.table(stmt.table)
    for c in stmt.columns:
        ts.col_index(c)
    _create_index(
        db, stmt.name, ts, stmt.columns, unique=stmt.unique, auto=False,
        sql=f"CREATE {'UNIQUE ' if stmt.unique else ''}INDEX {stmt.name} "
            f"ON {stmt.table} ({', '.join(stmt.columns)})")
    return Result(command="CREATE INDEX",
                  message=f"created index {stmt.name}")


def exec_drop_table(db, stmt: A.DropTable) -> Result:
    ts = db.catalog.table_or_none(stmt.name)
    if ts is None:
        if stmt.if_exists:
            return Result(command="DROP TABLE")
        raise SchemaError(f"no such table: {stmt.name}")
    BTree(db.pager, ts.root, "table").free_all()
    for ix in db.catalog.table_indexes(ts.name):
        BTree(db.pager, ix.root, "index").free_all()
    db.catalog.drop_table(ts.name)
    return Result(command="DROP TABLE",
                  message=f"dropped table {stmt.name}")


def exec_drop_index(db, stmt: A.DropIndex) -> Result:
    try:
        ix = db.catalog.index(stmt.name)
    except SchemaError:
        if stmt.if_exists:
            return Result(command="DROP INDEX")
        raise
    if ix.auto:
        raise SchemaError(
            f"index {stmt.name} is auto-created and cannot be dropped")
    BTree(db.pager, ix.root, "index").free_all()
    db.catalog.drop_index(ix.name)
    return Result(command="DROP INDEX",
                  message=f"dropped index {stmt.name}")


# ========================================================================== #
# EXPLAIN
# ========================================================================== #

def explain_select(db, sel: A.Select) -> List[str]:
    lines: List[str] = []
    if sel.union_op:
        lines.append(f"COMPOUND QUERY ({sel.union_op})")
    if sel.from_ is None:
        lines.append("SCAN CONSTANT ROW")
    elif sel.from_.subquery is not None:
        lines.append(f"MATERIALIZED SUBQUERY AS {sel.from_.name}")
        for sub in explain_select(db, sel.from_.subquery):
            lines.append("  " + sub)
    else:
        low = sel.from_.name.lower()
        if low in ("nimbus_master", "nimbus_schema"):
            lines.append("SCAN nimbus_master")
        else:
            ts = db.catalog.table(sel.from_.name)
            plan, full = plan_index_for_where(db, ts, sel.where)
            if not full and isinstance(plan, _RowidPointPlan):
                lines.append(f"SEARCH {ts.name} USING ROWID (rowid=?)")
            elif not full and isinstance(plan, _RowidRangePlan):
                lines.append(f"SEARCH {ts.name} USING ROWID (rowid range)")
            elif not full and isinstance(plan, _IndexPlan):
                conds = [f"{c}=?" for c in plan.index.columns[:plan.n_eq]]
                if plan.hi_val is not None or plan.lo_val is not None:
                    col = plan.index.columns[plan.n_eq]
                    if plan.lo_val is not None:
                        conds.append(f"{col}{'>=' if plan.lo_incl else '>'}?")
                    if plan.hi_val is not None:
                        conds.append(f"{col}{'<=' if plan.hi_incl else '<'}?")
                lines.append(f"SEARCH {ts.name} USING INDEX {plan.index.name} "
                             f"({', '.join(conds)})")
            else:
                lines.append(f"SCAN {ts.name}")
    for join in sel.joins:
        on = join.on
        is_equi = (isinstance(on, A.Binary) and on.op in _ROWID_EQ
                   and isinstance(on.left, A.ColumnRef)
                   and isinstance(on.right, A.ColumnRef))
        kind = "HASH" if is_equi and join.kind in ("INNER", "CROSS") \
            else "NESTED LOOP"
        lines.append(f"{kind} {join.kind} JOIN {join.table.name}")
    if sel.where is not None:
        lines.append("FILTER (WHERE)")
    if sel.group_by:
        lines.append(f"GROUP BY ({len(sel.group_by)} key(s))")
    elif any(is_aggregate(i.expr) for i in sel.items
             if not isinstance(i.expr, A.Star)):
        lines.append("AGGREGATE (single group)")
    if sel.having is not None:
        lines.append("FILTER (HAVING)")
    if sel.order_by:
        lines.append(f"SORT ({len(sel.order_by)} key(s))")
    if sel.distinct:
        lines.append("DISTINCT")
    if sel.limit is not None:
        lines.append("LIMIT")
    return lines
