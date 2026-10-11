import os

import pytest

from nimbus_db.errors import OperationalError
from nimbus_db.pager import Pager


class TestPagerBasics:
    def test_create_and_reload(self, tmp_path):
        path = str(tmp_path / "a.ndb")
        p = Pager(path)
        p.begin()
        pg = p.alloc_page()
        p.write_page(pg, bytearray(b"A" * p.page_size))
        p.commit()
        p.close()

        p2 = Pager(path)
        assert p2.num_pages == 2
        assert p2.read_page(pg) == bytearray(b"A" * p2.page_size)
        p2.close()

    def test_bad_magic(self, tmp_path):
        path = str(tmp_path / "b.ndb")
        with open(path, "wb") as f:
            f.write(b"NOT A DATABASE" * 100)
        with pytest.raises(OperationalError):
            Pager(path)

    def test_write_outside_txn_rejected(self, tmp_path):
        p = Pager(str(tmp_path / "c.ndb"))
        with pytest.raises(OperationalError):
            p.write_page(0, bytearray(p.page_size))
        p.close()

    def test_double_begin_rejected(self, tmp_path):
        p = Pager(str(tmp_path / "d.ndb"))
        p.begin()
        with pytest.raises(OperationalError):
            p.begin()
        p.rollback()
        p.close()


class TestTransactions:
    def test_rollback_restores(self, tmp_path):
        path = str(tmp_path / "e.ndb")
        p = Pager(path)
        p.begin()
        pg = p.alloc_page()
        p.write_page(pg, bytearray(b"X" * p.page_size))
        p.commit()
        p.begin()
        p.write_page(pg, bytearray(b"Y" * p.page_size))
        pg2 = p.alloc_page()
        p.write_page(pg2, bytearray(b"Z" * p.page_size))
        p.rollback()
        assert p.read_page(pg) == bytearray(b"X" * p.page_size)
        assert p.num_pages == 2
        p.close()

    def test_freelist_roundtrip(self, tmp_path):
        path = str(tmp_path / "f.ndb")
        p = Pager(path)
        p.begin()
        a = p.alloc_page()
        b = p.alloc_page()
        c = p.alloc_page()
        p.commit()
        p.begin()
        p.free_page(b)
        p.commit()
        p.begin()
        reused = p.alloc_page()
        assert reused == b
        p.commit()
        p.close()


class TestWalRecovery:
    def test_recovery_replays_committed(self, tmp_path):
        path = str(tmp_path / "g.ndb")
        p = Pager(path)
        p.begin()
        pg = p.alloc_page()
        p.write_page(pg, bytearray(b"R" * p.page_size))
        p.commit()
        # simulate crash: do not checkpoint/close; open a new pager
        p2 = Pager(path)
        assert p2.read_page(pg) == bytearray(b"R" * p2.page_size)
        p2.close()

    def test_torn_commit_dropped(self, tmp_path):
        path = str(tmp_path / "h.ndb")
        p = Pager(path)
        p.begin()
        pg = p.alloc_page()
        p.write_page(pg, bytearray(b"C" * p.page_size))
        p.commit()
        # append a torn (partial) frame as if the process died mid-write
        with open(path + "-wal", "ab") as f:
            f.write(b"\x01\x00\x00\x00\x02\x00\x00\x00\x00garbage-not-a-page")
        p2 = Pager(path)
        assert p2.read_page(pg) == bytearray(b"C" * p2.page_size)
        p2.close()

    def test_checkpoint_persists(self, tmp_path):
        path = str(tmp_path / "i.ndb")
        p = Pager(path)
        p.begin()
        pg = p.alloc_page()
        p.write_page(pg, bytearray(b"K" * p.page_size))
        p.commit()
        p.checkpoint()
        assert os.path.getsize(path + "-wal") == 0
        with open(path, "rb") as f:
            f.seek(pg * p.page_size)
            assert f.read(1) == b"K"
        p.close()
