"""60-second tour of nimbus-db's engine features.

Run:  python demo/tour.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from nimbus_db import Database

W = 64
MAX_ECHO = 100


def head(title: str) -> None:
    print()
    print("=" * W)
    print(f"  {title}")
    print("=" * W)


def run(db, sql: str, note: str = "") -> None:
    shown = sql if len(sql) <= MAX_ECHO else sql[:MAX_ECHO] + " ...'"
    print(f"  nimbus> {shown}")
    if note:
        print(f"          -- {note}")
    res = db.execute(sql)
    if res.rows:
        print("          " + " | ".join(res.columns))
        for row in res.rows[:6]:
            cells = " | ".join("NULL" if v is None else
                               (f"{v:.2f}" if isinstance(v, float) else str(v))
                               for v in row)
            print("          " + cells)
        if len(res.rows) > 6:
            print(f"          ... ({len(res.rows)} rows)")
    elif res.message:
        print(f"          {res.message}")


def main() -> int:
    db = Database(os.path.join(tempfile.mkdtemp(), "tour.ndb"))

    head("1 - schema with constraints")
    run(db, "CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT UNIQUE)",
        "UNIQUE gets an auto-managed B+tree index")
    run(db, "CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " salary REAL, dept_id INT)")

    head("2 - 2 000 rows in one transaction (B+tree splits)")
    import random
    rng = random.Random(7)
    names = ["ada", "bob", "cyd", "dee", "eli", "fay", "gus", "hal"]
    db.execute("BEGIN")
    for i in range(1, 2001):
        db.execute(f"INSERT INTO emp (name, salary, dept_id) VALUES "
                   f"('{rng.choice(names)}{i}', {40000 + rng.randrange(80000)}, "
                   f"{1 + rng.randrange(3)})")
    db.execute("COMMIT")
    print(f"          inserted 2000 rows; file is now "
          f"{db.pager.num_pages} pages (~{db.pager.num_pages * 4} KB)")

    head("3 - the planner picks an index (and EXPLAIN proves it)")
    run(db, "CREATE INDEX idx_salary ON emp (salary)")
    run(db, "EXPLAIN SELECT name FROM emp WHERE salary = 95000",
        "equality -> index seek instead of a full scan")
    run(db, "SELECT COUNT(*) FROM emp WHERE salary = 95000")
    run(db, "EXPLAIN SELECT * FROM emp WHERE id = 42",
        "rowid predicate -> table B-tree point lookup")

    head("4 - joins, groups, nested aggregates")
    db.execute("INSERT INTO dept (name) VALUES ('eng'), ('ops'), ('biz')")
    run(db, "SELECT d.name, COUNT(*) AS staff, ROUND(AVG(e.salary), 0) AS avg_sal"
            " FROM emp e JOIN dept d ON e.dept_id = d.id"
            " GROUP BY d.name ORDER BY avg_sal DESC")
    run(db, "SELECT CASE WHEN AVG(salary) > 80000 THEN 'top half' ELSE 'rest' END"
            " FROM emp", "aggregate nested inside CASE")
    run(db, "SELECT name FROM emp WHERE salary >"
            " (SELECT AVG(salary) FROM emp) LIMIT 3",
        "uncorrelated scalar subquery")

    head("5 - transactions: DDL rolls back too")
    run(db, "BEGIN")
    run(db, "CREATE TABLE oops (x INT)")
    run(db, "DROP TABLE dept")
    run(db, "ROLLBACK", "everything above is undone - schema included")
    run(db, "SELECT COUNT(*) FROM dept")

    head("6 - big payloads spill to overflow pages")
    run(db, "CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
    run(db, "INSERT INTO docs (body) VALUES ('" + "x" * 30000 + "')",
        "60 KB of text across chained overflow pages")
    run(db, "SELECT LENGTH(body) FROM docs")

    head("7 - the schema is data (query it like a table)")
    run(db, "SELECT type, name FROM nimbus_master ORDER BY type, name")

    db.close()
    print()
    print("=" * W)
    print("  tour complete. try the crash demo next:")
    print("      python demo/crash_demo.py")
    print("=" * W)
    return 0


if __name__ == "__main__":
    sys.exit(main())
