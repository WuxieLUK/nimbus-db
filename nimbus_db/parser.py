"""Recursive-descent SQL parser.

Precedence (low -> high, mirroring sqlite.org/lang_expr.html):

    OR  <  AND  <  NOT  <  = != <> IS IN LIKE BETWEEN  <  < <= > >=
    <  + -  <  * / %  <  ||  <  unary - +  <  primary

Produces the dataclasses in :mod:`ast_nodes`.  Unknown-but-valid SQL
raises ``UnsupportedError`` (e.g. FOREIGN KEY) so the CLI can print an
honest message instead of a misleading parse error.
"""
from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as A
from .errors import ParseError, UnsupportedError
from .lexer import Token, TokenType, tokenize

# words that terminate identifiers in contexts where a keyword could
# also appear (statement keywords, clause keywords, constraint words)
RESERVED = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT",
    "OFFSET", "AS", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER",
    "CROSS", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN",
    "CASE", "WHEN", "THEN", "ELSE", "END", "CREATE", "TABLE", "DROP",
    "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "BEGIN",
    "COMMIT", "ROLLBACK", "EXPLAIN", "PRIMARY", "KEY", "UNIQUE", "DEFAULT",
    "REFERENCES", "CHECK", "FOREIGN", "INDEX", "IF", "EXISTS", "DISTINCT",
    "ALL", "UNION", "INTERSECT", "EXCEPT", "TRUE", "FALSE", "CAST",
    "TRANSACTION",
}

_CONSTRAINT_WORDS = {
    "PRIMARY", "NOT", "NULL", "UNIQUE", "DEFAULT", "REFERENCES", "CHECK",
    "CONSTRAINT",
}


class Parser:
    def __init__(self, sql: str) -> None:
        self._toks = tokenize(sql)
        self._i = 0

    # ------------------------------------------------------------------ #
    # token utilities
    # ------------------------------------------------------------------ #

    def _peek(self, k: int = 0) -> Token:
        return self._toks[min(self._i + k, len(self._toks) - 1)]

    def _next(self) -> Token:
        t = self._toks[self._i]
        if t.type is not TokenType.EOF:
            self._i += 1
        return t

    def _err(self, msg: str, tok: Optional[Token] = None) -> None:
        tok = tok or self._peek()
        shown = repr(tok.value) if tok.value is not None else "end of input"
        raise ParseError(f"{msg} (got {shown} at {tok.where})")

    def _at_kw(self, *words: str) -> bool:
        return self._peek().is_kw(*words)

    def _take_kw(self, *words: str) -> Optional[Token]:
        if self._at_kw(*words):
            return self._next()
        return None

    def _expect_kw(self, word: str) -> Token:
        t = self._take_kw(word)
        if t is None:
            self._err(f"expected {word}")
        return t

    def _at_op(self, *ops: str) -> bool:
        t = self._peek()
        return t.type is TokenType.OP and t.value in ops

    def _take_op(self, *ops: str) -> Optional[Token]:
        if self._at_op(*ops):
            return self._next()
        return None

    def _expect_op(self, op: str) -> Token:
        t = self._take_op(op)
        if t is None:
            self._err(f"expected '{op}'")
        return t

    def _expect_ident(self, what: str = "identifier") -> str:
        t = self._peek()
        if t.type is TokenType.IDENT and (t.value or "").upper() not in RESERVED:
            return self._next().value  # type: ignore[return-value]
        self._err(f"expected {what}")

    def _at_reserved(self) -> bool:
        t = self._peek()
        return t.type is TokenType.IDENT and (t.value or "").upper() in RESERVED

    # ------------------------------------------------------------------ #
    # statement entry points
    # ------------------------------------------------------------------ #

    def parse_statement(self) -> object:
        stmt = self._parse_statement()
        self._take_op(";")
        if self._peek().type is not TokenType.EOF:
            self._err("unexpected trailing input after statement")
        return stmt

    def parse_script(self) -> List[object]:
        stmts: List[object] = []
        while True:
            while self._take_op(";"):
                pass
            if self._peek().type is TokenType.EOF:
                return stmts
            stmts.append(self._parse_statement())
            if not self._take_op(";") and self._peek().type is not TokenType.EOF:
                self._err("expected ';' between statements")

    def _parse_statement(self) -> object:
        t = self._peek()
        if t.type is not TokenType.IDENT:
            self._err("expected SQL statement")
        w = (t.value or "").upper()
        if w == "SELECT":
            return self._parse_select()
        if w == "CREATE":
            return self._parse_create()
        if w == "DROP":
            return self._parse_drop()
        if w == "INSERT":
            return self._parse_insert()
        if w == "UPDATE":
            return self._parse_update()
        if w == "DELETE":
            return self._parse_delete()
        if w == "BEGIN":
            self._next()
            self._take_kw("TRANSACTION")
            return A.Begin()
        if w == "COMMIT":
            self._next()
            self._take_kw("TRANSACTION")
            return A.Commit()
        if w == "ROLLBACK":
            self._next()
            self._take_kw("TRANSACTION")
            return A.Rollback()
        if w == "EXPLAIN":
            self._next()
            return A.Explain(self._parse_statement())
        self._err(f"unsupported statement {w!r}")

    # ------------------------------------------------------------------ #
    # SELECT
    # ------------------------------------------------------------------ #

    def _parse_select(self) -> A.Select:
        first = self._parse_select_core()

        union_op: Optional[str] = None
        other: Optional[A.Select] = None
        if self._at_kw("UNION"):
            self._next()
            union_op = "UNION ALL" if self._take_kw("ALL") else "UNION"
            other = self._parse_select_core()
        if self._at_kw("INTERSECT", "EXCEPT"):
            raise UnsupportedError("INTERSECT / EXCEPT are not supported")

        order_by: List[A.OrderItem] = []
        limit = offset = None
        if self._at_kw("ORDER"):
            self._next()
            self._expect_kw("BY")
            order_by.append(self._parse_order_item())
            while self._take_op(","):
                order_by.append(self._parse_order_item())
        if self._at_kw("LIMIT"):
            self._next()
            limit = self._parse_expr()
            if self._take_op(","):          # LIMIT offset, count
                offset = limit
                limit = self._parse_expr()
            elif self._take_kw("OFFSET"):
                offset = self._parse_expr()
        if self._at_kw("OFFSET"):
            self._err("OFFSET requires a LIMIT clause")

        first.order_by = order_by
        first.limit = limit
        first.offset = offset
        first.union_op = union_op
        first.union_other = other
        return first

    def _parse_order_item(self) -> A.OrderItem:
        expr = self._parse_expr()
        desc = False
        if self._take_kw("DESC"):
            desc = True
        else:
            self._take_kw("ASC")
        return A.OrderItem(expr, desc)

    def _parse_select_core(self) -> A.Select:
        self._expect_kw("SELECT")
        distinct = False
        if self._take_kw("DISTINCT"):
            distinct = True
        else:
            self._take_kw("ALL")

        items = [self._parse_select_item()]
        while self._take_op(","):
            items.append(self._parse_select_item())

        from_: Optional[A.TableRef] = None
        joins: List[A.Join] = []
        if self._take_kw("FROM"):
            from_, joins = self._parse_from()

        where = self._parse_expr() if self._take_kw("WHERE") else None

        group_by: List[A.Expr] = []
        if self._take_kw("GROUP"):
            self._expect_kw("BY")
            group_by.append(self._parse_expr())
            while self._take_op(","):
                group_by.append(self._parse_expr())
        if self._at_kw("HAVING") and not group_by:
            self._err("HAVING requires GROUP BY")
        having = self._parse_expr() if self._take_kw("HAVING") else None

        return A.Select(distinct=distinct, items=items, from_=from_,
                        joins=joins, where=where, group_by=group_by,
                        having=having)

    def _parse_select_item(self) -> A.SelectItem:
        if self._take_op("*"):
            return A.SelectItem(A.Star())
        t = self._peek()
        if (t.type is TokenType.IDENT and self._peek(1).type is TokenType.OP
                and self._peek(1).value == "." and self._peek(2).type is TokenType.OP
                and self._peek(2).value == "*"):
            self._next()
            self._next()
            self._next()
            return A.SelectItem(A.Star(table=str(t.value)))
        expr = self._parse_expr()
        alias: Optional[str] = None
        if self._take_kw("AS"):
            alias = self._expect_ident("column alias")
        elif (self._peek().type is TokenType.IDENT
              and (self._peek().value or "").upper() not in RESERVED):
            alias = self._next().value  # type: ignore[assignment]
        return A.SelectItem(expr, alias)

    def _parse_from(self):
        base = self._parse_table_ref()
        joins: List[A.Join] = []
        while True:
            if self._take_op(","):
                joins.append(A.Join("CROSS", self._parse_table_ref()))
            elif self._at_kw("JOIN"):
                self._next()
                table = self._parse_table_ref()
                self._expect_kw("ON")
                joins.append(A.Join("INNER", table, self._parse_expr()))
            elif self._at_kw("INNER"):
                self._next()
                self._expect_kw("JOIN")
                table = self._parse_table_ref()
                self._expect_kw("ON")
                joins.append(A.Join("INNER", table, self._parse_expr()))
            elif self._at_kw("CROSS"):
                self._next()
                self._expect_kw("JOIN")
                joins.append(A.Join("CROSS", self._parse_table_ref()))
            elif self._at_kw("LEFT"):
                self._next()
                self._take_kw("OUTER")
                self._expect_kw("JOIN")
                table = self._parse_table_ref()
                self._expect_kw("ON")
                joins.append(A.Join("LEFT", table, self._parse_expr()))
            elif self._at_kw("RIGHT", "FULL", "OUTER", "NATURAL"):
                raise UnsupportedError(
                    "RIGHT / FULL / NATURAL joins are not supported")
            else:
                return base, joins

    def _parse_table_ref(self) -> A.TableRef:
        if self._take_op("("):
            sub = self._parse_select()
            self._expect_op(")")
            self._take_kw("AS")
            alias = self._expect_ident("subquery alias")
            return A.TableRef(name=alias, alias=alias, subquery=sub)
        name = self._expect_ident("table name")
        alias: Optional[str] = None
        if self._take_kw("AS"):
            alias = self._expect_ident("table alias")
        elif (self._peek().type is TokenType.IDENT
              and (self._peek().value or "").upper() not in RESERVED):
            alias = self._next().value  # type: ignore[assignment]
        return A.TableRef(name=name, alias=alias)

    # ------------------------------------------------------------------ #
    # expressions
    # ------------------------------------------------------------------ #

    def _parse_expr(self) -> A.Expr:
        return self._parse_or()

    def _parse_or(self) -> A.Expr:
        left = self._parse_and()
        while self._take_kw("OR"):
            left = A.Binary("OR", left, self._parse_and())
        return left

    def _parse_and(self) -> A.Expr:
        left = self._parse_not()
        while self._take_kw("AND"):
            left = A.Binary("AND", left, self._parse_not())
        return left

    def _parse_not(self) -> A.Expr:
        if self._take_kw("NOT"):
            return A.Unary("NOT", self._parse_not())
        return self._parse_eq_like()

    def _parse_eq_like(self) -> A.Expr:
        left = self._parse_relational()
        while True:
            negated = False
            if self._at_kw("NOT") and self._peek(1).is_kw(
                    "IN", "LIKE", "BETWEEN"):
                self._next()
                negated = True
            if self._at_op("=", "==", "!=", "<>"):
                op = str(self._next().value)
                left = A.Binary(op, left, self._parse_relational())
            elif self._at_kw("IS"):
                self._next()
                neg = bool(self._take_kw("NOT"))
                self._expect_kw("NULL")
                left = A.IsNull(left, neg)
            elif self._at_kw("IN"):
                self._next()
                left = self._parse_in_tail(left, negated)
            elif self._at_kw("LIKE"):
                self._next()
                left = A.Like(left, self._parse_relational(), negated)
            elif self._at_kw("BETWEEN"):
                self._next()
                lo = self._parse_relational()
                self._expect_kw("AND")
                hi = self._parse_relational()
                left = A.Between(left, lo, hi, negated)
            else:
                if negated:
                    self._err("expected IN, LIKE or BETWEEN after NOT")
                return left

    def _parse_in_tail(self, left: A.Expr, negated: bool) -> A.Expr:
        self._expect_op("(")
        if self._at_kw("SELECT"):
            sub = self._parse_select()
            self._expect_op(")")
            return A.InSelect(left, sub, negated)
        items = [self._parse_expr()]
        while self._take_op(","):
            items.append(self._parse_expr())
        self._expect_op(")")
        return A.InList(left, items, negated)

    def _parse_relational(self) -> A.Expr:
        left = self._parse_additive()
        while self._at_op("<", "<=", ">", ">="):
            op = str(self._next().value)
            left = A.Binary(op, left, self._parse_additive())
        return left

    def _parse_additive(self) -> A.Expr:
        left = self._parse_multiplicative()
        while self._at_op("+", "-"):
            op = str(self._next().value)
            left = A.Binary(op, left, self._parse_multiplicative())
        return left

    def _parse_multiplicative(self) -> A.Expr:
        left = self._parse_concat()
        while self._at_op("*", "/", "%"):
            op = str(self._next().value)
            left = A.Binary(op, left, self._parse_concat())
        return left

    def _parse_concat(self) -> A.Expr:
        left = self._parse_unary()
        while self._at_op("||"):
            self._next()
            left = A.Binary("||", left, self._parse_unary())
        return left

    def _parse_unary(self) -> A.Expr:
        if self._at_op("-", "+"):
            op = str(self._next().value)
            operand = self._parse_unary()
            if op == "-" and isinstance(operand, A.Literal) and isinstance(
                    operand.value, (int, float)):
                return A.Literal(-operand.value)
            return A.Unary(op, operand)
        return self._parse_primary()

    def _parse_primary(self) -> A.Expr:
        t = self._peek()

        if self._take_op("("):
            if self._at_kw("SELECT"):
                sub = self._parse_select()
                self._expect_op(")")
                return A.ScalarSubquery(sub)
            expr = self._parse_expr()
            self._expect_op(")")
            return expr

        if t.type is TokenType.INT:
            self._next()
            return A.Literal(t.value)
        if t.type is TokenType.FLOAT:
            self._next()
            return A.Literal(t.value)
        if t.type is TokenType.STRING:
            self._next()
            return A.Literal(t.value)

        if t.type is TokenType.IDENT:
            word = (t.value or "").upper()
            if word == "NULL":
                self._next()
                return A.Literal(None)
            if word == "TRUE":
                self._next()
                return A.Literal(1)
            if word == "FALSE":
                self._next()
                return A.Literal(0)
            if word == "CASE":
                self._next()
                return self._parse_case()
            if word == "CAST":
                self._next()
                return self._parse_cast()
            if word in RESERVED and word not in ("ROWID", "_ROWID_", "OID"):
                self._err(f"unexpected keyword {word!r} in expression")
            # function call: IDENT '('
            if self._peek(1).type is TokenType.OP and self._peek(1).value == "(":
                return self._parse_function_call()
            # column reference
            self._next()
            if (self._at_op(".") and self._peek(1).type is TokenType.IDENT
                    and (self._peek(1).value or "").upper() not in RESERVED):
                self._next()
                return A.ColumnRef(str(t.value), str(self._next().value))
            return A.ColumnRef(None, str(t.value))

        self._err("expected expression")

    def _parse_function_call(self) -> A.Expr:
        name = str(self._next().value)
        self._expect_op("(")
        star = False
        distinct = False
        args: List[A.Expr] = []
        if self._at_op("*"):
            self._next()
            star = True
        elif not self._at_op(")"):
            if self._take_kw("DISTINCT"):
                distinct = True
            args.append(self._parse_expr())
            while self._take_op(","):
                args.append(self._parse_expr())
        self._expect_op(")")
        return A.FuncCall(name=name, args=args, star=star, distinct=distinct)

    def _parse_case(self) -> A.Expr:
        operand: Optional[A.Expr] = None
        if not self._at_kw("WHEN"):
            operand = self._parse_expr()
        whens: List[tuple] = []
        while self._take_kw("WHEN"):
            cond = self._parse_expr()
            self._expect_kw("THEN")
            whens.append((cond, self._parse_expr()))
        if not whens:
            self._err("CASE requires at least one WHEN clause")
        else_ = self._parse_expr() if self._take_kw("ELSE") else None
        self._expect_kw("END")
        return A.Case(operand, whens, else_)

    def _parse_cast(self) -> A.Expr:
        self._expect_op("(")
        expr = self._parse_expr()
        self._expect_kw("AS")
        type_name = self._parse_type_name()
        self._expect_op(")")
        return A.Cast(expr, type_name)

    # ------------------------------------------------------------------ #
    # DDL
    # ------------------------------------------------------------------ #

    def _parse_type_name(self) -> str:
        parts = [self._expect_ident("type name")]
        while (self._peek().type is TokenType.IDENT
               and (self._peek().value or "").upper() not in _CONSTRAINT_WORDS
               and (self._peek().value or "").upper() not in RESERVED - {"KEY"}):
            parts.append(str(self._next().value))
        if self._take_op("("):
            n1 = self._peek()
            if n1.type is not TokenType.INT:
                self._err("expected integer in type size")
            self._next()
            size = f"({n1.value}"
            if self._take_op(","):
                n2 = self._peek()
                if n2.type is not TokenType.INT:
                    self._err("expected integer in type size")
                self._next()
                size += f",{n2.value}"
            self._expect_op(")")
            parts.append(size + ")")
        return " ".join(parts)

    def _parse_create(self) -> object:
        self._expect_kw("CREATE")
        if self._at_kw("TEMP", "TEMPORARY", "VIEW", "TRIGGER"):
            raise UnsupportedError(
                "TEMPORARY tables, VIEWS and TRIGGERS are not supported")
        unique = bool(self._take_kw("UNIQUE"))
        if not self._take_kw("INDEX"):
            unique = False
            self._expect_kw("TABLE")
            return self._parse_create_table()
        return self._parse_create_index(unique)

    def _parse_create_table(self) -> A.CreateTable:
        if_not_exists = False
        if self._take_kw("IF"):
            self._expect_kw("NOT")
            self._expect_kw("EXISTS")
            if_not_exists = True
        name = self._expect_ident("table name")
        self._expect_op("(")

        columns: List[A.ColumnDef] = []
        constraints: List[A.TableConstraint] = []
        while True:
            if self._at_kw("PRIMARY", "UNIQUE", "CHECK", "FOREIGN",
                           "CONSTRAINT"):
                constraints.append(self._parse_table_constraint())
            else:
                columns.append(self._parse_column_def())
            if not self._take_op(","):
                break
        self._expect_op(")")
        if not columns:
            self._err("table must have at least one column")
        return A.CreateTable(name, columns, constraints,
                             if_not_exists=if_not_exists)

    def _parse_column_def(self) -> A.ColumnDef:
        name = self._expect_ident("column name")
        type_decl = ""
        if not self._at_kw(*_CONSTRAINT_WORDS) and not self._at_op(",", ")"):
            type_decl = self._parse_type_name()

        col = A.ColumnDef(name=name, type_decl=type_decl)
        while True:
            if self._take_kw("CONSTRAINT"):
                self._expect_ident("constraint name")  # named, then ignored
                continue
            if self._at_kw("PRIMARY"):
                self._next()
                self._expect_kw("KEY")
                col.pk = True
                self._take_kw("ASC", "DESC")
            elif self._at_kw("NOT"):
                self._next()
                self._expect_kw("NULL")
                col.not_null = True
            elif self._at_kw("NULL"):
                self._next()  # explicit NULL-able, the default
            elif self._at_kw("UNIQUE"):
                self._next()
                col.unique = True
            elif self._at_kw("DEFAULT"):
                self._next()
                if self._take_op("("):
                    col.default = self._parse_expr()
                    self._expect_op(")")
                else:
                    col.default = self._parse_unary()
            elif self._at_kw("REFERENCES"):
                raise UnsupportedError(
                    "FOREIGN KEY constraints are not supported; enforce "
                    "referential integrity in application code")
            elif self._at_kw("CHECK"):
                raise UnsupportedError("CHECK constraints are not supported")
            else:
                return col

    def _parse_table_constraint(self) -> A.TableConstraint:
        if self._take_kw("CONSTRAINT"):
            self._expect_ident("constraint name")  # named, then ignored

        if self._take_kw("PRIMARY"):
            self._expect_kw("KEY")
            cols = self._parse_paren_ident_list()
            return A.TableConstraint(kind="PK", columns=cols)
        if self._take_kw("UNIQUE"):
            cols = self._parse_paren_ident_list()
            return A.TableConstraint(kind="UNIQUE", columns=cols)
        if self._at_kw("CHECK"):
            raise UnsupportedError("CHECK constraints are not supported")
        if self._at_kw("FOREIGN"):
            raise UnsupportedError(
                "FOREIGN KEY constraints are not supported; enforce "
                "referential integrity in application code")
        self._err("expected table constraint")

    def _parse_paren_ident_list(self) -> List[str]:
        self._expect_op("(")
        cols = [self._expect_ident("column name")]
        while self._take_op(","):
            cols.append(self._expect_ident("column name"))
        self._expect_op(")")
        return cols

    def _parse_create_index(self, unique: bool) -> A.CreateIndex:
        if_not_exists = False
        if self._take_kw("IF"):
            self._expect_kw("NOT")
            self._expect_kw("EXISTS")
            if_not_exists = True
        name = self._expect_ident("index name")
        self._expect_kw("ON")
        table = self._expect_ident("table name")
        columns = self._parse_paren_ident_list()
        if self._at_kw("WHERE"):
            raise UnsupportedError("partial indexes are not supported")
        return A.CreateIndex(name, table, columns, unique=unique,
                             if_not_exists=if_not_exists)

    def _parse_drop(self) -> object:
        self._expect_kw("DROP")
        if self._take_kw("TABLE"):
            if_exists = False
            if self._take_kw("IF"):
                self._expect_kw("EXISTS")
                if_exists = True
            return A.DropTable(self._expect_ident("table name"),
                               if_exists=if_exists)
        if self._take_kw("INDEX"):
            if_exists = False
            if self._take_kw("IF"):
                self._expect_kw("EXISTS")
                if_exists = True
            return A.DropIndex(self._expect_ident("index name"),
                               if_exists=if_exists)
        self._err("expected TABLE or INDEX after DROP")

    # ------------------------------------------------------------------ #
    # DML
    # ------------------------------------------------------------------ #

    def _parse_insert(self) -> A.Insert:
        self._expect_kw("INSERT")
        self._expect_kw("INTO")
        table = self._expect_ident("table name")
        columns: Optional[List[str]] = None
        if self._take_op("("):
            columns = [self._expect_ident("column name")]
            while self._take_op(","):
                columns.append(self._expect_ident("column name"))
            self._expect_op(")")
        if self._at_kw("SELECT"):
            self._err("INSERT ... SELECT is not supported; use VALUES")
        self._expect_kw("VALUES")
        rows: List[List[A.Expr]] = []
        while True:
            self._expect_op("(")
            rows.append(self._parse_expr_list())
            self._expect_op(")")
            if not self._take_op(","):
                break
        return A.Insert(table=table, columns=columns, rows=rows)

    def _parse_expr_list(self) -> List[A.Expr]:
        if self._at_op(")"):
            return []
        items = [self._parse_expr()]
        while self._take_op(","):
            items.append(self._parse_expr())
        return items

    def _parse_update(self) -> A.Update:
        self._expect_kw("UPDATE")
        table = self._expect_ident("table name")
        self._expect_kw("SET")
        sets: List[tuple] = []
        while True:
            col = self._expect_ident("column name")
            self._expect_op("=")
            sets.append((col, self._parse_expr()))
            if not self._take_op(","):
                break
        where = self._parse_expr() if self._take_kw("WHERE") else None
        return A.Update(table=table, sets=sets, where=where)

    def _parse_delete(self) -> A.Delete:
        self._expect_kw("DELETE")
        self._expect_kw("FROM")
        table = self._expect_ident("table name")
        where = self._parse_expr() if self._take_kw("WHERE") else None
        return A.Delete(table=table, where=where)


def parse(sql: str) -> object:
    """Parse a single SQL statement."""
    return Parser(sql).parse_statement()


def parse_script(sql: str) -> List[object]:
    """Parse a sequence of SQL statements."""
    return Parser(sql).parse_script()
