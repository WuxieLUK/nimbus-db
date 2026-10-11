import pytest

from nimbus_db.errors import (ConstraintError, OperationalError,
                              SchemaError, TypeMismatchError)


class TestDDL:
    def test_create_and_list(self, db):
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        rows = db.execute("SELECT name, type FROM nimbus_master "
                          "ORDER BY name").rows
        assert ["t", "table"] in rows

    def test_duplicate_table(self, db):
        db.execute("CREATE TABLE t (a INT)")
        with pytest.raises(SchemaError):
            db.execute("CREATE TABLE t (b INT)")

    def test_if_not_exists(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS t (a INT)")
        db.execute("CREATE TABLE IF NOT EXISTS t (a INT)")  # no-op

    def test_drop_table(self, db):
        db.execute("CREATE TABLE t (a INT)")
        db.execute("INSERT INTO t VALUES (1), (2), (3)")
        db.execute("DROP TABLE t")
        with pytest.raises(SchemaError):
            db.execute("SELECT * FROM t")
        # pages were recycled: creating again must work
        db.execute("CREATE TABLE t (x INT)")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[0]]

    def test_drop_if_exists(self, db):
        db.execute("DROP TABLE IF EXISTS ghost")

    def test_unknown_column_in_ddl(self, db):
        with pytest.raises(SchemaError):
            db.execute("CREATE TABLE t (a INT, PRIMARY KEY (nope))")


class TestInsert:
    def test_auto_rowid(self, db):
        db.execute("CREATE TABLE t (a TEXT)")
        db.execute("INSERT INTO t VALUES ('x')")
        db.execute("INSERT INTO t VALUES ('y')")
        assert db.execute("SELECT rowid, a FROM t").rows == [[1, "x"], [2, "y"]]

    def test_explicit_rowid_and_conflict(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (10, 'x')")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (10, 'y')")

    def test_explicit_pk_alias_negatives(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (id, v) VALUES (-5, 'neg')")
        assert db.execute("SELECT v FROM t WHERE id = -5").rows == [["neg"]]
        # auto rowid continues above the max (sqlite semantics: even negative)
        db.execute("INSERT INTO t (v) VALUES ('auto')")
        assert db.execute("SELECT id FROM t ORDER BY id").rows == [[-5], [-4]]

    def test_multi_row_count_mismatch(self, db):
        db.execute("CREATE TABLE t (a INT, b INT)")
        with pytest.raises(OperationalError):
            db.execute("INSERT INTO t (a, b) VALUES (1)")

    def test_defaults(self, db):
        db.execute("CREATE TABLE t (a INT, b TEXT DEFAULT 'hi', c INT DEFAULT 7)")
        db.execute("INSERT INTO t (a) VALUES (1)")
        assert db.execute("SELECT b, c FROM t").rows == [["hi", 7]]

    def test_type_affinity_enforced(self, db):
        db.execute("CREATE TABLE t (a INT, s TEXT)")
        with pytest.raises(TypeMismatchError):
            db.execute("INSERT INTO t (a) VALUES ('nope')")
        with pytest.raises(TypeMismatchError):
            db.execute("INSERT INTO t (s) VALUES (123)")
        # lossless float -> int is fine
        db.execute("INSERT INTO t (a) VALUES (3.0)")
        assert db.execute("SELECT a FROM t").rows == [[3]]

    def test_expression_values(self, db):
        db.execute("CREATE TABLE t (a INT, b INT)")
        db.execute("INSERT INTO t (a, b) VALUES (1 + 2, 3 * 4)")
        assert db.execute("SELECT * FROM t").rows == [[3, 12]]


class TestUpdateDelete:
    def test_update(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t (v) VALUES (1), (2), (3)")
        r = db.execute("UPDATE t SET v = v * 10 WHERE id > 1")
        assert r.rows_affected == 2
        assert db.execute("SELECT v FROM t ORDER BY id").rows == [[1], [20], [30]]

    def test_update_all(self, db):
        db.execute("CREATE TABLE t (v INT)")
        db.execute("INSERT INTO t VALUES (1), (2)")
        db.execute("UPDATE t SET v = 99")
        assert db.execute("SELECT COUNT(*) FROM t WHERE v = 99").rows == [[2]]

    def test_delete_all(self, db):
        db.execute("CREATE TABLE t (v INT)")
        db.execute("INSERT INTO t VALUES (1), (2), (3)")
        r = db.execute("DELETE FROM t")
        assert r.rows_affected == 3
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[0]]
        # sqlite semantics: an empty table restarts rowids at 1
        db.execute("INSERT INTO t VALUES (9)")
        assert db.execute("SELECT rowid FROM t").rows == [[1]]

    def test_delete_where(self, db):
        db.execute("CREATE TABLE t (v INT)")
        db.execute("INSERT INTO t VALUES (1), (2), (3)")
        db.execute("DELETE FROM t WHERE v >= 2")
        assert db.execute("SELECT v FROM t").rows == [[1]]


class TestConstraints:
    def test_unique_column(self, db):
        db.execute("CREATE TABLE t (a TEXT UNIQUE)")
        db.execute("INSERT INTO t VALUES ('x')")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES ('x')")
        db.execute("INSERT INTO t VALUES ('y')")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]

    def test_unique_composite(self, db):
        db.execute("CREATE TABLE t (a INT, b INT, UNIQUE (a, b))")
        db.execute("INSERT INTO t VALUES (1, 2)")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (1, 2)")
        db.execute("INSERT INTO t VALUES (1, 3)")  # different combo ok

    def test_composite_pk(self, db):
        db.execute("CREATE TABLE t (a INT, b TEXT, PRIMARY KEY (a, b))")
        db.execute("INSERT INTO t VALUES (1, 'x')")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (1, 'x')")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[1]]

    def test_not_null(self, db):
        db.execute("CREATE TABLE t (a INT NOT NULL)")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (NULL)")
        db.execute("INSERT INTO t VALUES (0)")

    def test_pk_null_auto_assigns(self, db):
        # sqlite semantics: NULL in an INTEGER PRIMARY KEY auto-assigns
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t (id) VALUES (NULL)")
        assert db.execute("SELECT id FROM t").rows == [[1]]

    def test_update_enforces_unique(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, email TEXT UNIQUE)")
        db.execute("INSERT INTO t (email) VALUES ('a@x'), ('b@x')")
        with pytest.raises(ConstraintError):
            db.execute("UPDATE t SET email = 'a@x' WHERE email = 'b@x'")

    def test_auto_index_cannot_drop(self, db):
        db.execute("CREATE TABLE t (a TEXT UNIQUE)")
        name = db.execute("SELECT name FROM nimbus_master WHERE type='index'"
                          ).rows[0][0]
        with pytest.raises(SchemaError):
            db.execute(f"DROP INDEX {name}")


class TestIndexes:
    def test_create_and_use(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t (v) VALUES (1), (2), (3), (2)")
        db.execute("CREATE INDEX idx ON t(v)")
        plan = db.execute("EXPLAIN SELECT * FROM t WHERE v = 2")
        assert "USING INDEX idx" in plan.rows[0][0]
        assert db.execute("SELECT id FROM t WHERE v = 2 ORDER BY id"
                          ).rows == [[2], [4]]

    def test_unique_index(self, db):
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("INSERT INTO t VALUES ('a')")
        db.execute("CREATE UNIQUE INDEX u ON t(v)")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES ('a')")

    def test_create_index_duplicate_fails(self, db):
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("INSERT INTO t VALUES ('a'), ('a')")
        with pytest.raises(ConstraintError):
            db.execute("CREATE UNIQUE INDEX u ON t(v)")

    def test_index_maintained_on_write(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t (v) VALUES (5)")
        db.execute("CREATE INDEX idx ON t(v)")
        db.execute("INSERT INTO t (v) VALUES (7)")
        db.execute("UPDATE t SET v = 9 WHERE v = 7")
        db.execute("DELETE FROM t WHERE v = 9")
        assert db.execute("SELECT * FROM t WHERE v = 9").rows == []
        assert db.execute("SELECT * FROM t WHERE v = 5").rows == [[1, 5]]

    def test_drop_index(self, db):
        db.execute("CREATE TABLE t (v INT)")
        db.execute("CREATE INDEX idx ON t(v)")
        db.execute("DROP INDEX idx")
        plan = db.execute("EXPLAIN SELECT * FROM t WHERE v = 1")
        assert "SCAN t" in plan.rows[0][0]

    def test_range_query_via_index(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INT)")
        for i in range(200):
            db.execute(f"INSERT INTO t (v) VALUES ({i % 50})")
        db.execute("CREATE INDEX idx ON t(v)")
        assert db.execute(
            "SELECT COUNT(*) FROM t WHERE v >= 10 AND v < 20").rows == [[40]]
        assert db.execute(
            "SELECT COUNT(*) FROM t WHERE v > 45").rows == [[16]]
