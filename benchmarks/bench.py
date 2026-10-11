"""Benchmark nimbus-db against Python's built-in sqlite3.

Run:  python benchmarks/bench.py
"""
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nimbus_db import Database  # noqa: E402
import sqlite3  # noqa: E402

N = 5000
LOOKUPS = 2000
rng = random.Random(42)


def bench_nimbus(tmp):
    db = Database(os.path.join(tmp, "nimbus.ndb"))
    t0 = time.perf_counter()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INT)")
    db.execute("BEGIN")
    for i in range(N):
        db.execute(f"INSERT INTO t (id, v, n) VALUES ({i}, 'row-{i}', "
                   f"{rng.randint(0, 100000)})")
    db.execute("COMMIT")
    t_insert = time.perf_counter() - t0

    t0 = time.perf_counter()
    db.execute("CREATE INDEX idx ON t(n)")
    t_index = time.perf_counter() - t0

    keys = [rng.randrange(N) for _ in range(LOOKUPS)]
    t0 = time.perf_counter()
    for k in keys:
        db.execute(f"SELECT v FROM t WHERE id = {k}")
    t_lookup = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k in keys:
        db.execute(f"SELECT v FROM t WHERE n = {k}")
    t_idx_scan = time.perf_counter() - t0

    t0 = time.perf_counter()
    db.execute("SELECT n, COUNT(*), AVG(n) FROM t GROUP BY n")
    t_agg = time.perf_counter() - t0

    t0 = time.perf_counter()
    db.execute("BEGIN")
    for i in range(0, N, 2):
        db.execute(f"DELETE FROM t WHERE id = {i}")
    db.execute("COMMIT")
    t_delete = time.perf_counter() - t0
    db.close()
    return dict(insert=t_insert, index=t_index, lookup=t_lookup / LOOKUPS,
                idx_scan=t_idx_scan / LOOKUPS, agg=t_agg, delete=t_delete)


def bench_sqlite(tmp):
    con = sqlite3.connect(os.path.join(tmp, "sq.db"))
    t0 = time.perf_counter()
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INT)")
    for i in range(N):
        con.execute("INSERT INTO t (id, v, n) VALUES (?, ?, ?)",
                    (i, f"row-{i}", rng.randint(0, 100000)))
    con.commit()
    t_insert = time.perf_counter() - t0

    t0 = time.perf_counter()
    con.execute("CREATE INDEX idx ON t(n)")
    t_index = time.perf_counter() - t0

    keys = [rng.randrange(N) for _ in range(LOOKUPS)]
    t0 = time.perf_counter()
    for k in keys:
        con.execute("SELECT v FROM t WHERE id = ?", (k,)).fetchall()
    t_lookup = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k in keys:
        con.execute("SELECT v FROM t WHERE n = ?", (k,)).fetchall()
    t_idx_scan = time.perf_counter() - t0

    t0 = time.perf_counter()
    con.execute("SELECT n, COUNT(*), AVG(n) FROM t GROUP BY n").fetchall()
    t_agg = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i in range(0, N, 2):
        con.execute("DELETE FROM t WHERE id = ?", (i,))
    con.commit()
    t_delete = time.perf_counter() - t0
    con.close()
    return dict(insert=t_insert, index=t_index, lookup=t_lookup / LOOKUPS,
                idx_scan=t_idx_scan / LOOKUPS, agg=t_agg, delete=t_delete)


def main():
    tmp = tempfile.mkdtemp()
    print(f"rows={N}, point lookups={LOOKUPS}\n")
    nimbus = bench_nimbus(tmp)
    sqlite = bench_sqlite(tmp)

    rows = [("INSERT (one txn)", "insert", N, "rows"),
            ("CREATE INDEX", "index", N, "rows"),
            ("point lookup (rowid)", "lookup", 1, "op"),
            ("indexed point lookup", "idx_scan", 1, "op"),
            ("GROUP BY aggregate", "agg", 1, "query"),
            ("DELETE half", "delete", N // 2, "rows")]
    print(f"{'operation':<24}{'nimbus-db':>12}{'sqlite3':>12}{'ratio':>9}")
    print("-" * 57)
    for label, key, unit_n, unit in rows:
        a, b = nimbus[key] * 1000, sqlite[key] * 1000   # -> milliseconds
        ratio = a / b if b else float("inf")
        per = f" ({a * 1000 / unit_n:.0f}us/{unit})" if unit_n > 1 else ""
        print(f"{label:<24}{a:>9.1f}ms{b:>9.2f}ms{ratio:>8.1f}x{per}")


if __name__ == "__main__":
    main()
