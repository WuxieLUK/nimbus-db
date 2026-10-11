"""AST node definitions.

Plain dataclasses; the parser builds them, the planner resolves column
references against schemas and compiles them into executable plans.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# expressions
# --------------------------------------------------------------------------- #

@dataclass
class Expr:
    pass


@dataclass
class Literal(Expr):
    value: Any


@dataclass
class ColumnRef(Expr):
    table: Optional[str]   # qualifier (alias) if written as t.c
    name: str


@dataclass
class Star(Expr):
    """``*`` or ``t.*`` — only valid in a select list or COUNT(*)."""
    table: Optional[str] = None


@dataclass
class Unary(Expr):
    op: str                # '-', '+', 'NOT'
    operand: Expr


@dataclass
class Binary(Expr):
    op: str                # + - * / % || = <> < <= > >= AND OR
    left: Expr
    right: Expr


@dataclass
class IsNull(Expr):
    expr: Expr
    negated: bool = False


@dataclass
class Between(Expr):
    expr: Expr
    lo: Expr
    hi: Expr
    negated: bool = False


@dataclass
class InList(Expr):
    expr: Expr
    items: List[Expr]
    negated: bool = False


@dataclass
class InSelect(Expr):
    expr: Expr
    select: "Select"
    negated: bool = False


@dataclass
class Like(Expr):
    expr: Expr
    pattern: Expr
    negated: bool = False


@dataclass
class FuncCall(Expr):
    name: str
    args: List[Expr]
    star: bool = False          # COUNT(*)
    distinct: bool = False      # COUNT(DISTINCT x), SUM(DISTINCT x), ...


@dataclass
class Case(Expr):
    operand: Optional[Expr]
    whens: List[Tuple[Expr, Expr]]
    else_: Optional[Expr]


@dataclass
class Cast(Expr):
    expr: Expr
    type_name: str


@dataclass
class ScalarSubquery(Expr):
    select: "Select"


# --------------------------------------------------------------------------- #
# SELECT
# --------------------------------------------------------------------------- #

@dataclass
class SelectItem:
    expr: Expr                 # Star allowed here
    alias: Optional[str] = None


@dataclass
class TableRef:
    name: str                  # base table name, or alias of a subquery
    alias: Optional[str] = None
    subquery: Optional["Select"] = None   # FROM (SELECT ...) AS t


@dataclass
class Join:
    kind: str                  # 'INNER' | 'LEFT' | 'CROSS'
    table: TableRef
    on: Optional[Expr] = None


@dataclass
class OrderItem:
    expr: Expr
    desc: bool = False


@dataclass
class Select:
    distinct: bool = False
    items: List[SelectItem] = field(default_factory=list)
    from_: Optional[TableRef] = None
    joins: List[Join] = field(default_factory=list)
    where: Optional[Expr] = None
    group_by: List[Expr] = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: List[OrderItem] = field(default_factory=list)
    limit: Optional[Expr] = None
    offset: Optional[Expr] = None
    # compound SELECT: <this select> UNION [ALL] <union_other>
    union_op: Optional[str] = None
    union_other: Optional["Select"] = None


# --------------------------------------------------------------------------- #
# DDL / DML / transactions
# --------------------------------------------------------------------------- #

@dataclass
class ColumnDef:
    name: str
    type_decl: str
    not_null: bool = False
    pk: bool = False
    unique: bool = False
    default: Optional[Expr] = None
    references: Optional[Tuple[str, Optional[str]]] = None


@dataclass
class TableConstraint:
    kind: str                  # 'PK' | 'UNIQUE' | 'CHECK' | 'FK'
    columns: List[str] = field(default_factory=list)
    expr: Optional[Expr] = None                # CHECK
    references: Optional[Tuple[str, List[str]]] = None


@dataclass
class CreateTable:
    name: str
    columns: List[ColumnDef]
    constraints: List[TableConstraint]
    if_not_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    columns: List[str]
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: Optional[List[str]]
    rows: List[List[Expr]]


@dataclass
class Update:
    table: str
    sets: List[Tuple[str, Expr]]
    where: Optional[Expr]


@dataclass
class Delete:
    table: str
    where: Optional[Expr]


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Explain:
    stmt: object


Statement = object
