"""Expression compilation: resolve names against a FROM scope and turn
AST nodes into closures the executor can evaluate per row.

A *scope* is the ordered list of ``(alias, TableSchema)`` produced by
the FROM clause; a row is the parallel list of per-slot value lists and
rowids.  Compilation fails fast on unknown columns/tables and refuses
correlated subqueries with a clear message.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from . import ast_nodes as A
from .catalog import ROWID_NAMES, TableSchema
from .errors import SchemaError, UnsupportedError
from .values import (coerce_for_column, group_key, like_match, sql_compare,
                     truthy)


class Scope:
    def __init__(self, slots: Optional[List[Tuple[str, TableSchema]]] = None):
        self.slots: List[Tuple[str, TableSchema]] = slots or []

    def add(self, alias: str, ts: TableSchema) -> None:
        self.slots.append((alias, ts))

    def aliases(self) -> List[str]:
        return [a for a, _ in self.slots]

    def resolve(self, name: str, qualifier: Optional[str]
                ) -> Tuple[int, int]:
        """-> (slot_index, column_index); ci == -1 means the rowid
        pseudo-column.  Raises on unknown / ambiguous names."""
        if qualifier is not None:
            low_q = qualifier.lower()
            for si, (alias, ts) in enumerate(self.slots):
                if alias.lower() == low_q or ts.name.lower() == low_q:
                    if name.lower() in ROWID_NAMES:
                        return si, -1
                    return si, ts.col_index(name)
            raise SchemaError(f"no such table: {qualifier}")
        low = name.lower()
        hits = []
        for si, (_alias, ts) in enumerate(self.slots):
            if ts.has_column(name):
                hits.append((si, ts.col_index(name)))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SchemaError(f"ambiguous column name: {name}")
        if low in ROWID_NAMES:
            if len(self.slots) == 1:
                return 0, -1
            raise SchemaError(f"ambiguous column name: {name}")
        raise SchemaError(f"no such column: {name}")


# --------------------------------------------------------------------------- #
# compiled expression nodes
# --------------------------------------------------------------------------- #

class CExpr:
    def eval(self, row: List, rowids: List) -> object:
        raise NotImplementedError

    def has_column(self) -> bool:
        return False


class CLit(CExpr):
    def __init__(self, v: object) -> None:
        self.v = v

    def eval(self, row, rowids):
        return self.v


class CCol(CExpr):
    def __init__(self, si: int, ci: int, name: str) -> None:
        self.si, self.ci, self.name = si, ci, name

    def eval(self, row, rowids):
        return row[self.si][self.ci]

    def has_column(self) -> bool:
        return True


class CRowid(CExpr):
    def __init__(self, si: int) -> None:
        self.si = si

    def eval(self, row, rowids):
        return rowids[self.si]

    def has_column(self) -> bool:
        return True


class CUnary(CExpr):
    def __init__(self, op: str, operand: CExpr) -> None:
        self.op, self.operand = op, operand

    def eval(self, row, rowids):
        v = self.operand.eval(row, rowids)
        if self.op == "NOT":
            if v is None:
                return None
            return 0 if truthy(v) else 1
        if v is None:
            return None
        if isinstance(v, bool):
            v = int(v)
        if not isinstance(v, (int, float)):
            return 0
        return -v if self.op == "-" else v

    def has_column(self):
        return self.operand.has_column()


def _num(v: object) -> object:
    if isinstance(v, bool):
        return int(v)
    return v


def _arith(op: str, a: object, b: object) -> object:
    if a is None or b is None:
        return None
    a, b = _num(a), _num(b)
    if isinstance(a, (bytes, bytearray)):
        a = str(a, "utf-8", "replace")
    if isinstance(b, (bytes, bytearray)):
        b = str(b, "utf-8", "replace")
    if op == "||":
        return _to_text(a) + _to_text(b)
    # non-numeric operands behave as 0 (sqlite-style coercion)
    if not isinstance(a, (int, float)):
        a = _text_to_num(a)
    if not isinstance(b, (int, float)):
        b = _text_to_num(b)
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                return None
            if isinstance(a, int) and isinstance(b, int):
                q = abs(a) // abs(b)
                return q if (a >= 0) == (b >= 0) else -q
            return a / b
        if op == "%":
            if b == 0:
                return None
            if isinstance(a, int) and isinstance(b, int):
                r = abs(a) % abs(b)
                return r if a >= 0 else -r
            return a - b * float(int(a / b))
    except OverflowError:
        return None
    raise AssertionError(op)


def _text_to_num(s: str):
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return 0


def _to_text(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{v:.1f}"
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return str(bytes(v), "utf-8", "replace")
    return str(v)


class CBinary(CExpr):
    def __init__(self, op: str, left: CExpr, right: CExpr) -> None:
        self.op, self.left, self.right = op, left, right

    def eval(self, row, rowids):
        op = self.op
        if op == "AND":
            l = self.left.eval(row, rowids)
            lf = None if l is None else truthy(l)
            if lf is False:
                return 0
            r = self.right.eval(row, rowids)
            rf = None if r is None else truthy(r)
            if rf is False:
                return 0
            if lf is None or rf is None:
                return None
            return 1
        if op == "OR":
            l = self.left.eval(row, rowids)
            lf = None if l is None else truthy(l)
            if lf is True:
                return 1
            r = self.right.eval(row, rowids)
            rf = None if r is None else truthy(r)
            if rf is True:
                return 1
            if lf is None or rf is None:
                return None
            return 0

        a = self.left.eval(row, rowids)
        b = self.right.eval(row, rowids)
        if op in ("+", "-", "*", "/", "%", "||"):
            return _arith(op, a, b)
        # comparisons: NULL yields NULL
        if a is None or b is None:
            return None
        c = sql_compare(a, b)
        if op in ("=", "=="):
            return 1 if c == 0 else 0
        if op in ("!=", "<>"):
            return 1 if c != 0 else 0
        if op == "<":
            return 1 if c < 0 else 0
        if op == "<=":
            return 1 if c <= 0 else 0
        if op == ">":
            return 1 if c > 0 else 0
        if op == ">=":
            return 1 if c >= 0 else 0
        raise AssertionError(op)

    def has_column(self):
        return self.left.has_column() or self.right.has_column()


class CIsNull(CExpr):
    def __init__(self, expr: CExpr, negated: bool) -> None:
        self.expr, self.negated = expr, negated

    def eval(self, row, rowids):
        v = self.expr.eval(row, rowids)
        res = v is None
        return (not res) if self.negated else res

    def has_column(self):
        return self.expr.has_column()


class CBetween(CExpr):
    def __init__(self, expr: CExpr, lo: CExpr, hi: CExpr, negated: bool):
        self.expr, self.lo, self.hi, self.negated = expr, lo, hi, negated

    def eval(self, row, rowids):
        v = self.expr.eval(row, rowids)
        lo = self.lo.eval(row, rowids)
        hi = self.hi.eval(row, rowids)
        if v is None:
            return None
        c1 = None if lo is None else sql_compare(v, lo)
        c2 = None if hi is None else sql_compare(v, hi)
        ge = None if c1 is None else c1 >= 0
        le = None if c2 is None else c2 <= 0
        if ge is False or le is False:
            res = False
        elif ge is None or le is None:
            return None
        else:
            res = True
        return (not res) if self.negated else res

    def has_column(self):
        return (self.expr.has_column() or self.lo.has_column()
                or self.hi.has_column())


class CInList(CExpr):
    def __init__(self, expr: CExpr, items: List[CExpr], negated: bool):
        self.expr, self.items, self.negated = expr, items, negated

    def eval(self, row, rowids):
        v = self.expr.eval(row, rowids)
        if v is None:
            return None
        found = False
        saw_null = False
        for item in self.items:
            w = item.eval(row, rowids)
            if w is None:
                saw_null = True
                continue
            if sql_compare(v, w) == 0:
                found = True
                break
        if found:
            return 0 if self.negated else 1
        if saw_null:
            return None
        return 1 if self.negated else 0

    def has_column(self):
        return self.expr.has_column() or any(i.has_column()
                                             for i in self.items)


class CInSelect(CExpr):
    """Uncorrelated ``x IN (SELECT ...)`` -- evaluated once, cached."""

    def __init__(self, expr: CExpr, select: A.Select, negated: bool,
                 outer: Optional[Scope]) -> None:
        self.expr, self.negated = expr, negated
        from .executor import run_subquery_values   # late import
        self._values = run_subquery_values(select, outer)
        self._set = set()
        self._has_null = False
        for v in self._values:
            if v is None:
                self._has_null = True
            else:
                self._set.add(group_key(v))

    def eval(self, row, rowids):
        v = self.expr.eval(row, rowids)
        if v is None:
            return None
        found = group_key(v) in self._set
        if found:
            return 0 if self.negated else 1
        if self._has_null:
            return None
        return 1 if self.negated else 0

    def has_column(self):
        return self.expr.has_column()


class CLike(CExpr):
    def __init__(self, expr: CExpr, pattern: CExpr, negated: bool) -> None:
        self.expr, self.pattern, self.negated = expr, pattern, negated

    def eval(self, row, rowids):
        v = self.expr.eval(row, rowids)
        p = self.pattern.eval(row, rowids)
        res = like_match(v, p)
        if res is None:
            return None
        return (not res) if self.negated else res

    def has_column(self):
        return self.expr.has_column() or self.pattern.has_column()


class CCase(CExpr):
    def __init__(self, operand: Optional[CExpr],
                 whens: List[Tuple[CExpr, CExpr]], else_: Optional[CExpr]):
        self.operand, self.whens, self.else_ = operand, whens, else_

    def eval(self, row, rowids):
        if self.operand is not None:
            base = self.operand.eval(row, rowids)
            for cond, res in self.whens:
                w = cond.eval(row, rowids)
                if base is not None and w is not None and \
                        sql_compare(base, w) == 0:
                    return res.eval(row, rowids)
        else:
            for cond, res in self.whens:
                if truthy(cond.eval(row, rowids)):
                    return res.eval(row, rowids)
        if self.else_ is not None:
            return self.else_.eval(row, rowids)
        return None

    def has_column(self):
        if self.operand is not None and self.operand.has_column():
            return True
        if self.else_ is not None and self.else_.has_column():
            return True
        return any(c.has_column() or r.has_column() for c, r in self.whens)


class CCast(CExpr):
    def __init__(self, expr: CExpr, type_name: str) -> None:
        self.expr, self.type_name = expr, type_name.upper()

    def eval(self, row, rowids):
        return cast_value(self.expr.eval(row, rowids), self.type_name)

    def has_column(self):
        return self.expr.has_column()


def cast_value(v: object, type_name: str) -> object:
    t = type_name
    if v is None:
        return None
    if "INT" in t:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            return _text_to_num_int(v)
        return 0
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return _to_text(v)
    if "BLOB" in t or not t:
        if isinstance(v, bytes):
            return v
        return _to_text(v).encode("utf-8")
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            n = _text_to_num(v)
            return float(n)
        return 0.0
    # NUMERIC
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return _text_to_num(v)
    return v


def _text_to_num_int(s: str) -> int:
    s = s.strip()
    sign = 1
    if s[:1] in "+-":
        if s[0] == "-":
            sign = -1
        s = s[1:]
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break
    return sign * int(digits) if digits else 0


class CScalarSubquery(CExpr):
    def __init__(self, select: A.Select, outer: Optional[Scope]) -> None:
        from .executor import run_scalar_subquery   # late import
        self._value = run_scalar_subquery(select, outer)

    def eval(self, row, rowids):
        return self._value


class CAgg(CExpr):
    """Placeholder for an aggregate result; the executor binds
    ``value`` from its precomputed agg list before evaluation."""

    def __init__(self, agg_index: int) -> None:
        self.agg_index = agg_index
        self.value: object = None

    def eval(self, row, rowids):
        return self.value


class CFunc(CExpr):
    """Scalar function call."""

    def __init__(self, name: str, args: List[CExpr]) -> None:
        self.name, self.args = name, args

    def eval(self, row, rowids):
        vals = [a.eval(row, rowids) for a in self.args]
        return call_scalar(self.name, vals)

    def has_column(self):
        return any(a.has_column() for a in self.args)


# --------------------------------------------------------------------------- #
# scalar functions
# --------------------------------------------------------------------------- #

def call_scalar(name: str, vals: List) -> object:
    n = name.upper()
    if n == "ABS":
        _arity(n, vals, 1)
        v = vals[0]
        if v is None:
            return None
        if isinstance(v, bool):
            v = int(v)
        if not isinstance(v, (int, float)):
            v = _text_to_num(v)
        return abs(v)
    if n == "ROUND":
        if not 1 <= len(vals) <= 2:
            raise SchemaError(f"wrong number of arguments to ROUND()")
        v = vals[0]
        if v is None or not isinstance(v, (int, float)):
            return None
        digits = 0
        if len(vals) == 2 and vals[1] is not None:
            digits = int(_num_nonnull(vals[1]))
        r = round(float(v), digits)
        return r if digits > 0 else round(r)
    if n == "UPPER":
        _arity(n, vals, 1)
        return None if vals[0] is None else _to_text(vals[0]).upper()
    if n == "LOWER":
        _arity(n, vals, 1)
        return None if vals[0] is None else _to_text(vals[0]).lower()
    if n == "LENGTH":
        _arity(n, vals, 1)
        v = vals[0]
        if v is None:
            return None
        if isinstance(v, (bytes, bytearray)):
            return len(v)
        return len(_to_text(v))
    if n in ("SUBSTR", "SUBSTRING"):
        if not 2 <= len(vals) <= 3:
            raise SchemaError(f"wrong number of arguments to {n}()")
        s = vals[0]
        if s is None or vals[1] is None:
            return None
        text = s if isinstance(s, str) else _to_text(s)
        start = int(_num_nonnull(vals[1]))
        length = None
        if len(vals) == 3:
            if vals[2] is None:
                return None
            length = int(_num_nonnull(vals[2]))
        if start > 0:
            begin = start - 1
        elif start == 0:
            begin = 0
        else:
            begin = max(len(text) + start, 0)
        if length is None:
            return text[begin:]
        if length < 0:
            length = 0
        return text[begin:begin + length]
    if n == "COALESCE":
        if not vals:
            raise SchemaError("COALESCE requires at least one argument")
        for v in vals:
            if v is not None:
                return v
        return None
    if n == "IFNULL":
        _arity(n, vals, 2)
        return vals[0] if vals[0] is not None else vals[1]
    if n == "NULLIF":
        _arity(n, vals, 2)
        if vals[0] is not None and vals[1] is not None and \
                sql_compare(vals[0], vals[1]) == 0:
            return None
        return vals[0]
    if n == "TYPEOF":
        _arity(n, vals, 1)
        v = vals[0]
        if v is None:
            return "null"
        if isinstance(v, bool) or isinstance(v, int):
            return "integer"
        if isinstance(v, float):
            return "real"
        if isinstance(v, str):
            return "text"
        return "blob"
    if n == "HEX":
        _arity(n, vals, 1)
        v = vals[0]
        if v is None:
            return None
        raw = v if isinstance(v, (bytes, bytearray)) else _to_text(v).encode()
        return raw.hex().upper()
    raise SchemaError(f"no such function: {name}")


def _arity(name: str, vals: List, k: int) -> None:
    if len(vals) != k:
        raise SchemaError(f"wrong number of arguments to {name}()")


def _num_nonnull(v: object):
    v = _num(v)
    if isinstance(v, (int, float)):
        return v
    return _text_to_num(str(v))


AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP_CONCAT", "TOTAL"}


def is_aggregate(expr: A.Expr) -> bool:
    if isinstance(expr, A.FuncCall):
        if expr.name.upper() in AGGREGATES:
            return True
        return any(is_aggregate(a) for a in expr.args)
    if isinstance(expr, A.Binary):
        return is_aggregate(expr.left) or is_aggregate(expr.right)
    if isinstance(expr, A.Unary):
        return is_aggregate(expr.operand)
    if isinstance(expr, A.IsNull):
        return is_aggregate(expr.expr)
    if isinstance(expr, A.Between):
        return any(is_aggregate(e) for e in (expr.expr, expr.lo, expr.hi))
    if isinstance(expr, A.InList):
        return is_aggregate(expr.expr) or any(is_aggregate(i)
                                              for i in expr.items)
    if isinstance(expr, A.Like):
        return is_aggregate(expr.expr) or is_aggregate(expr.pattern)
    if isinstance(expr, A.Case):
        parts = list(expr.whens and [w for pair in expr.whens for w in pair]
                     or [])
        if expr.operand:
            parts.append(expr.operand)
        if expr.else_:
            parts.append(expr.else_)
        return any(is_aggregate(p) for p in parts)
    if isinstance(expr, A.Cast):
        return is_aggregate(expr.expr)
    return False


# --------------------------------------------------------------------------- #
# compiler
# --------------------------------------------------------------------------- #

def compile_expr(expr: A.Expr, scope: Scope,
                 outer: Optional[Scope] = None,
                 agg_slots: Optional[dict] = None) -> CExpr:
    """Compile *expr*.  When *agg_slots* is given, aggregate calls are
    replaced with CAgg placeholders registered into it
    (FuncCall-ast -> slot index)."""
    if isinstance(expr, A.Literal):
        return CLit(expr.value)
    if isinstance(expr, A.ColumnRef):
        si, ci = scope.resolve(expr.name, expr.table)
        if ci == -1:
            return CRowid(si)
        alias, ts = scope.slots[si]
        if ts.rowid_alias and ts.rowid_alias.lower() == expr.name.lower():
            return CRowid(si)
        return CCol(si, ci, expr.name)
    if isinstance(expr, A.Unary):
        return CUnary(expr.op, compile_expr(expr.operand, scope, outer,
                                            agg_slots))
    if isinstance(expr, A.Binary):
        return CBinary(expr.op,
                       compile_expr(expr.left, scope, outer, agg_slots),
                       compile_expr(expr.right, scope, outer, agg_slots))
    if isinstance(expr, A.IsNull):
        return CIsNull(compile_expr(expr.expr, scope, outer, agg_slots),
                       expr.negated)
    if isinstance(expr, A.Between):
        return CBetween(compile_expr(expr.expr, scope, outer, agg_slots),
                        compile_expr(expr.lo, scope, outer, agg_slots),
                        compile_expr(expr.hi, scope, outer, agg_slots),
                        expr.negated)
    if isinstance(expr, A.InList):
        return CInList(compile_expr(expr.expr, scope, outer, agg_slots),
                       [compile_expr(i, scope, outer, agg_slots)
                        for i in expr.items], expr.negated)
    if isinstance(expr, A.InSelect):
        return CInSelect(compile_expr(expr.expr, scope, outer, agg_slots),
                         expr.select, expr.negated, scope)
    if isinstance(expr, A.Like):
        return CLike(compile_expr(expr.expr, scope, outer, agg_slots),
                     compile_expr(expr.pattern, scope, outer, agg_slots),
                     expr.negated)
    if isinstance(expr, A.Case):
        operand = (compile_expr(expr.operand, scope, outer, agg_slots)
                   if expr.operand else None)
        whens = [(compile_expr(c, scope, outer, agg_slots),
                  compile_expr(r, scope, outer, agg_slots))
                 for c, r in expr.whens]
        else_ = (compile_expr(expr.else_, scope, outer, agg_slots)
                 if expr.else_ else None)
        return CCase(operand, whens, else_)
    if isinstance(expr, A.Cast):
        return CCast(compile_expr(expr.expr, scope, outer, agg_slots),
                     expr.type_name)
    if isinstance(expr, A.ScalarSubquery):
        return CScalarSubquery(expr.select, scope)
    if isinstance(expr, A.FuncCall):
        name = expr.name.upper()
        if name in AGGREGATES:
            if agg_slots is None:
                raise SchemaError(f"misuse of aggregate function {name}()")
            key = id(expr)
            if key not in agg_slots:
                agg_slots[key] = len(agg_slots)
            return CAgg(agg_slots[key])
        if expr.star:
            raise SchemaError(f"{name}() takes no '*' argument")
        if expr.distinct:
            raise SchemaError(f"DISTINCT is only valid inside aggregates")
        return CFunc(expr.name,
                     [compile_expr(a, scope, outer, agg_slots)
                      for a in expr.args])
    if isinstance(expr, A.Star):
        raise SchemaError("'*' is not valid here")
    raise UnsupportedError(f"unsupported expression: {type(expr).__name__}")
