import os
import random

import pytest

from nimbus_db.btree import BTree
from nimbus_db.errors import OperationalError
from nimbus_db.pager import Pager
from nimbus_db.values import serialize_row


@pytest.fixture()
def pager(tmp_path):
    p = Pager(str(tmp_path / "bt.ndb"))
    p.begin()
    yield p
    p.rollback()
    p.close()


class TestTableTree:
    def test_sequential_inserts(self, pager):
        tree = BTree.create_root(pager, "table")
        for i in range(1, 1001):
            tree.insert(i, serialize_row([f"v{i}"]))
        assert [r for r, _ in tree.iter_table()] == list(range(1, 1001))
        assert tree.count() == 1000

    def test_random_inserts_lookup_all(self, pager):
        tree = BTree.create_root(pager, "table")
        keys = list(range(1, 2001))
        random.Random(42).shuffle(keys)
        for k in keys:
            tree.insert(k, serialize_row([k * 2]))
        for k in keys:
            assert deserialize(tree.lookup(k)) == [k * 2]

    def test_multi_level_splits(self, pager):
        tree = BTree.create_root(pager, "table")
        for i in range(5000):
            tree.insert(i, serialize_row([i]))
        # root must be interior now (>= 2 levels)
        page = pager.read_page(tree.root)
        assert page[0] == 0x05  # TABLE_INTERIOR
        assert tree.count() == 5000
        assert tree.max_rowid() == 4999

    def test_delete_rebalance(self, pager):
        tree = BTree.create_root(pager, "table")
        keys = list(range(1, 2001))
        random.Random(7).shuffle(keys)
        for k in keys:
            tree.insert(k, serialize_row([k]))
        random.Random(8).shuffle(keys)
        deleted = set()
        for i, k in enumerate(keys):
            assert tree.delete(k)
            deleted.add(k)
            if (i + 1) % 250 == 0:
                for x in deleted:
                    assert tree.lookup(x) is None
                for x in keys:
                    if x not in deleted:
                        assert tree.lookup(x) is not None
        assert tree.count() == 0

    def test_delete_missing_key(self, pager):
        tree = BTree.create_root(pager, "table")
        tree.insert(1, serialize_row([1]))
        assert tree.delete(2) is False
        assert tree.delete(1) is True
        assert tree.delete(1) is False

    def test_large_payloads_overflow(self, pager):
        tree = BTree.create_root(pager, "table")
        blob = b"z" * 30000
        tree.insert(1, serialize_row([blob]))
        tree.insert(2, serialize_row([b"small"]))
        tree.insert(3, serialize_row([b"w" * 17000]))
        assert deserialize(tree.lookup(1)) == [blob]
        assert deserialize(tree.lookup(2)) == [b"small"]
        assert deserialize(tree.lookup(3)) == [b"w" * 17000]
        assert tree.delete(1)
        assert tree.lookup(1) is None
        assert deserialize(tree.lookup(3)) == [b"w" * 17000]

    def test_range_scan(self, pager):
        tree = BTree.create_root(pager, "table")
        for i in range(100):
            tree.insert(i, serialize_row([i]))
        rids = [r for r, _ in tree.iter_table_range(10, 19)]
        assert rids == list(range(10, 20))
        rids = [r for r, _ in tree.iter_table_range(10, 20, hi_incl=False)]
        assert rids == list(range(10, 20))


class TestIndexTree:
    def test_composite_keys(self, pager):
        tree = BTree.create_root(pager, "index")
        entries = [(f"g{i % 5}", i % 17, i) for i in range(1, 1001)]
        random.Random(3).shuffle(entries)
        for key in entries:
            tree.insert(key, b"")
        for key in entries:
            assert tree.lookup_index(key)
        assert tree.count() == 1000

    def test_sorted_scan(self, pager):
        tree = BTree.create_root(pager, "index")
        entries = [(i % 10, -i, i) for i in range(500)]
        random.Random(5).shuffle(entries)
        for key in entries:
            tree.insert(key, b"")
        seen = list(tree.iter_index_all())
        assert seen == sorted(seen)

    def test_prefix_search(self, pager):
        tree = BTree.create_root(pager, "index")
        entries = [(f"n{i % 7}", i % 11, i) for i in range(700)]
        random.Random(9).shuffle(entries)
        for key in entries:
            tree.insert(key, b"")
        found = list(tree.iter_index_from(
            ("n3", 4), lambda k: not (k[0] == "n3" and k[1] == 4)))
        assert found and all(k[0] == "n3" and k[1] == 4 for k in found)
        expected = sum(1 for e in entries if e[0] == "n3" and e[1] == 4)
        assert len(found) == expected

    def test_seek_prefix(self, pager):
        tree = BTree.create_root(pager, "index")
        for i in range(100):
            tree.insert(("a", i, i), b"")
        assert tree.seek_prefix(("a", 50))[2] == 50
        assert tree.seek_prefix(("a", 500)) is None
        assert tree.seek_prefix(("b", 1)) is None

    def test_index_delete_rebalance(self, pager):
        tree = BTree.create_root(pager, "index")
        entries = [(i % 13, i % 7, i) for i in range(800)]
        random.Random(11).shuffle(entries)
        for key in entries:
            tree.insert(key, b"")
        random.Random(12).shuffle(entries)
        gone = set()
        for i, key in enumerate(entries):
            assert tree.delete(key)
            gone.add(key)
            if (i + 1) % 200 == 0:
                for x in gone:
                    assert not tree.lookup_index(x)
                for x in entries:
                    if x not in gone:
                        assert tree.lookup_index(x)

    def test_unique_index_flag(self, pager):
        tree = BTree.create_root(pager, "index")
        tree.insert(("k", 1), b"")
        assert tree.seek_prefix(("k",)) is not None


def deserialize(payload):
    from nimbus_db.values import deserialize_row
    return deserialize_row(payload)


class TestWalkAndFree:
    def test_walk_finds_all_pages(self, pager):
        tree = BTree.create_root(pager, "table")
        for i in range(300):
            tree.insert(i, serialize_row([i]))
        tree.insert(100000, serialize_row([b"x" * 9000]))  # overflow
        pages = tree.walk_pages()
        assert tree.root in pages
        assert len(pages) == len(set(pages))
        before = pager.num_pages
        tree.free_all()
        head, count = pager._hdr_get()[3], pager._hdr_get()[4]
        assert count == len(pages)

    def test_freelist_reuse(self, pager):
        tree = BTree.create_root(pager, "table")
        for i in range(500):
            tree.insert(i, serialize_row([i]))
        pages_before = pager.num_pages
        for i in range(0, 500, 2):
            tree.delete(i)
        # deleting compacts; allocating afterwards should reuse freed pages
        tree2 = BTree.create_root(pager, "table")
        for i in range(500):
            tree2.insert(i, serialize_row([i]))
        assert pager.num_pages < pages_before + 500
