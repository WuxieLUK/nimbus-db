"""Crash-recovery demo: kill a writer mid-transaction, lose nothing committed.

A child process opens the database, commits 1 000 rows, starts a new
transaction, writes 500 more rows and *announces* it is inside the
uncommitted transaction.  The parent then hard-kills the child (no close, no
flush - the OS never runs cleanup), reopens the file and reports what
survived.  Expectation, verified below:

    * the 1 000 committed rows are intact  (WAL replay)
    * the 500 in-flight rows are gone      (never fsynced)

Run:  python demo/crash_demo.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CHILD_SCRIPT = r'''
import os, sys, time
sys.path.insert(0, {root!r})
from nimbus_db import Database

db = Database({db_path!r})
db.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, item TEXT)")
db.execute("BEGIN")
for i in range(1, 1001):
    db.execute(f"INSERT INTO orders (item) VALUES ('item-{{i:04d}}')")
db.execute("COMMIT")
print("committed=1000", flush=True)

db.execute("BEGIN")
for i in range(1001, 1501):
    db.execute(f"INSERT INTO orders (item) VALUES ('item-{{i:04d}}')")
print("uncommitted=500 now kill me", flush=True)
time.sleep(30)          # the parent kills the process here
'''

MARKS = {"=": "=" * 62}


def line(text: str = "", ch: str = " ") -> None:
    print(text if text else ch * 62)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="nimbus_crash_")
    db_path = os.path.join(tmp, "shop.ndb")
    child = os.path.join(tmp, "writer.py")
    with open(child, "w", encoding="utf-8") as f:
        f.write(CHILD_SCRIPT.format(root=ROOT, db_path=db_path))

    line(ch="=")
    print("  nimbus-db crash-recovery demo")
    line(ch="=")
    print(f"  database : {db_path}")
    print(f"  writer   : separate process, will be SIGKILLed mid-transaction")
    line()

    proc = subprocess.Popen([sys.executable, child],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**os.environ, "PYTHONPATH": ROOT})
    killed = False
    for out in proc.stdout:
        msg = out.strip()
        print(f"  writer> {msg}")
        if "uncommitted" in msg:
            proc.kill()               # hard crash: no close(), no cleanup
            killed = True
            break
    proc.wait()
    print(f"  writer  process killed (returncode {proc.returncode})")
    line()
    if not killed:
        print("ERROR: writer never reached the uncommitted phase")
        return 1

    from nimbus_db import Database   # parent reopens like a fresh process
    db = Database(db_path)
    total = db.execute("SELECT COUNT(*) FROM orders").rows[0][0]
    committed = db.execute(
        "SELECT COUNT(*) FROM orders WHERE id <= 1000").rows[0][0]
    in_flight = db.execute(
        "SELECT COUNT(*) FROM orders WHERE id > 1000").rows[0][0]
    sample = db.execute(
        "SELECT id, item FROM orders WHERE id IN (1, 500, 1000)").rows

    print("  reopening the database after the crash ...")
    line(ch="-")
    print(f"  committed rows survived : {committed} (expected 1000)")
    print(f"  in-flight rows lost     : {in_flight}   (expected 0)")
    print(f"  total rows              : {total}")
    line(ch="-")
    for rid, item in sample:
        print(f"    spot check id={rid:<4} -> {item!r}")
    db.close()

    ok = committed == 1000 and in_flight == 0
    line()
    print("  RESULT: " + ("DURABLE - committed data survived the crash, "
                          "the torn transaction was dropped."
                          if ok else "UNEXPECTED - durability broken."))
    line(ch="=")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
