# Architecture

```
            SQL text
               │
        ┌──────▼──────┐
        │   lexer.py  │  tokens + positions
        └──────┬──────┘
        ┌──────▼──────┐
        │  parser.py  │  recursive descent → AST
        └──────┬──────┘
        ┌──────▼──────┐   reads schema
        │  catalog.py │◄─────────────┐
        └──────┬──────┘              │
        ┌──────▼──────┐              │ root pages
        │ planner.py  │ name resolve │
        └──────┬──────┘ + compile    │
        ┌──────▼──────┐              │
        │ executor.py │──────────────┤
        │  (plans,    │              │
        │   scans,    │      ┌───────▼──────┐
        │   joins,    │      │   btree.py   │ tables & indexes
        │   DML/DDL)  │      │ split/merge/ │
        └──────┬──────┘      │   overflow   │
               │             └───────┬──────┘
               │             ┌───────▼──────┐
               └────────────►│   pager.py   │ pages + WAL + freelist
                             └───────┬──────┘
                                     ▼
                        file.ndb  +  file.ndb-wal
```

## 1. Front end (`lexer.py`, `parser.py`, `ast_nodes.py`)

A single-pass tokenizer produces IDENT / INT / FLOAT / STRING (text or blob) /
OP tokens with 1-based line:column positions — every parse error says where it
happened. `''` and `""` escape themselves; `--` comments are skipped.

The parser is recursive descent with one method per precedence level
(`OR < AND < NOT < equality/IN/LIKE/BETWEEN < comparison < additive <
multiplicative < concat < unary`), mirroring the SQL standard. Output is typed
dataclasses (`Select`, `Insert`, `CreateTable`, ...). Deliberately-unsupported
SQL (RIGHT JOIN, FOREIGN KEY, views...) raises `UnsupportedError` with the
reason rather than a misleading syntax error.

## 2. Values (`values.py`)

A value is a plain Python object: `None` (NULL), `int`/`float`, `str`, `bytes`.
Records serialize as `[type-tag + varint payload]` sequences; integers use
zig-zag LEB128 so the whole value space fits 64-bit varints. The module also
implements SQL comparison semantics — cross-type ordering NULL < numeric <
text < blob — three-valued truthiness, ASCII-case-insensitive `LIKE`, and
SQLite's type-affinity rules (with stricter, lossless-only coercion).

## 3. Storage (`pager.py`)

The database is one file of 4 KB pages. **Page 0 is the header**:

```
magic(16) | page_size u32 | num_pages u32 | schema_root u32
         | freelist_head u32 | freelist_count u32
```

Freed pages chain through their first 4 bytes into a freelist that
`alloc_page()` consumes before growing the file — deletes genuinely shrink the
footprint on reuse.

### Write-ahead log

`file.ndb-wal` is a sequence of frames: `page_no u32 | commit_flag u32 |
checksum u32 | page image`. Commits append dirty pages, fsync, then update an
in-memory overlay that `read_page()` consults before touching the file. At
2000 frames (or on close) a **checkpoint** writes frames back and truncates.

**Recovery**: on open, frames are validated by checksum; the log is replayed
only up to the last frame carrying a commit flag. A torn write mid-commit
loses that transaction and nothing else — the test suite kills sessions and
reopens to verify.

## 4. B+trees (`btree.py`)

One page layout serves both tree kinds:

```
u8 type | u16 num_cells | u16 content_offset
u32 rightmost_child (interior) | u32 next_leaf (leaf)
u16 cell_pointers[num_cells] → cell content grows down from page end
```

* **Table trees** key on rowid; leaves hold `(rowid, record)`.
* **Index trees** key on `(col1..colN, rowid)` tuples — the rowid suffix makes
  every index entry unique and lets UNIQUE checks and index-maintained updates
  address exact entries.

Interior-cell convention: cell `(K, child)` routes keys **strictly below K**
into `child`; the rightmost pointer takes the remainder. Splits copy the right
half's first key up (leaves keep every entry — leaf-chain scans stay
complete). Deletes rebalance at `MIN_CELLS = 2`: borrow a boundary cell from a
sibling (rotating the parent separator) or merge with one (the separator
descends into the merged node); the root collapses when it empties.

Payloads larger than the page spill to **overflow pages** (`u32 next | data`),
chain-freeing on rewrite, with a loop guard against corruption.

## 5. Catalog (`catalog.py`)

Schema lives in an ordinary table B-tree rooted in the header
(`sqlite_master` style), one row per object:
`[type, name, tbl_name, rootpage, sql, auto, unique]`. Tables re-parse their
stored `CREATE TABLE` on load. Users can query `nimbus_master` directly.
Because DDL is just data, **schema changes participate in transactions** —
`Catalog.snapshot()/restore()` mirrors in-memory state to the pager's
rollback.

An `INTEGER PRIMARY KEY` column aliases the rowid (stored as the record's
cell key, materialized on read); other PK/UNIQUE sets get auto-managed unique
indexes (`nimbus_auto_<table>_<n>`).

## 6. Planning & execution (`planner.py`, `executor.py`)

Expressions compile once per query into closures over a *scope* — the list of
`(alias, schema)` slots built from the FROM clause. A compiled expression
evaluates per combined row `[[slot0 values], [slot1 values], ...]`.

**Index selection** scans top-level WHERE conjuncts for column = const /
range patterns, scores candidate indexes by (equality-prefix length, bounded
columns), and also recognizes rowid predicates that use the table tree
directly. Selected predicates pre-narrow the scan; the full WHERE still runs
as a filter afterwards (index choice never changes results).

Joins: single-equality `a.x = b.y` joins build a hash table on the right
side; anything else falls back to nested loops with the ON predicate. LEFT
JOIN emits null-extended rows for unmatched left rows.

Grouping hashes on `group_key()` values (so `1`, `1.0` and `True` group
together) preserving first-seen order; aggregate results bind into
`CAgg` placeholders so `ROUND(AVG(x), 0)` works. ORDER BY sorts with the SQL
total order, resolving names against output aliases, positions, or the input
scope.

## 7. Transactions (`db.py`)

Autocommit wraps every statement in BEGIN…COMMIT; failures roll back pages
*and* catalog state. Explicit `BEGIN` spans statements; errors inside keep
the transaction open for a client ROLLBACK. The WAL fsync runs once per
commit, so batched inserts amortize durability.
