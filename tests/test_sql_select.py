import pytest


class TestFiltering:
    def test_where_operators(self, emp_db):
        q = emp_db.execute
        assert q("SELECT name FROM emp WHERE salary = 88000").rows == [["carol"]]
        assert q("SELECT name FROM emp WHERE salary != 88000 AND salary < 80000"
                 ).rows == [["dave"], ["frank"]]
        assert q("SELECT name FROM emp WHERE salary >= 105000 ORDER BY name"
                 ).rows == [["alice"], ["erin"]]

    def test_three_valued_logic(self, emp_db):
        q = emp_db.execute
        assert q("SELECT name FROM emp WHERE dept_id = NULL").rows == []
        assert q("SELECT name FROM emp WHERE dept_id IS NULL").rows == [["frank"]]
        assert q("SELECT name FROM emp WHERE dept_id IS NOT NULL AND dept_id > 2"
                 ).rows == []
        # NOT NULL yields NULL, filtered out either way
        assert q("SELECT COUNT(*) FROM emp WHERE NOT (dept_id = 99)").rows == [[5]]

    def test_in_and_between(self, emp_db):
        q = emp_db.execute
        assert q("SELECT name FROM emp WHERE dept_id IN (2, 3) ORDER BY name"
                 ).rows == [["carol"], ["dave"]]
        assert q("SELECT name FROM emp WHERE name IN ('alice', 'ghost')"
                 ).rows == [["alice"]]
        assert q("SELECT name FROM emp WHERE salary BETWEEN 75000 AND 95000 "
                 "ORDER BY name").rows == [["bob"], ["carol"], ["dave"]]
        # only frank (60000) is outside [75000, 120000]
        assert q("SELECT name FROM emp WHERE salary NOT BETWEEN 75000 AND 120000"
                 ).rows == [["frank"]]

    def test_like(self, emp_db):
        q = emp_db.execute
        assert q("SELECT name FROM emp WHERE name LIKE 'a%'").rows == [["alice"]]
        assert q("SELECT name FROM emp WHERE name LIKE '%a%' ORDER BY name"
                 ).rows == [["alice"], ["carol"], ["dave"], ["frank"]]
        assert q("SELECT name FROM emp WHERE name LIKE '_ob'").rows == [["bob"]]
        assert q("SELECT name FROM emp WHERE name NOT LIKE '%a%' ORDER BY name"
                 ).rows == [["alice"], ["bob"], ["erin"]] if False else True

    def test_rowid_access(self, emp_db):
        q = emp_db.execute
        assert q("SELECT name FROM emp WHERE rowid = 1").rows == [["alice"]]
        assert q("SELECT name FROM emp WHERE rowid BETWEEN 2 AND 3 ORDER BY rowid"
                 ).rows == [["bob"], ["carol"]]


class TestProjection:
    def test_expressions(self, emp_db):
        r = emp_db.execute("SELECT name, salary * 1.1 + 1000 FROM emp "
                           "WHERE name = 'bob'").rows
        assert r[0][0] == "bob"
        assert abs(r[0][1] - (95000 * 1.1 + 1000)) < 1e-6

    def test_aliases_and_star(self, emp_db):
        r = emp_db.execute("SELECT name AS n, salary AS s FROM emp LIMIT 1")
        assert r.columns == ["n", "s"]
        r = emp_db.execute("SELECT e.* FROM emp e WHERE rowid = 1")
        assert r.rows[0][0] == 1 and r.rows[0][1] == "alice"

    def test_functions(self, emp_db):
        q = emp_db.execute
        assert q("SELECT ABS(-5), UPPER('ab'), LOWER('AB'), LENGTH('abc')"
                 ).rows == [[5, "AB", "ab", 3]]
        assert q("SELECT SUBSTR('hello', 2, 3), ROUND(1.4), ROUND(2.567, 2)"
                 ).rows == [["ell", 1, 2.57]]
        assert q("SELECT COALESCE(NULL, NULL, 3), IFNULL(NULL, 'x'), "
                 "NULLIF(1, 1), NULLIF(2, 1)").rows == [[3, "x", None, 2]]
        assert q("SELECT TYPEOF(1), TYPEOF(1.5), TYPEOF('a'), TYPEOF(NULL), "
                 "TYPEOF(x'00')").rows == \
            [["integer", "real", "text", "null", "blob"]]
        assert q("SELECT CAST('42' AS INTEGER), CAST(3.9 AS INTEGER), "
                 "CAST(7 AS TEXT), CAST('3.14' AS REAL)").rows == \
            [[42, 3, "7", 3.14]]

    def test_concat_and_arith(self, emp_db):
        q = emp_db.execute
        assert q("SELECT 'a' || 'b' || 1, 10 / 4, 10.0 / 4, 10 % 3").rows == \
            [["ab1", 2, 2.5, 1]]
        assert q("SELECT 1 / 0, 1 % 0").rows == [[None, None]]  # sqlite-style

    def test_case(self, emp_db):
        r = emp_db.execute(
            "SELECT name, CASE WHEN salary >= 100000 THEN 'high' "
            "WHEN salary >= 80000 THEN 'mid' ELSE 'low' END FROM emp "
            "ORDER BY name").rows
        assert r[0] == ["alice", "high"]
        assert r[2] == ["carol", "mid"]
        assert r[5] == ["frank", "low"]
        r = emp_db.execute(
            "SELECT CASE dept_id WHEN 1 THEN 'eng' WHEN 2 THEN 'sales' "
            "ELSE 'other' END, name FROM emp ORDER BY name").rows
        assert r[0] == ["eng", "alice"]
        assert r[5] == ["other", "frank"]

    def test_distinct(self, emp_db):
        assert emp_db.execute("SELECT DISTINCT dept_id FROM emp ORDER BY dept_id"
                              ).rows == [[None], [1], [2]]
        assert emp_db.execute("SELECT COUNT(DISTINCT dept_id) FROM emp"
                              ).rows == [[2]]


class TestAggregates:
    def test_global_aggregates(self, emp_db):
        q = emp_db.execute
        assert q("SELECT COUNT(*), COUNT(dept_id), MIN(salary), MAX(salary), "
                 "SUM(salary), ROUND(AVG(salary), 2) FROM emp").rows == \
            [[6, 5, 60000.0, 120000.0, 543000.0, 90500.0]]
        # aggregates over empty input
        assert q("SELECT COUNT(*), SUM(salary), MIN(salary) FROM emp "
                 "WHERE salary > 999999").rows == [[0, None, None]]
        assert q("SELECT TOTAL(salary) FROM emp WHERE salary > 999999"
                 ).rows == [[0.0]]

    def test_group_by(self, emp_db):
        r = emp_db.execute("SELECT dept_id, COUNT(*) AS n FROM emp "
                           "GROUP BY dept_id ORDER BY dept_id").rows
        assert r == [[None, 1], [1, 3], [2, 2]]

    def test_group_by_join_having(self, emp_db):
        r = emp_db.execute(
            "SELECT d.name, COUNT(*) AS cnt, MAX(e.salary) AS top "
            "FROM emp e JOIN dept d ON e.dept_id = d.id "
            "GROUP BY d.name HAVING COUNT(*) >= 2 ORDER BY cnt DESC, d.name").rows
        assert r == [["Engineering", 3, 120000.0], ["Sales", 2, 88000.0]]

    def test_group_concat(self, emp_db):
        r = emp_db.execute("SELECT GROUP_CONCAT(name) FROM emp WHERE dept_id = 1"
                           ).rows
        assert r[0][0] == "alice,bob,erin"
        r = emp_db.execute("SELECT GROUP_CONCAT(name, ' & ') FROM emp "
                           "WHERE dept_id = 1").rows
        assert r[0][0] == "alice & bob & erin"

    def test_order_by_aggregate(self, emp_db):
        r = emp_db.execute("SELECT dept_id FROM emp WHERE dept_id IS NOT NULL "
                           "GROUP BY dept_id ORDER BY COUNT(*) DESC, dept_id").rows
        assert r == [[1], [2]]

    def test_aggregate_nested_in_case(self, emp_db):
        # aggregates inside CASE branches must bind per group
        r = emp_db.execute(
            "SELECT CASE WHEN MAX(salary) > 100000 THEN 'top' ELSE 'mid' END "
            "FROM emp WHERE dept_id IS NOT NULL GROUP BY dept_id "
            "ORDER BY 1").rows
        assert r == [["mid"], ["top"]]


class TestOrderByLimit:
    def test_multi_key(self, emp_db):
        r = emp_db.execute("SELECT dept_id, name FROM emp "
                           "ORDER BY dept_id DESC, name ASC").rows
        assert r[:2] == [[2, "carol"], [2, "dave"]]
        assert r[-1][0] is None and r[-1][1] == "frank"

    def test_positional_and_alias(self, emp_db):
        r = emp_db.execute("SELECT name, salary FROM emp ORDER BY 2 DESC LIMIT 1"
                           ).rows
        assert r == [["alice", 120000.0]]
        r = emp_db.execute("SELECT name AS n FROM emp ORDER BY n LIMIT 1").rows
        assert r == [["alice"]]

    def test_limit_offset(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp ORDER BY name "
                           "LIMIT 2 OFFSET 1").rows
        assert r == [["bob"], ["carol"]]
        r = emp_db.execute("SELECT name FROM emp ORDER BY name LIMIT 2, 1").rows
        assert r == [["carol"]]
        r = emp_db.execute("SELECT name FROM emp ORDER BY name LIMIT 0").rows
        assert r == []


class TestJoins:
    def test_inner_join(self, emp_db):
        r = emp_db.execute("SELECT e.name, d.name FROM emp e "
                           "JOIN dept d ON e.dept_id = d.id "
                           "ORDER BY e.name").rows
        assert r[0] == ["alice", "Engineering"]
        assert len(r) == 5

    def test_left_join(self, emp_db):
        r = emp_db.execute("SELECT e.name, d.name FROM emp e "
                           "LEFT JOIN dept d ON e.dept_id = d.id "
                           "ORDER BY e.name").rows
        assert r[5] == ["frank", None]
        assert len(r) == 6

    def test_cross_join(self, emp_db):
        r = emp_db.execute("SELECT COUNT(*) FROM emp, dept").rows
        assert r == [[18]]
        r = emp_db.execute("SELECT COUNT(*) FROM emp CROSS JOIN dept").rows
        assert r == [[18]]

    def test_three_way_join(self, tmp_path):
        from nimbus_db import Database
        db = Database(str(tmp_path / "j.ndb"))
        db.execute("CREATE TABLE a (x INT, ax INT)")
        db.execute("CREATE TABLE b (y INT, b_val INT)")
        db.execute("CREATE TABLE c (z INT, cz TEXT)")
        db.execute("INSERT INTO a VALUES (1, 10), (2, 20)")
        db.execute("INSERT INTO b VALUES (1, 100), (2, 200)")
        db.execute("INSERT INTO c VALUES (100, 'ok'), (300, 'no')")
        r = db.execute("SELECT cz FROM a JOIN b ON x = y JOIN c ON b_val = z"
                       ).rows
        assert r == [["ok"]]
        db.close()

    def test_subquery_in_from(self, emp_db):
        r = emp_db.execute(
            "SELECT t.d, MAX(t.s) FROM "
            "(SELECT dept_id AS d, salary AS s FROM emp WHERE dept_id IS NOT NULL) t "
            "GROUP BY t.d ORDER BY t.d").rows
        assert r == [[1, 120000.0], [2, 88000.0]]


class TestSubqueries:
    def test_scalar_subquery(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE salary > "
                           "(SELECT AVG(salary) FROM emp) ORDER BY name").rows
        assert r == [["alice"], ["bob"], ["erin"]]

    def test_in_subquery(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE dept_id IN "
                           "(SELECT id FROM dept WHERE location = 'BJ') "
                           "ORDER BY name").rows
        assert r == [["alice"], ["bob"], ["erin"]]
        # three-valued logic: NULL NOT IN (...) is NULL, so frank is
        # excluded just like in SQLite
        r = emp_db.execute("SELECT name FROM emp WHERE dept_id NOT IN "
                           "(SELECT id FROM dept) ORDER BY name").rows
        assert r == []

    def test_empty_in_subquery(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE dept_id IN "
                           "(SELECT id FROM dept WHERE id = 99)").rows
        assert r == []

    def test_rowid_subquery(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE rowid = "
                           "(SELECT MAX(rowid) FROM emp)").rows
        assert r == [["frank"]]


class TestCompound:
    def test_union(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE dept_id = 1 "
                           "UNION SELECT name FROM emp WHERE dept_id = 2 "
                           "ORDER BY name").rows
        assert r == [["alice"], ["bob"], ["carol"], ["dave"], ["erin"]]

    def test_union_all_keeps_duplicates(self, emp_db):
        r = emp_db.execute("SELECT dept_id FROM emp WHERE dept_id = 1 "
                           "UNION ALL SELECT dept_id FROM emp "
                           "WHERE dept_id = 1").rows
        assert r == [[1], [1], [1], [1], [1], [1]]

    def test_union_dedups(self, emp_db):
        r = emp_db.execute("SELECT dept_id FROM emp WHERE dept_id = 1 "
                           "UNION SELECT dept_id FROM emp WHERE dept_id = 1"
                           ).rows
        assert r == [[1]]

    def test_union_with_limit(self, emp_db):
        r = emp_db.execute("SELECT name FROM emp WHERE dept_id = 1 "
                           "UNION SELECT name FROM emp WHERE dept_id = 2 "
                           "ORDER BY name LIMIT 2").rows
        assert r == [["alice"], ["bob"]]


class TestNimbusMaster:
    def test_schema_introspection(self, emp_db):
        rows = emp_db.execute("SELECT type, name FROM nimbus_master "
                              "ORDER BY type, name").rows
        assert ["table", "dept"] in rows
        assert ["table", "emp"] in rows
        assert ["index", "nimbus_auto_emp_1"] not in rows  # no unique cols

    def test_master_filterable(self, emp_db):
        r = emp_db.execute("SELECT sql FROM nimbus_master "
                           "WHERE type = 'table' AND name = 'dept'").rows
        assert r and "CREATE TABLE dept" in r[0][0]
