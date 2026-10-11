import pytest

from nimbus_db.errors import ConstraintError, TypeMismatchError
from nimbus_db.values import (affinity_of, coerce_for_column, deserialize_row,
                              group_key, like_match, serialize_row,
                              serialize_value, sql_compare, truthy)


class TestRecordEncoding:
    def test_roundtrip_all_types(self):
        row = [None, 42, -3.5, "hello wörld", b"\x00\x01\xff", True]
        data = serialize_row(row)
        assert deserialize_row(data) == [None, 42, -3.5, "hello wörld",
                                         b"\x00\x01\xff", 1]

    def test_roundtrip_extremes(self):
        row = [2 ** 63 - 1, -(2 ** 63), 1e300, -0.0, "", ""]
        assert deserialize_row(serialize_row(row)) == row

    def test_int_out_of_range(self):
        with pytest.raises(ConstraintError):
            serialize_value(2 ** 63)

    def test_empty_row(self):
        assert deserialize_row(serialize_row([])) == []


class TestComparison:
    def test_cross_type_ordering(self):
        # NULL < numbers < text < blob
        assert sql_compare(None, 1) < 0
        assert sql_compare(1, "a") < 0
        assert sql_compare("a", b"b") < 0
        assert sql_compare(2.5, 3) < 0
        assert sql_compare("a", "b") < 0

    def test_bool_is_numeric(self):
        assert sql_compare(True, 1) == 0
        assert sql_compare(False, 0) == 0


class TestTruthiness:
    def test_truthy(self):
        assert truthy(1) and truthy(2.5) and truthy(-1)
        assert not truthy(0) and not truthy(0.0)
        assert not truthy(None)
        assert not truthy("") and not truthy("abc")  # text is not boolean


class TestGroupKey:
    def test_numeric_identity(self):
        assert group_key(1) == group_key(1.0) == group_key(True)
        assert group_key(1) != group_key("1")
        assert group_key(None) == group_key(None)


class TestLike:
    def test_patterns(self):
        assert like_match("hello", "h%") is True
        assert like_match("hello", "H%") is True       # ascii case-insensitive
        assert like_match("hello", "h_llo") is True
        assert like_match("hello", "%ell%") is True
        assert like_match("hello", "x%") is False
        assert like_match(None, "h%") is None
        assert like_match("hello", None) is None
        assert like_match(5, "h%") is None
        assert like_match("100%", "100%") is True


class TestAffinity:
    def test_affinity_rules(self):
        assert affinity_of("INTEGER") == "INTEGER"
        assert affinity_of("BIGINT") == "INTEGER"
        assert affinity_of("VARCHAR(255)") == "TEXT"
        assert affinity_of("TEXT") == "TEXT"
        assert affinity_of("REAL") == "REAL"
        assert affinity_of("DOUBLE PRECISION") == "REAL"
        assert affinity_of("BLOB") == "BLOB"
        assert affinity_of("") == "BLOB"
        assert affinity_of("DECIMAL(10,2)") == "NUMERIC"

    def test_coercion(self):
        assert coerce_for_column(1.0, "INTEGER", "c") == 1
        assert coerce_for_column(2, "REAL", "c") == 2.0
        with pytest.raises(TypeMismatchError):
            coerce_for_column("abc", "INTEGER", "c")
        with pytest.raises(TypeMismatchError):
            coerce_for_column(123, "TEXT", "c")
        with pytest.raises(TypeMismatchError):
            coerce_for_column(123, "BLOB", "c")
