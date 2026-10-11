import os

import pytest

from nimbus_db import Database
from nimbus_db.errors import (ConstraintError, NimbusError,
                              OperationalError, SchemaError)


class TestAutocommit:
    def test_each_statement_is_atomic(self, tmp_path):
        path = str(tmp_path / "ac.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
        db.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
        # multi-row insert violates UNIQUE on 3rd row -> nothing persisted
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t (v) VALUES ('c'), ('d'), ('a')")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]
        db.close()

    def test_failed_statement_rolls_back(self, tmp_path):
        path = str(tmp_path / "fb.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (v INT NOT NULL)")
        with pytest.raises(NimbusError):
            db.execute("INSERT INTO t VALUES (1)")
            db.execute("INSERT INTO t VALUES (NULL)")
        # first insert was committed (autocommit per statement)
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[1]]
        db.close()


class TestExplicitTransactions:
    def test_commit_persists(self, tmp_path):
        path = str(tmp_path / "tc.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (v INT)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("COMMIT")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]
        db.close()

    def test_rollback_discards(self, tmp_path):
        path = str(tmp_path / "tr.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (v INT)")
        db.execute("INSERT INTO t VALUES (0)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("UPDATE t SET v = 99")
        db.execute("DELETE FROM t WHERE v = 0")
        db.execute("ROLLBACK")
        assert db.execute("SELECT * FROM t").rows == [[0]]
        db.close()

    def test_rollback_restores_schema(self, tmp_path):
        path = str(tmp_path / "ts.ndb")
        db = Database(path)
        db.execute("CREATE TABLE keep (v INT)")
        db.execute("BEGIN")
        db.execute("CREATE TABLE temp (v INT)")
        db.execute("DROP TABLE keep")
        db.execute("ROLLBACK")
        assert db.execute("SELECT COUNT(*) FROM keep").rows == [[0]]
        with pytest.raises(SchemaError):
            db.execute("SELECT * FROM temp")
        db.close()

    def test_error_inside_txn_keeps_it_open(self, tmp_path):
        path = str(tmp_path / "te.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (v INT UNIQUE)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2)")
        with pytest.raises(ConstraintError):
            db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (3)")
        db.execute("COMMIT")
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[3]]
        db.close()


class TestDurability:
    def test_persistence_across_reopen(self, tmp_path):
        path = str(tmp_path / "p.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n REAL)")
        db.execute("CREATE INDEX idx ON t(v)")
        db.execute("INSERT INTO t (v, n) VALUES ('x', 1.5), ('y', 2.5)")
        db.close()
        db = Database(path)
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]
        assert db.execute("SELECT n FROM t WHERE v = 'x'").rows == [[1.5]]
        assert db.execute("SELECT * FROM t WHERE v = 'y'").rows == [[2, "y", 2.5]]
        db.execute("INSERT INTO t (v, n) VALUES ('z', 3.5)")
        db.close()
        db = Database(path)
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[3]]
        db.close()

    def test_crash_recovery(self, tmp_path):
        path = str(tmp_path / "crash.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (v) VALUES ('committed')")
        db.close()

        # a second session that "crashes" without closing
        crashed = Database(path)
        crashed.execute("INSERT INTO t (v) VALUES ('from-crash')")
        del crashed

        db = Database(path)
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]
        assert db.execute("SELECT v FROM t WHERE rowid = 2"
                          ).rows == [["from-crash"]]
        db.close()

    def test_delete_and_vacuum_cycle(self, tmp_path):
        path = str(tmp_path / "vac.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (v INT)")
        for i in range(500):
            db.execute(f"INSERT INTO t VALUES ({i})")
        size_full = os.path.getsize(path)
        db.execute("DELETE FROM t")
        db.execute("INSERT INTO t VALUES (1)")  # should reuse freed pages
        assert os.path.getsize(path) <= size_full
        assert db.execute("SELECT COUNT(*) FROM t").rows == [[1]]
        db.close()


class TestLargeData:
    def test_big_text_roundtrip(self, tmp_path):
        path = str(tmp_path / "big.ndb")
        db = Database(path)
        db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
        payload = "ab" * 25000  # 50 KB -> spans many overflow pages
        db.execute(f"INSERT INTO docs (body) VALUES ('{payload}')")
        assert db.execute("SELECT LENGTH(body) FROM docs").rows == [[50000]]
        db.close()
        db = Database(path)
        assert db.execute("SELECT LENGTH(body) FROM docs").rows == [[50000]]
        db.close()

    def test_thousand_rows_index(self, tmp_path):
        path = str(tmp_path / "many.ndb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k INT, v TEXT)")
        db.execute("BEGIN")
        for i in range(1000):
            db.execute(f"INSERT INTO t (k, v) VALUES ({i % 37}, 'row{i}')")
        db.execute("COMMIT")
        db.execute("CREATE INDEX idx ON t(k)")
        assert db.execute("SELECT COUNT(*) FROM t WHERE k = 5").rows == \
            [[len([i for i in range(1000) if i % 37 == 5])]]
        r = db.execute("SELECT COUNT(*) FROM t WHERE k > 30").rows
        assert r[0][0] == len([i for i in range(1000) if i % 37 > 30])
        db.close()
