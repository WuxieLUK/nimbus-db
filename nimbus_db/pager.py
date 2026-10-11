"""Page-based storage with a write-ahead log.

File layout
-----------
``<name>.ndb`` -- page 0 is the database header; pages 1..N-1 hold B-tree
nodes and overflow pages::

    header (page 0):  magic(16) | page_size u32 | num_pages u32
                      | schema_root u32 | freelist_head u32 | freelist_count u32

``<name>.ndb-wal`` -- a sequence of frames; one frame per modified page
per commit::

    frame: page_no u32 | commit u32 (non-zero on the last frame of a
           commit: new num_pages) | checksum u32 | page image (page_size bytes)

Durability model
----------------
Transactions append dirty pages to the WAL and fsync it before the
commit returns.  Readers see committed frames through an in-memory
overlay map (single-process engine).  When the WAL grows past
``AUTO_CHECKPOINT_FRAMES`` (or on close), committed frames are written
back to the main file and the WAL is truncated.  On open, a non-empty
WAL is replayed up to its last valid commit frame -- a torn write
crashes mid-commit loses only that transaction.
"""
from __future__ import annotations

import os
import struct
from typing import Dict, Optional, Tuple

from .errors import OperationalError

MAGIC = b"NIMBUS-DB v1\x00\x00\x00\x00"

HEADER_SIZE = 36
_HDR = struct.Struct(">16sIIIII")

PAGE_TABLE_LEAF = 0x0D
PAGE_TABLE_INTERIOR = 0x05
PAGE_INDEX_LEAF = 0x0A
PAGE_INDEX_INTERIOR = 0x02

AUTO_CHECKPOINT_FRAMES = 2000


def _checksum(data: bytes) -> int:
    # FNV-1a, 32 bit
    h = 0x811C9DC5
    for chunk_start in range(0, len(data), 512):
        chunk = data[chunk_start:chunk_start + 512]
        for b in chunk:
            h ^= b
            h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class Pager:
    """Manages pages, the freelist, transactions and the WAL."""

    def __init__(self, path: str, page_size: int = 4096) -> None:
        self.path = path
        self.wal_path = path + "-wal"
        self.page_size = page_size

        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.file = open(path, "r+b" if not is_new else "w+b")

        if is_new:
            self._init_new()
        else:
            self._load_header()

        # committed-but-not-checkpointed WAL pages: page_no -> bytes
        self._wal_overlay: Dict[int, bytes] = {}
        # pages dirtied by the open transaction
        self._dirty: Dict[int, bytearray] = {}
        self._wal_frame_count = 0
        self._wal_file = None
        self._in_txn = False

        self._recover_wal()

    # ------------------------------------------------------------------ #
    # header / creation
    # ------------------------------------------------------------------ #

    def _init_new(self) -> None:
        header = _HDR.pack(MAGIC, self.page_size, 1, 0, 0, 0)
        page0 = bytearray(self.page_size)
        page0[:HEADER_SIZE] = header
        self.file.write(page0)
        self.file.flush()
        os.fsync(self.file.fileno())
        self._header = page0

    def _load_header(self) -> None:
        self.file.seek(0)
        raw = self.file.read(HEADER_SIZE)
        if len(raw) < HEADER_SIZE:
            raise OperationalError(f"database file '{self.path}' is truncated")
        magic, page_size, _num, _schema, _flh, _flc = _HDR.unpack(raw)
        if magic != MAGIC:
            raise OperationalError(
                f"'{self.path}' is not a nimbus-db file (bad magic)")
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise OperationalError(f"corrupt header: bad page size {page_size}")
        self.page_size = page_size
        self._header = bytearray(self.file.read(self.page_size - HEADER_SIZE))
        self.file.seek(0)
        first = self.file.read(HEADER_SIZE)
        self._header = bytearray(first + bytes(self._header))

    # header fields ----------------------------------------------------- #

    def _hdr_get(self) -> Tuple[int, int, int, int]:
        return _HDR.unpack(bytes(self._header[:HEADER_SIZE]))[1:]

    def _hdr_set(self, page_size: int, num_pages: int, schema_root: int,
                 freelist_head: int, freelist_count: int) -> None:
        self._header[:HEADER_SIZE] = _HDR.pack(
            MAGIC, page_size, num_pages, schema_root, freelist_head,
            freelist_count)

    @property
    def num_pages(self) -> int:
        return self._hdr_get()[1]

    @property
    def schema_root(self) -> int:
        return self._hdr_get()[2]

    def set_schema_root(self, root: int) -> None:
        self._hdr_set(self.page_size, self.num_pages, root,
                      self._hdr_get()[3], self._hdr_get()[4])
        self._dirty[0] = self._header

    # ------------------------------------------------------------------ #
    # page access
    # ------------------------------------------------------------------ #

    def read_page(self, page_no: int) -> bytearray:
        if not 0 <= page_no < self.num_pages:
            raise OperationalError(f"page {page_no} out of range")
        if page_no in self._dirty:
            return self._dirty[page_no]
        if page_no in self._wal_overlay:
            return bytearray(self._wal_overlay[page_no])
        self.file.seek(page_no * self.page_size)
        raw = self.file.read(self.page_size)
        if len(raw) != self.page_size:
            raise OperationalError(
                f"short read: page {page_no} is beyond the end of the file "
                "(database is corrupt)")
        return bytearray(raw)

    def _write_page_direct(self, page_no: int, data: bytes) -> None:
        self.file.seek(page_no * self.page_size)
        self.file.write(data)

    def alloc_page(self) -> int:
        """Take a page from the freelist, or extend the file."""
        head, count = self._hdr_get()[3], self._hdr_get()[4]
        if count > 0 and head != 0:
            page = self.read_page(head)
            nxt = struct.unpack_from(">I", page, 0)[0]
            self._hdr_set(self.page_size, self.num_pages, self.schema_root,
                          nxt, count - 1)
            self._dirty[0] = self._header
            page[:] = bytes(self.page_size)
            self._dirty[head] = page
            return head
        new_no = self.num_pages
        self._hdr_set(self.page_size, new_no + 1, self.schema_root,
                      head, count)
        self._dirty[0] = self._header
        self._dirty[new_no] = bytearray(self.page_size)
        return new_no

    def free_page(self, page_no: int) -> None:
        if page_no == 0:
            raise OperationalError("cannot free the header page")
        head, count = self._hdr_get()[3], self._hdr_get()[4]
        page = bytearray(self.page_size)
        struct.pack_into(">I", page, 0, head)
        self._hdr_set(self.page_size, self.num_pages, self.schema_root,
                      page_no, count + 1)
        self._dirty[0] = self._header
        self._dirty[page_no] = page

    def write_page(self, page_no: int, data: bytearray) -> None:
        """Mark a page dirty inside the open transaction."""
        if not self._in_txn:
            raise OperationalError("write outside a transaction")
        self._dirty[page_no] = data

    # ------------------------------------------------------------------ #
    # transactions
    # ------------------------------------------------------------------ #

    @property
    def in_txn(self) -> bool:
        return self._in_txn

    def begin(self) -> None:
        if self._in_txn:
            raise OperationalError("transaction already open")
        self._in_txn = True

    def commit(self) -> None:
        if not self._in_txn:
            return
        self._in_txn = False
        if not self._dirty:
            return

        wf = self._wal_file
        if wf is None:
            wf = self._wal_file = open(self.wal_path, "ab")

        frames = sorted(self._dirty.items())
        for idx, (page_no, data) in enumerate(frames):
            last = idx == len(frames) - 1
            commit_flag = self.num_pages if last else 0
            blob = struct.pack(">III", page_no, commit_flag,
                               _checksum(bytes(data))) + bytes(data)
            wf.write(blob)
            self._wal_overlay[page_no] = bytes(data)
            self._wal_frame_count += 1
        wf.flush()
        os.fsync(wf.fileno())
        self._dirty.clear()

        if self._wal_frame_count >= AUTO_CHECKPOINT_FRAMES:
            self.checkpoint()

    def rollback(self) -> None:
        if not self._in_txn:
            self._dirty.clear()
            return
        self._in_txn = False
        # re-load the header as it was before the txn (page 0 may be dirty)
        if 0 in self._dirty:
            del self._dirty[0]
            if 0 in self._wal_overlay:
                self._header = bytearray(self._wal_overlay[0])
            else:
                self.file.seek(0)
                self._header = bytearray(self.file.read(self.page_size))
        self._dirty.clear()

    # ------------------------------------------------------------------ #
    # WAL recovery / checkpoint
    # ------------------------------------------------------------------ #

    def _recover_wal(self) -> None:
        if not os.path.exists(self.wal_path) or \
                os.path.getsize(self.wal_path) == 0:
            return
        frame_size = 12 + self.page_size
        with open(self.wal_path, "rb") as f:
            blob = f.read()

        frames: list = []
        off = 0
        while off + frame_size <= len(blob):
            page_no, commit, ck = struct.unpack_from(">III", blob, off)
            image = blob[off + 12: off + frame_size]
            if page_no == 0 and commit == 0 and ck == 0:
                break  # zero-filled tail (torn extend)
            if _checksum(image) != ck:
                break  # torn/corrupt frame: ignore everything after
            frames.append((page_no, commit, image))
            off += frame_size

        last_commit = -1
        for i, (_p, c, _img) in enumerate(frames):
            if c:
                last_commit = i
        if last_commit < 0:
            # no complete commit: discard the WAL entirely
            self._truncate_wal()
            return

        for page_no, _c, image in frames[:last_commit + 1]:
            self._write_page_direct(page_no, image)
            if page_no == 0:
                self._header = bytearray(image)
        self.file.flush()
        os.fsync(self.file.fileno())
        self._truncate_wal()

    def _truncate_wal(self) -> None:
        if self._wal_file is not None:
            self._wal_file.close()
            self._wal_file = None
        if os.path.exists(self.wal_path):
            with open(self.wal_path, "wb") as f:
                f.truncate(0)
            self._wal_frame_count = 0

    def checkpoint(self) -> None:
        """Write committed WAL frames back to the main file."""
        if self._in_txn:
            raise OperationalError("cannot checkpoint inside a transaction")
        if not self._wal_overlay:
            return
        for page_no, image in sorted(self._wal_overlay.items()):
            self._write_page_direct(page_no, image)
        self.file.flush()
        os.fsync(self.file.fileno())
        self._wal_overlay.clear()
        self._truncate_wal()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._in_txn:
            self.rollback()
        self.checkpoint()
        if self._wal_file is not None:
            self._wal_file.close()
            self._wal_file = None
        self.file.close()

    def __enter__(self) -> "Pager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
