import pytest

from nimbus_db.errors import ParseError, UnsupportedError
from nimbus_db.parser import parse


def test_select_basic():
    stmt = parse("SELECT a, b AS x FROM t WHERE a > 1 ORDER BY b DESC LIMIT 5")
    assert stmt.items[1].alias == "x"
    assert stmt.order_by[0].desc
    assert stmt.limit.value == 5


def test_operator_precedence():
    # AND binds tighter than OR
    stmt = parse("SELECT 1 WHERE a OR b AND c")
    where = stmt.where
    assert where.op == "OR"
    assert where.right.op == "AND"


def test_arithmetic_precedence():
    stmt = parse("SELECT 1 WHERE a + b * c < d")
    # < is top; left side: a + (b * c)
    lt = stmt.where
    assert lt.op == "<"
    assert lt.left.op == "+"
    assert lt.left.right.op == "*"


def test_string_escapes():
    stmt = parse("SELECT 'it''s'")
    assert stmt.items[0].expr.value == "it's"


def test_quoted_identifiers():
    stmt = parse('SELECT "weird col" FROM "my table"')
    assert stmt.items[0].expr.name == "weird col"
    assert stmt.from_.name == "my table"


def test_comments():
    stmt = parse("SELECT 1 -- trailing comment\n+ 2")
    assert stmt.items[0].expr.op == "+"


@pytest.mark.parametrize("sql", [
    "SELECT", "SELECT FROM", "SELECT 1 FROM", "INSERT INTO t",
    "UPDATE t SET", "DELETE", "CREATE TABLE", "SELECT 1 +",
])
def test_parse_errors(sql):
    with pytest.raises(ParseError):
        parse(sql)


def test_error_position():
    with pytest.raises(ParseError) as exc:
        parse("SELECT 1;\nSELECT FROM WHERE")
    assert "line 2" in str(exc.value)


def test_unsupported_fk():
    with pytest.raises(UnsupportedError):
        parse("CREATE TABLE a (b INT REFERENCES c (id))")


def test_create_table_shapes():
    stmt = parse("""CREATE TABLE t (
        a INT PRIMARY KEY,
        b TEXT NOT NULL UNIQUE,
        c REAL DEFAULT 1.5,
        d VARCHAR(20),
        PRIMARY KEY (a),
        UNIQUE (b, d)
    )""")
    assert stmt.name == "t"
    assert stmt.columns[1].not_null
    assert stmt.columns[2].default.value == 1.5
    kinds = [c.kind for c in stmt.constraints]
    assert "PK" in kinds and "UNIQUE" in kinds


def test_insert_variants():
    stmt = parse("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    assert stmt.columns == ["a", "b"]
    assert len(stmt.rows) == 2
    assert stmt.rows[1][1].value == "y"


def test_expression_breadth():
    parse("SELECT CASE WHEN a THEN b ELSE c END, CAST(a AS INTEGER), "
          "a BETWEEN 1 AND 10, a IN (1, 2, 3), a NOT LIKE 'x%', "
          "a IS NOT NULL, NOT a, -a, a || 'b', COUNT(DISTINCT a) "
          "FROM t GROUP BY a HAVING COUNT(*) > 1")
    parse("SELECT * FROM (SELECT a FROM t) sub WHERE sub.a > 0")
    parse("SELECT * FROM t LEFT JOIN u ON t.a = u.a CROSS JOIN v")
    parse("SELECT a FROM t UNION SELECT b FROM u ORDER BY 1 LIMIT 3 OFFSET 2")
    parse("SELECT a FROM t UNION ALL SELECT b FROM u")


def test_script_parsing():
    from nimbus_db.parser import parse_script
    stmts = parse_script("SELECT 1; SELECT 2; SELECT 3")
    assert len(stmts) == 3
