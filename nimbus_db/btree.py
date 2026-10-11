"""B+tree engine over pager pages.

Two tree kinds:
* ``table``  -- keyed by rowid; leaves hold ``(rowid, record)``.
* ``index``  -- keyed by a record tuple ``(col1, ..., colN, rowid)``;
  interior nodes repeat separator keys (B+-tree with keys in leaves).

Page layout (B-tree pages)::

    u8  page_type            (TABLE_LEAF / TABLE_INTERIOR / INDEX_LEAF / INDEX_INTERIOR)
    u16 num_cells
    u16 content_offset       (cells grow down from the end of the page)
    u32 rightmost_child      (interior pages only)
    u32 next_leaf            (leaf pages: sibling chain for range scans)
    -- pad to 16 bytes --
    u16 cell_pointers[num_cells]     (grows up, kept in key order)
    ... free space ...
    cell content             (grows down from the end)

Large payloads spill to overflow pages (``u32 next | data...``).
Ownership rule: every helper that *rewrites* a page from parsed entries
(``_write_leaf`` / ``_write_interior``) takes ownership of the entries'
old overflow chains -- it frees them and re-encodes.  Code that moves an
entry between pages carries its chain head inside the entry tuple so the
next rewrite frees it exactly once.  Chains of entries that move *up*
(out of the page, e.g. a split median) are freed by the splitter and the
entry travels as bare ``(key, payload)``.

Deletion rebalances: a node under ``MIN_CELLS`` borrows a boundary cell
from a sibling (parent separator updated) or merges with one (parent
separator descends into the merged node); the root shrinks when empty.
"""
from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple

from .errors import OperationalError
from .pager import (PAGE_INDEX_INTERIOR, PAGE_INDEX_LEAF, PAGE_TABLE_INTERIOR,
                    PAGE_TABLE_LEAF, Pager)
from .values import INT64_MIN, deserialize_row, serialize_row, sql_compare

_HDR = struct.Struct(">BHHII")
HEADER_SIZE = 16
MIN_CELLS = 2


def _wv(out: bytearray, n: int) -> None:
    """Unsigned LEB128 varint."""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _rv(buf: bytes, off: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, off


def _zigzag(out: bytearray, n: int) -> None:
    _wv(out, ((n << 1) ^ (n >> 63)) & ((1 << 64) - 1))


def _unzigzag(buf: bytes, off: int) -> Tuple[int, int]:
    u, off = _rv(buf, off)
    return (u >> 1) ^ -(u & 1), off


def _unpack_header(page: bytes) -> Tuple[int, int, int, int, int]:
    ptype, ncells, coff, right, nxt = _HDR.unpack_from(page, 0)
    return ptype, ncells, coff, right, nxt


def _pack_header(page: bytearray, ptype: int, ncells: int, coff: int,
                 right: int = 0, nxt: int = 0) -> None:
    _HDR.pack_into(page, 0, ptype, ncells, coff, right, nxt)


def _cell_ptr(page: bytes, idx: int) -> int:
    return struct.unpack_from(">H", page, HEADER_SIZE + 2 * idx)[0]


def _set_cell_ptr(page: bytearray, idx: int, off: int) -> None:
    struct.pack_into(">H", page, HEADER_SIZE + 2 * idx, off)


def _is_leaf(ptype: int) -> bool:
    return ptype in (PAGE_TABLE_LEAF, PAGE_INDEX_LEAF)


def cmp_index_key(a: Tuple, b: Tuple) -> int:
    for x, y in zip(a, b):
        c = sql_compare(x, y)
        if c:
            return c
    return (len(a) > len(b)) - (len(a) < len(b))


def _len_varint(n: int) -> int:
    out = 1
    n >>= 7
    while n:
        out += 1
        n >>= 7
    return out


# entry formats
#   leaf    (table):  (rowid, payload, overflow_head)
#   leaf    (index):  (key_tuple, payload, overflow_head)
#   interior(table):  (rowid_key, left_child, None, 0)
#   interior(index):  (key_tuple, left_child, payload, overflow_head)


class BTree:
    def __init__(self, pager: Pager, root: int, kind: str) -> None:
        assert kind in ("table", "index")
        self.pager = pager
        self.root = root
        self.kind = kind
        self.page_size = pager.page_size
        self.max_local = self.page_size - 35
        self.min_local = ((self.page_size - 12) * 32) // 255 - 23

    # ------------------------------------------------------------------ #
    # payload encoding with overflow spill
    # ------------------------------------------------------------------ #

    def _encode_payload(self, out: bytearray, payload: bytes) -> None:
        n = len(payload)
        _wv(out, n)
        if n <= self.max_local:
            out += payload
            return
        k = self.min_local + (n - self.min_local) % (self.page_size - 4)
        local = k if k <= self.max_local else self.min_local
        out += payload[:local]
        rest = payload[local:]
        chunks = []
        while rest:
            chunks.append(rest[:self.page_size - 4])
            rest = rest[self.page_size - 4:]
        heads = [self.pager.alloc_page() for _ in chunks]
        for i, chunk in enumerate(chunks):
            page = bytearray(self.page_size)
            nxt = heads[i + 1] if i + 1 < len(heads) else 0
            struct.pack_into(">I", page, 0, nxt)
            page[4:4 + len(chunk)] = chunk
            self.pager.write_page(heads[i], page)
        out += struct.pack(">I", heads[0])

    def _decode_payload(self, page: bytes, plen_off: int) -> bytes:
        """Decode from the position of the *length varint*."""
        n, off = _rv(page, plen_off)
        if n <= self.max_local:
            return bytes(page[off:off + n])
        k = self.min_local + (n - self.min_local) % (self.page_size - 4)
        local = k if k <= self.max_local else self.min_local
        data = bytearray(page[off:off + local])
        head = struct.unpack_from(">I", page, off + local)[0]
        remaining = n - local
        cur = head
        while remaining > 0 and cur:
            opage = self.pager.read_page(cur)
            cur = struct.unpack_from(">I", opage, 0)[0]
            chunk = opage[4:4 + min(remaining, self.page_size - 4)]
            data += chunk
            remaining -= len(chunk)
        if remaining > 0:
            raise OperationalError("corrupt overflow chain")
        return bytes(data)

    def _free_overflow(self, head: int) -> None:
        seen_guard = 0
        while head:
            page = self.pager.read_page(head)
            nxt = struct.unpack_from(">I", page, 0)[0]
            self.pager.free_page(head)
            head = nxt
            seen_guard += 1
            if seen_guard > 1_000_000:
                raise OperationalError("overflow chain loop (corrupt db)")

    # ------------------------------------------------------------------ #
    # page <-> entries
    # ------------------------------------------------------------------ #

    def _payload_head(self, page: bytes, n_pay: int, len_off: int) -> int:
        """Overflow chain head for a cell whose length varint starts at
        *len_off*; 0 when the payload is local."""
        if n_pay <= self.max_local:
            return 0
        k = self.min_local + (n_pay - self.min_local) % (self.page_size - 4)
        local = k if k <= self.max_local else self.min_local
        return struct.unpack_from(">I", page,
                                  len_off + _len_varint(n_pay) + local)[0]

    def _read_leaf_entries(self, page: bytes) -> List[Tuple]:
        ptype, n, _coff, _r, _nxt = _unpack_header(page)
        entries = []
        for i in range(n):
            off = _cell_ptr(page, i)
            if ptype == PAGE_TABLE_LEAF:
                rowid, off = _unzigzag(page, off)
                n_pay, _pstart = _rv(page, off)
                payload = self._decode_payload(page, off)
                head = self._payload_head(page, n_pay, off)
                entries.append((rowid, payload, head))
            else:
                n_pay, _pstart = _rv(page, off)
                payload = self._decode_payload(page, off)
                head = self._payload_head(page, n_pay, off)
                entries.append((tuple(deserialize_row(payload)), payload, head))
        return entries

    def _read_interior(self, page: bytes) -> Tuple[List[Tuple], int]:
        ptype, n, _coff, rightmost, _nxt = _unpack_header(page)
        entries = []
        for i in range(n):
            off = _cell_ptr(page, i)
            child = struct.unpack_from(">I", page, off)[0]
            off += 4
            if ptype == PAGE_TABLE_INTERIOR:
                rowid, _ = _unzigzag(page, off)
                entries.append((rowid, child, None, 0))
            else:
                n_pay, _pstart = _rv(page, off)
                payload = self._decode_payload(page, off)
                head = self._payload_head(page, n_pay, off)
                entries.append((tuple(deserialize_row(payload)), child,
                                payload, head))
        return entries, rightmost

    def _layout_leaf(self, entries: List[Tuple], next_leaf: int) -> bytearray:
        ptype = PAGE_TABLE_LEAF if self.kind == "table" else PAGE_INDEX_LEAF
        cells: List[bytes] = []
        for key, payload, head in entries:
            if head:
                self._free_overflow(head)
            out = bytearray()
            if self.kind == "table":
                _zigzag(out, key)
            self._encode_payload(out, payload)
            cells.append(bytes(out))
        return self._emit_page(ptype, cells, rightmost=0, next_leaf=next_leaf)

    def _layout_interior(self, entries: List[Tuple],
                         rightmost: int) -> bytearray:
        ptype = (PAGE_TABLE_INTERIOR if self.kind == "table"
                 else PAGE_INDEX_INTERIOR)
        cells: List[bytes] = []
        for key, child, payload, head in entries:
            if head:
                self._free_overflow(head)
            out = bytearray()
            out += struct.pack(">I", child)
            if self.kind == "table":
                _zigzag(out, key)
            else:
                self._encode_payload(out, payload or b"")
            cells.append(bytes(out))
        return self._emit_page(ptype, cells, rightmost=rightmost, next_leaf=0)

    def _emit_page(self, ptype: int, cells: List[bytes], rightmost: int,
                   next_leaf: int) -> bytearray:
        total = HEADER_SIZE + 2 * len(cells) + sum(len(c) for c in cells)
        if total > self.page_size:
            raise OperationalError("page rebuild does not fit (internal)")
        page = bytearray(self.page_size)
        coff = self.page_size - sum(len(c) for c in cells)
        _pack_header(page, ptype, len(cells), coff, rightmost, next_leaf)
        write_off = self.page_size
        for i, cell in enumerate(cells):
            write_off -= len(cell)
            page[write_off:write_off + len(cell)] = cell
            _set_cell_ptr(page, i, write_off)
        return page

    def _write_leaf(self, page_no: int, entries: List[Tuple],
                    next_leaf: int) -> None:
        self.pager.write_page(page_no, self._layout_leaf(entries, next_leaf))

    def _write_interior(self, page_no: int, entries: List[Tuple],
                        rightmost: int) -> None:
        self.pager.write_page(page_no,
                              self._layout_interior(entries, rightmost))

    # ------------------------------------------------------------------ #
    # size estimation (pure, no allocation)
    # ------------------------------------------------------------------ #

    def _payload_encoded_size(self, n: int) -> int:
        size = _len_varint(n) + min(n, self.max_local)
        if n > self.max_local:
            size += 4
        return size

    def _leaf_cell_size(self, key, payload: bytes) -> int:
        if self.kind == "table":
            tmp = bytearray()
            _zigzag(tmp, key)
            return len(tmp) + self._payload_encoded_size(len(payload))
        return self._payload_encoded_size(len(payload))

    def _interior_cell_size(self, entry: Tuple) -> int:
        key, _child, payload, _head = entry
        if self.kind == "table":
            tmp = bytearray()
            _zigzag(tmp, key)
            return 4 + len(tmp)
        return 4 + self._payload_encoded_size(len(payload or b""))

    # ------------------------------------------------------------------ #
    # navigation
    # ------------------------------------------------------------------ #

    def _cell_key(self, page: bytes, idx: int):
        off = _cell_ptr(page, idx)
        ptype = page[0]
        if ptype == PAGE_TABLE_LEAF:
            return _unzigzag(page, off)[0]
        if ptype == PAGE_TABLE_INTERIOR:
            return _unzigzag(page, off + 4)[0]
        if ptype == PAGE_INDEX_LEAF:
            return tuple(deserialize_row(self._decode_payload(page, off)))
        return tuple(deserialize_row(self._decode_payload(page, off + 4)))

    def _cmp(self, a, b) -> int:
        if self.kind == "table":
            return (a > b) - (a < b)
        return cmp_index_key(a, b)

    def _lower_bound_leaf(self, key) -> Tuple[int, int]:
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            ptype, n, _c, right, _nx = _unpack_header(page)
            if _is_leaf(ptype):
                lo, hi = 0, n
                while lo < hi:
                    mid = (lo + hi) // 2
                    if self._cmp(self._cell_key(page, mid), key) < 0:
                        lo = mid + 1
                    else:
                        hi = mid
                return page_no, lo
            page_no = self._child_for(page, key)

    def _child_for(self, page: bytes, key) -> int:
        """Convention: cell (K_i, C_i) -- subtree(C_i) holds keys
        [K_{i-1}, K_i); K_i is the minimum key of the NEXT subtree."""
        ptype, n, _c, rightmost, _nx = _unpack_header(page)
        for i in range(n):
            if self._cmp(self._cell_key(page, i), key) > 0:
                return struct.unpack_from(">I", page, _cell_ptr(page, i))[0]
        return rightmost

    def _first_leaf(self) -> int:
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            ptype, n, _c, right, _nx = _unpack_header(page)
            if _is_leaf(ptype):
                return page_no
            page_no = (struct.unpack_from(">I", page, _cell_ptr(page, 0))[0]
                       if n > 0 else right)

    def _last_leaf(self) -> int:
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            ptype, _n, _c, right, _nx = _unpack_header(page)
            if _is_leaf(ptype):
                return page_no
            page_no = right

    def max_rowid(self) -> int:
        leaf = self._last_leaf()
        page = self.pager.read_page(leaf)
        n = _unpack_header(page)[1]
        if n == 0:
            return 0
        return _unzigzag(page, _cell_ptr(page, n - 1))[0]

    # ------------------------------------------------------------------ #
    # point operations
    # ------------------------------------------------------------------ #

    def lookup(self, rowid: int) -> Optional[bytes]:
        if self.kind != "table":
            raise OperationalError("lookup is table-only")
        page_no, idx = self._lower_bound_leaf(rowid)
        page = self.pager.read_page(page_no)
        n = _unpack_header(page)[1]
        if idx < n and _unzigzag(page, _cell_ptr(page, idx))[0] == rowid:
            off = _cell_ptr(page, idx) + _zigzag_len(rowid)
            n_pay, pstart = _rv(page, off)
            return self._decode_payload(page, off)
        return None

    def lookup_index(self, key: Tuple) -> bool:
        page_no, idx = self._lower_bound_leaf(key)
        page = self.pager.read_page(page_no)
        n = _unpack_header(page)[1]
        if idx >= n:
            return False
        off = _cell_ptr(page, idx)
        n_pay, pstart = _rv(page, off)
        found = tuple(deserialize_row(self._decode_payload(page, off)))
        return cmp_index_key(found, key) == 0

    def insert(self, key, payload: bytes) -> None:
        """Insert; key must not exist.  Table: key=rowid, payload=record.
        Index: key=full tuple, payload ignored (derived from key)."""
        if self.kind == "index":
            payload = serialize_row(list(key))
        split = self._insert_recursive(self.root, key, payload)
        if split is None:
            return
        sep_key, sep_payload, right_no = split
        old_root = self.root
        new_root = self.pager.alloc_page()
        entry = ((sep_key, old_root, sep_payload, 0) if self.kind == "index"
                 else (sep_key, old_root, None, 0))
        self._write_interior(new_root, [entry], right_no)
        self.root = new_root

    def _insert_recursive(self, page_no: int, key, payload: bytes):
        page = self.pager.read_page(page_no)
        ptype, n, _c, rightmost, nxt = _unpack_header(page)
        if _is_leaf(ptype):
            entries = self._read_leaf_entries(page)
            lo, hi = 0, len(entries)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._cmp(entries[mid][0], key) < 0:
                    lo = mid + 1
                else:
                    hi = mid
            if lo < len(entries) and self._cmp(entries[lo][0], key) == 0:
                raise OperationalError("duplicate key in B-tree insert")
            entries.insert(lo, (key, payload, 0))
            used = HEADER_SIZE + 2 * len(entries) + sum(
                self._leaf_cell_size(k, p) for k, p, _h in entries)
            if used <= self.page_size:
                self._write_leaf(page_no, entries, nxt)
                return None
            return self._split_leaf(page_no, entries, nxt)

        entries, rightmost = self._read_interior(page)
        child_slot = n
        for i in range(n):
            if self._cmp(self._cell_key(page, i), key) > 0:
                child_slot = i
                break
        child = entries[child_slot][1] if child_slot < n else rightmost
        split = self._insert_recursive(child, key, payload)
        if split is None:
            return None
        sep_key, sep_payload, new_right = split
        if child_slot < n:
            # the child that split is cell child_slot's LEFT page: it keeps
            # its position under the new separator, and the cell's old key
            # now routes to the new right page
            old_key, old_child, old_payload, old_head = entries[child_slot]
            entries[child_slot] = (old_key, new_right, old_payload, old_head)
            entries.insert(child_slot, (sep_key, old_child, sep_payload, 0))
        else:
            # the rightmost child split: append (sep -> old rightmost)
            # and promote the new page to rightmost
            entries.append((sep_key, rightmost, sep_payload, 0))
            rightmost = new_right
        used = HEADER_SIZE + 2 * len(entries) + sum(
            self._interior_cell_size(e) for e in entries)
        if used <= self.page_size:
            self._write_interior(page_no, entries, rightmost)
            return None
        return self._split_interior(page_no, entries, rightmost)

    def _split_leaf(self, page_no: int, entries: List[Tuple],
                    next_leaf: int):
        """Split a full leaf; the separator is COPIED up (all entries stay
        in the leaves) so leaf-chain scans see the complete key set."""
        n = len(entries)
        right_no = self.pager.alloc_page()
        m = n // 2
        left, right = entries[:m], entries[m:]
        self._write_leaf(right_no, right, next_leaf)
        self._write_leaf(page_no, left, right_no)
        sep_payload = right[0][1] if self.kind == "index" else None
        return right[0][0], sep_payload, right_no

    def _split_interior(self, page_no: int, entries: List[Tuple],
                        rightmost: int):
        n = len(entries)
        m = n // 2
        median = entries[m]
        left, right = entries[:m], entries[m + 1:]
        self._write_interior(page_no, left, median[1])
        right_no = self.pager.alloc_page()
        self._write_interior(right_no, right, rightmost)
        if median[3]:
            self._free_overflow(median[3])
        return median[0], median[2], right_no

    # ------------------------------------------------------------------ #
    # deletion with rebalance
    # ------------------------------------------------------------------ #

    def delete(self, key) -> bool:
        path: List[Tuple[int, int]] = []     # (interior_page_no, child_slot)
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            ptype, n, _c, right, _nx = _unpack_header(page)
            if _is_leaf(ptype):
                break
            slot = n
            for i in range(n):
                if self._cmp(self._cell_key(page, i), key) > 0:
                    slot = i
                    break
            child = (struct.unpack_from(">I", page, _cell_ptr(page, slot))[0]
                     if slot < n else right)
            path.append((page_no, slot))
            page_no = child

        page = self.pager.read_page(page_no)
        n = _unpack_header(page)[1]
        nxt = _unpack_header(page)[4]
        entries = self._read_leaf_entries(page)
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cmp(entries[mid][0], key) < 0:
                lo = mid + 1
            else:
                hi = mid
        if lo >= len(entries) or self._cmp(entries[lo][0], key) != 0:
            return False
        if entries[lo][2]:
            self._free_overflow(entries[lo][2])
        del entries[lo]
        self._write_leaf(page_no, entries, nxt)
        if path:
            self._rebalance_up(path, len(path) - 1, page_no)
        return True

    def _rebalance_up(self, path: List[Tuple[int, int]], depth: int,
                      child_page: int) -> None:
        if depth < 0:
            return
        parent_no, slot = path[depth]
        cn = _unpack_header(self.pager.read_page(child_page))[1]
        if cn >= MIN_CELLS:
            return

        pentries, pright = self._read_interior(self.pager.read_page(parent_no))
        children = [e[1] for e in pentries] + [pright]
        left_sib = children[slot - 1] if slot > 0 else None
        right_sib = children[slot + 1] if slot + 1 < len(children) else None

        def cells_of(pg: int) -> int:
            return _unpack_header(self.pager.read_page(pg))[1]

        if right_sib is not None and cells_of(right_sib) > MIN_CELLS:
            self._borrow(parent_no, slot, child_page, right_sib, from_right=True)
            return
        if left_sib is not None and cells_of(left_sib) > MIN_CELLS:
            self._borrow(parent_no, slot, child_page, left_sib, from_right=False)
            return
        self._merge_child(parent_no, slot, child_page, left_sib, right_sib)
        if depth > 0:
            self._rebalance_up(path, depth - 1, parent_no)
        else:
            self._maybe_shrink_root(parent_no)

    def _maybe_shrink_root(self, root_no: int) -> None:
        page = self.pager.read_page(root_no)
        ptype, n, _c, rightmost, _nx = _unpack_header(page)
        if not _is_leaf(ptype) and n == 0:
            self.pager.free_page(root_no)
            self.root = rightmost

    def _borrow(self, parent_no: int, slot: int, child_page: int,
                sib_page: int, from_right: bool) -> None:
        pentries, pright = self._read_interior(self.pager.read_page(parent_no))
        child = self.pager.read_page(child_page)
        sib = self.pager.read_page(sib_page)
        is_leaf = _is_leaf(child[0])

        if is_leaf:
            centries = self._read_leaf_entries(child)
            sentries = self._read_leaf_entries(sib)
            if from_right:
                moved = sentries.pop(0)
                centries.append(moved)
                new_sep = sentries[0][0]
                new_payload = sentries[0][1]
                sep_idx = slot
            else:
                moved = sentries.pop()
                centries.insert(0, moved)
                new_sep = centries[0][0]
                new_payload = centries[0][1]
                sep_idx = slot - 1
            self._write_leaf(sib_page, sentries, _unpack_header(sib)[4])
            self._write_leaf(child_page, centries, _unpack_header(child)[4])
            self._replace_sep(pentries, sep_idx, new_sep,
                              payload=new_payload)
            self._write_interior(parent_no, pentries, pright)
            return

        centries, cright = self._read_interior(child)
        sentries, sright = self._read_interior(sib)
        if from_right:
            # S's first cell (L, k) rotates: C gains last cell (R_C, sep)
            # with sep = parent separator; C.rightmost = L; parent sep = k.
            moved = sentries.pop(0)
            sep_cell = pentries[slot]           # (sep, child, payload, head)
            centries.append((sep_cell[0], cright, sep_cell[2], sep_cell[3]))
            cright_new = moved[1]
            new_sep = moved[0]
            self._write_interior(sib_page, sentries, sright)
            self._write_interior(child_page, centries, cright_new)
            self._replace_sep(pentries, slot, new_sep, payload=moved[2],
                              head=moved[3])
            self._write_interior(parent_no, pentries, pright)
        else:
            # S's last cell (L, k) rotates: C gains first cell (R_S, sep);
            # S.rightmost = L; parent sep = k.
            moved = sentries.pop()
            sep_cell = pentries[slot - 1]       # separates left sib | child
            centries.insert(0, (sep_cell[0], sright, sep_cell[2], sep_cell[3]))
            sright_new = moved[1]
            new_sep = moved[0]
            self._write_interior(sib_page, sentries, sright_new)
            self._write_interior(child_page, centries, cright)
            self._replace_sep(pentries, slot - 1, new_sep, payload=moved[2],
                              head=moved[3])
            self._write_interior(parent_no, pentries, pright)

    def _replace_sep(self, pentries: List[Tuple], cell_idx: int, new_sep,
                     payload=None, head: int = 0) -> None:
        if 0 <= cell_idx < len(pentries):
            child = pentries[cell_idx][1]
            pentries[cell_idx] = (new_sep, child, payload, head)

    def _merge_child(self, parent_no: int, slot: int, child_page: int,
                     left_sib: Optional[int], right_sib: Optional[int]) -> None:
        pentries, pright = self._read_interior(self.pager.read_page(parent_no))
        child = self.pager.read_page(child_page)
        is_leaf = _is_leaf(child[0])

        # children[slot] is the underflowed page; its slot index in the
        # parent's child list (cells 0..n-1 plus rightmost at n)
        if left_sib is not None:
            keep, gone = left_sib, child_page
            keep_idx, gone_idx = slot - 1, slot
        else:
            keep, gone = child_page, right_sib
            keep_idx, gone_idx = slot, slot + 1

        keep_img = self.pager.read_page(keep)
        gone_img = self.pager.read_page(gone)

        if is_leaf:
            kentries = self._read_leaf_entries(keep_img)
            gentries = self._read_leaf_entries(gone_img)
            # two <= half-full leaves fit in one page
            total = HEADER_SIZE + 2 * (len(kentries) + len(gentries)) + sum(
                self._leaf_cell_size(k, p) for k, p, _h in kentries + gentries)
            if total > self.page_size:
                raise OperationalError("leaf merge overflow (corrupt tree)")
            kentries.extend(gentries)
            self._write_leaf(keep, kentries, _unpack_header(gone_img)[4])
        else:
            kentries, kright = self._read_interior(keep_img)
            gentries, gright = self._read_interior(gone_img)
            # the separator between keep|gone descends into the merged node
            sep = pentries[keep_idx]
            kentries.append((sep[0], kright, sep[2], sep[3]))
            kentries.extend(gentries)
            self._write_interior(keep, kentries, gright)

        self.pager.free_page(gone)

        # parent update: the cell that referenced `gone` now points at the
        # merged page; the cell that referenced `keep` (whose key was the
        # separator) disappears
        if gone_idx < len(pentries):
            g = pentries[gone_idx]
            pentries[gone_idx] = (g[0], keep, g[2], g[3])
            del pentries[keep_idx]
        else:
            del pentries[keep_idx]
            pright = keep
        self._write_interior(parent_no, pentries, pright)

    # ------------------------------------------------------------------ #
    # scans
    # ------------------------------------------------------------------ #

    @classmethod
    def create_root(cls, pager: Pager, kind: str) -> "BTree":
        tree = cls(pager, 0, kind)
        root = pager.alloc_page()
        if kind == "table":
            pager.write_page(root, tree._layout_leaf([], 0))
        else:
            pager.write_page(root, tree._layout_leaf([], 0))
        tree.root = root
        return tree

    def iter_table_range(self, lo: int, hi: Optional[int],
                         hi_incl: bool = True,
                         lo_incl: bool = True) -> Iterator[Tuple[int, bytes]]:
        if self.kind != "table":
            raise OperationalError("iter_table_range is table-only")
        page_no, idx = self._lower_bound_leaf(lo)
        while page_no:
            page = self.pager.read_page(page_no)
            n, nxt = _unpack_header(page)[1], _unpack_header(page)[4]
            while idx < n:
                off = _cell_ptr(page, idx)
                rowid, off = _unzigzag(page, off)
                if not lo_incl and rowid == lo:
                    idx += 1
                    continue
                if hi is not None:
                    c = (rowid > hi) - (rowid < hi)
                    if c > 0 or (c == 0 and not hi_incl):
                        return
                n_pay, pstart = _rv(page, off)
                yield rowid, self._decode_payload(page, off)
                idx += 1
            page_no, idx = nxt, 0

    def iter_index_from(self, lo: Optional[Tuple],
                        stop_fn: Callable[[Tuple], bool]) -> Iterator[Tuple]:
        """Scan index entries from lower bound *lo* until stop_fn(key)."""
        if self.kind != "index":
            raise OperationalError("iter_index_from is index-only")
        start_key = lo if lo is not None else (INT64_MIN,)
        page_no, idx = self._lower_bound_leaf(start_key)
        while page_no:
            page = self.pager.read_page(page_no)
            n, nxt = _unpack_header(page)[1], _unpack_header(page)[4]
            while idx < n:
                off = _cell_ptr(page, idx)
                n_pay, pstart = _rv(page, off)
                key = tuple(deserialize_row(self._decode_payload(page, off)))
                if stop_fn(key):
                    return
                yield key
                idx += 1
            page_no, idx = nxt, 0

    def seek_prefix(self, values: Tuple) -> Optional[Tuple]:
        """First full index entry whose first len(values) columns equal
        *values*, or None."""
        page_no, idx = self._lower_bound_leaf(tuple(values))
        page = self.pager.read_page(page_no)
        n = _unpack_header(page)[1]
        if idx >= n:
            return None
        off = _cell_ptr(page, idx)
        n_pay, pstart = _rv(page, off)
        key = tuple(deserialize_row(self._decode_payload(page, off)))
        if len(key) < len(values):
            return None
        for a, b in zip(key, values):
            if sql_compare(a, b) != 0:
                return None
        return key

    def iter_table(self) -> Iterator[Tuple[int, bytes]]:
        if self.kind != "table":
            raise OperationalError("iter_table is table-only")
        leaf = self._first_leaf()
        while leaf:
            page = self.pager.read_page(leaf)
            ptype, n, _c, _r, nxt = _unpack_header(page)
            for i in range(n):
                off = _cell_ptr(page, i)
                rowid, off = _unzigzag(page, off)
                n_pay, pstart = _rv(page, off)
                yield rowid, self._decode_payload(page, off)
            leaf = nxt

    def iter_index_range(self, lo: Optional[Tuple], hi: Optional[Tuple],
                         hi_incl: bool = True) -> Iterator[Tuple]:
        if self.kind != "index":
            raise OperationalError("iter_index_range is index-only")
        start_key = lo if lo is not None else (INT64_MIN,)
        page_no, idx = self._lower_bound_leaf(start_key)
        while page_no:
            page = self.pager.read_page(page_no)
            n, nxt = _unpack_header(page)[1], _unpack_header(page)[4]
            while idx < n:
                off = _cell_ptr(page, idx)
                n_pay, pstart = _rv(page, off)
                key = tuple(deserialize_row(self._decode_payload(page, off)))
                if hi is not None:
                    c = cmp_index_key(key, hi)
                    if c > 0 or (c == 0 and not hi_incl):
                        return
                yield key
                idx += 1
            page_no, idx = nxt, 0

    def iter_index_all(self) -> Iterator[Tuple]:
        return self.iter_index_range(None, None)

    # ------------------------------------------------------------------ #
    # structural utilities
    # ------------------------------------------------------------------ #

    def walk_pages(self) -> List[int]:
        seen: List[int] = []
        stack = [self.root]

        def walk_chain(head: int) -> None:
            cur = head
            while cur and cur not in seen:
                seen.append(cur)
                opage = self.pager.read_page(cur)
                cur = struct.unpack_from(">I", opage, 0)[0]

        while stack:
            page_no = stack.pop()
            if page_no in seen or page_no == 0:
                continue
            seen.append(page_no)
            page = self.pager.read_page(page_no)
            ptype, n, _c, right, _nx = _unpack_header(page)
            if _is_leaf(ptype):
                for _k, _p, head in self._read_leaf_entries(page):
                    walk_chain(head)
            else:
                entries, rightmost = self._read_interior(page)
                for _k, child, _p, head in entries:
                    stack.append(child)
                    walk_chain(head)
                stack.append(rightmost)
        return seen

    def free_all(self) -> None:
        for page_no in self.walk_pages():
            self.pager.free_page(page_no)

    def count(self) -> int:
        it = self.iter_table() if self.kind == "table" else self.iter_index_all()
        return sum(1 for _ in it)


def _zigzag_len(n: int) -> int:
    out = bytearray()
    _zigzag(out, n)
    return len(out)
