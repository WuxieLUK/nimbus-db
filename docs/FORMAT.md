# The nimbus-db file format

Version 1. All integers are **big-endian**. A database is two files:

```
demo.ndb        page store (the database)
demo.ndb-wal    write-ahead log (may be absent / empty)
```

`P` below is the page size (4096 by default; a power of two, 512–65536).

---

## 1. The page store

Pages are numbered from 0; page *n* occupies bytes `[n·P, (n+1)·P)`.

### 1.1 Header — page 0

| offset | size | field | notes |
|---|---|---|---|
| 0 | 16 | magic | `NIMBUS-DB v1\0\0\0\0` |
| 16 | u32 | `page_size` | `P` |
| 20 | u32 | `num_pages` | current file length in pages |
| 24 | u32 | `schema_root` | page number of the schema B-tree root (0 = empty db) |
| 28 | u32 | `freelist_head` | first free page, 0 = none |
| 32 | u32 | `freelist_count` | number of free pages |

The rest of page 0 is zero padding.

### 1.2 B-tree pages

Every non-header, non-freelist page belongs to a B-tree (table, index, or
schema — the schema table is an ordinary table tree):

| offset | size | field | notes |
|---|---|---|---|
| 0 | u8 | page type | `0x0D` table leaf · `0x05` table interior · `0x0A` index leaf · `0x02` index interior |
| 1 | u16 | `num_cells` | |
| 3 | u16 | `content_offset` | first byte of cell content (grows down from page end) |
| 5 | u32 | `rightmost_child` | interior only: last subtree |
| 9 | u32 | `next_leaf` | leaf only: sibling page for range scans, 0 = end |
| 13 | 3 | *(padding to 16)* | |
| 16 | u16 × n | cell pointers | in **key order**; point at cell content |

**Routing invariant** (interior): cell `(K → C)` holds keys `< K`; the
rightmost pointer holds keys `≥` the last cell's key. Splits copy the right
half's first key up; all entries stay in the leaves, so a leaf-chain scan sees
every key exactly once.

### 1.3 Cells

Cell content grows down from the end of the page; cells are referenced only
through the pointer array.

**Table leaf** — `rowid`, then record payload:

```
zigzag-varint  rowid
uvarint        payload_len
payload[0..local]          (local prefix; see §1.4)
u32 overflow_head          (only if payload spilled)
```

**Table interior**:

```
u32            left child page
zigzag-varint  separator rowid  (first key of the NEXT subtree)
```

**Index leaf / index interior** — index trees key on the record
`(col₁ .. colₙ, rowid)`; interior cells carry a copy of the separator:

```
[index interior only] u32  left child page
uvarint        payload_len
payload        record encoding of the key tuple
[u32 overflow_head if spilled]
```

### 1.4 Overflow pages

Payloads larger than the local threshold spill:

```
min_local = ((P - 12) · 32) // 255 - 23
max_local = P - 35
K = min_local + (payload_len - min_local) % (P - 4)
local = K if K ≤ max_local else min_local
```

Bytes beyond `local` go to a chain of overflow pages — each starts with a
`u32` next-pointer (0 = last) followed by up to `P - 4` payload bytes.

### 1.5 Freelist pages

A free page's first `u32` is the next free page (0 = end). Allocating prefers
the freelist before growing the file, so deletes recycle pages.

### 1.6 The record format

A record is a sequence of values, each a 1-byte tag + body:

| tag | meaning | body |
|---|---|---|
| 0 | NULL | — |
| 1 | INTEGER | zig-zag LEB128 (64-bit domain) |
| 2 | REAL | IEEE-754 float64 |
| 3 | TEXT | uvarint byte-length + UTF-8 |
| 4 | BLOB | uvarint byte-length + raw bytes |

Two varint kinds, used deliberately: **zig-zag** for signed integers, plain
**unsigned LEB128** for lengths.

An INTEGER PRIMARY KEY column is the rowid itself: it is stored as NULL in the
record and materialized from the cell key on read.

### 1.7 The schema table

Rooted at `schema_root`, one record per object:

```
[type TEXT, name TEXT, tbl_name TEXT, rootpage INT, sql TEXT, auto INT, unique INT]
```

`type` is `table` or `index`. Tables re-parse their stored `CREATE TABLE` SQL
on open. Because the schema is ordinary data, DDL participates in
transactions. User queries see it through the `nimbus_master` pseudo-table.

---

## 2. The write-ahead log

A sequence of frames, one per modified page per commit:

```
u32  page_no
u32  commit_flag   0, or the post-commit num_pages on the LAST frame of a commit
u32  checksum      FNV-1a-32 over the page image
[P]  page image
```

* **Commit**: dirty pages are appended, the WAL is flushed and **fsynced**
  before the commit returns. Readers see committed frames through an in-memory
  overlay until the next checkpoint.
* **Checkpoint** (≥ 2000 frames, or on close): frames are written back to the
  page store, which is fsynced, then the WAL is truncated.
* **Recovery** (on open): frames are scanned and checksum-verified; pages are
  replayed only up to the **last valid commit frame**. A torn or corrupt frame
  ends the replay — a crash mid-commit loses exactly that transaction.

---

## 3. A real example

`CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t (v) VALUES ('ada');`
— actual bytes from the resulting file:

```
page 0 (header)                        page 1 (table leaf)
00000000  4e 49 4d 42 55 53 2d 44      00001000  0d 00 01 0f f8 ...
          42 20 76 31 00 00 00 00      00001010  0f f8 ...   <- cell ptr → 0x0ff8
00000010  00 00 10 00  00 00 00 03     00001ff8  02 06 00 03 03 61 64 61
          00 00 00 02  00 00 00 00
```

Reading page 0: magic ok, `P = 0x1000` (4096), 3 pages, schema root = page 2,
empty freelist.

Reading page 1: type `0x0D` (table leaf), 1 cell, content at `0x0ff8`.
The cell decodes as:

```
02            zigzag varint -> rowid 1
06            payload length 6
  00            tag 0: NULL       (id — the rowid alias)
  03            tag 3: TEXT
  03 61 64 61   len 3, "ada"
```

Page 2 (not shown fully) is the schema leaf holding the row
`('table', 't', 't', 1, 'CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)', 0, 0)`.
