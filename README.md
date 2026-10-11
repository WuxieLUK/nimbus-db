# nimbus-db

**A SQL database engine built from scratch in pure Python — zero dependencies.**

```
lexer → parser → planner → executor
                      ↓
                  catalog (SQL-stored schema)
                      ↓
              B+tree tables & indexes
                      ↓
        page store + write-ahead log + crash recovery
```

nimbus-db is a single-file-database engine in the spirit of SQLite, written to
understand — and demonstrate — every layer of a real OLTP database:

| Layer | What it does |
|---|---|
| `lexer.py` | hand-written SQL tokenizer (strings, blobs `x'AB'`, comments, positions) |
| `parser.py` | recursive-descent parser → typed AST, operator precedence per the SQL standard |
| `planner.py` | name resolution against FROM scopes, expression compilation to closures, three-valued logic |
| `executor.py` | index selection, hash & nested-loop joins, grouping, sorting, DML/DDL writers |
| `catalog.py` | schema stored *in the database itself* (a `nimbus_master` pseudo-table, like `sqlite_master`) |
| `btree.py` | B+trees for tables (rowid-keyed) and indexes (composite-keyed), with page splits, borrow/merge rebalancing, and overflow pages for large payloads |
| `pager.py` | 4 KB page store, freelist, transactions, **write-ahead log with checksums and crash recovery** |

No third-party packages. No `sqlite3`. Python ≥ 3.10.

---

## Quick start

```bash
pip install -e .            # or just add nimbus-db/ to your path
python -m nimbus_db app.ndb # sqlite3-style shell
```

```python
from nimbus_db import Database

db = Database("app.ndb")
db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE)")

db.execute("INSERT INTO users (name, email) VALUES ('ada', 'ada@lovelace.dev')")

for row in db.execute("SELECT id, name FROM users WHERE name LIKE 'a%'").rows:
    print(row)

db.execute("BEGIN")
db.execute("UPDATE users SET name = 'Ada' WHERE id = 1")
db.execute("ROLLBACK")

db.close()
```

## SQL coverage

```sql
-- DDL
CREATE TABLE orders (
    id      INTEGER PRIMARY KEY,
    item    TEXT NOT NULL,
    qty     INT DEFAULT 1,
    price   REAL,
    UNIQUE (item, qty)
);
CREATE [UNIQUE] INDEX idx_price ON orders (price);
DROP TABLE / DROP INDEX (with IF EXISTS)

-- DML
INSERT INTO orders (item, qty, price) VALUES ('widget', 2, 9.99), ('gizmo', 1, 3.5);
UPDATE orders SET qty = qty + 1 WHERE price < 5;
DELETE FROM orders WHERE id = 3;

-- queries
SELECT o.item, SUM(o.qty * o.price) AS total, CASE WHEN o.qty > 1 THEN 'multi' ELSE 'single' END
FROM orders o
LEFT JOIN users u ON o.buyer = u.id          -- also INNER / CROSS, hash or nested loop
WHERE o.price BETWEEN 1 AND 10 AND o.item LIKE 'w%'
   OR o.item IN (SELECT name FROM users)     -- uncorrelated subqueries
GROUP BY o.item
HAVING SUM(o.qty) > 1
ORDER BY total DESC, o.item
LIMIT 10 OFFSET 5;

SELECT item FROM orders WHERE qty = 1 UNION SELECT item FROM orders WHERE price > 8;

-- functions: ABS ROUND UPPER LOWER LENGTH SUBSTR COALESCE IFNULL NULLIF TYPEOF HEX
-- aggregates: COUNT SUM TOTAL AVG MIN MAX GROUP_CONCAT (with DISTINCT)

EXPLAIN SELECT * FROM orders WHERE price = 9.99;   -- shows the chosen index
SELECT * FROM nimbus_master;                       -- the schema table
```

**Indexes actually work**: the planner picks an index from equality prefixes and
range bounds, falls back to the rowid B-tree for `rowid` predicates, and
`EXPLAIN` tells you what it chose.

**Durability actually works**: every commit is fsynced to a WAL; on reopen the
WAL is replayed up to the last checksum-valid commit frame. Kill the process
mid-transaction and you lose exactly that transaction — nothing else.

## Transactions

* **Autocommit** — each statement is its own transaction; a failed statement
  rolls back completely (including schema changes).
* **`BEGIN` / `COMMIT` / `ROLLBACK`** — multi-statement transactions; the
  catalog snapshots in-memory state so DDL rolls back too.
* Single writer per database file (the engine takes a process-local lock).

## The shell

```
$ python -m nimbus_db demo.ndb
nimbus-db v1.0.0 -- enter '.help' for help
nimbus> CREATE TABLE t (a INT PRIMARY KEY, b TEXT);
nimbus> INSERT INTO t (b) VALUES ('hello'), ('world');
nimbus> SELECT * FROM t WHERE b LIKE 'w%';
+---+-------+
| a | b     |
+---+-------+
| 2 | world |
+---+-------+
rows: 1
nimbus> .tables
t
nimbus> EXPLAIN SELECT * FROM t WHERE a = 2;
SEARCH t USING ROWID (rowid=?)
```

Dot commands: `.tables .schema [t] .indexes [t] .databases .stats .help .quit`

## Performance

`python benchmarks/bench.py` — 5 000 rows, Python 3.13, vs the C-implemented
`sqlite3` stdlib module:

```
operation                  nimbus-db     sqlite3    ratio
---------------------------------------------------------
point lookup (rowid)          0.2ms     0.02ms     7.1x
indexed point lookup          0.2ms     0.03ms     6.6x
GROUP BY aggregate           80.5ms     3.65ms    22.1x
INSERT (one txn)           2927.0ms    13.11ms   223.3x
DELETE half                1976.3ms     7.33ms   269.5x
CREATE INDEX               4668.3ms     3.35ms  1393.9x
```

Read paths land within **~7× of C sqlite** — the B-tree search itself is doing
its job. Writes pay for a deliberate simplicity: every mutation re-encodes the
whole affected page (correctness first). See *Design trade-offs* below.

## Design trade-offs (the honest list)

* **Page-rewrite writes.** Insert/delete rebuild the affected B-tree page from
  parsed entries instead of patching bytes in place. Simple, obviously correct,
  slower.
* **Materialized row sources.** Scans read pages into Python lists before
  filtering/joining; no streaming iterators below the executor.
* **Single process, single writer.** No page locking or MVCC; concurrent
  readers in separate processes see only checkpointed data.
* **Strict typing.** Numbers never silently become text (unlike SQLite's
  loose affinity rules) — only lossless int↔float conversions are applied.
* **Not implemented**: RIGHT/FULL joins, correlated subqueries, CHECK / FOREIGN
  KEY constraints, views, triggers, INTERSECT/EXCEPT. Each raises a clear
  `UnsupportedError` naming what's missing.

## Testing

```bash
python -m pytest tests/ -q     # 137 tests
```

The suite covers the parser (precedence, escapes, malformed SQL), value
semantics (three-valued logic, LIKE, affinity), B+trees (5 000-key shuffled
inserts with incremental verification, delete rebalancing, overflow chains,
freelist reuse), the WAL (torn-write recovery, checkpoints), full SQL surface,
and crash-durability scenarios — including killing a session mid-write and
reopening.

## Project layout

```
nimbus_db/
    lexer.py  parser.py  ast_nodes.py     SQL front-end
    planner.py  executor.py               compilation & execution
    catalog.py                            schema (stored in-db)
    btree.py  pager.py  values.py         storage engine
    db.py  cli.py  __main__.py            facade & shell
tests/        137 pytest tests
benchmarks/   bench.py  (vs sqlite3)
examples/     quickstart.py
```

## Acknowledgements

Standards of the genre that informed this project:

* [db_tutorial — "Let's Build a Simple Database"](https://cstack.github.io/db_tutorial/) (cstack) — the C-language SQLite clone that started it all
* [toydb](https://github.com/erikgrinaker/toydb) (Erik Grinaker) — Rust distributed SQL database, the gold standard for "build a DB to learn" write-ups
* [limbo](https://github.com/tursodatabase/limbo) (Turso) — a complete SQLite rewrite in Rust
* [Build Your Own Database from Scratch in Go](https://build-your-own.org/) — bottom-up storage engine walkthroughs
* The [SQLite documentation](https://sqlite.org/) — file format, WAL design, and datatype affinity rules

## License

MIT
