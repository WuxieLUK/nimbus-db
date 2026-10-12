"""The SQL value model.

A value is a plain Python object:

* ``None``                    -> SQL NULL
* ``int`` / ``float``         -> numeric (bool is normalized to int)
* ``str``                     -> TEXT (UTF-8)
* ``bytes``                   -> BLOB

This module implements the on-disk record encoding (type tag + varint
lengths), SQL comparison semantics (including cross-type ordering and
NULL placement), three-valued truthiness, ``LIKE`` matching and
SQLite-style type affinity.
"""
from __future__ import annotations

import re
import struct
from fractions import Fraction
from typing import Any, List, Optional, Sequence, Tuple

from .errors import ConstraintError, TypeMismatchError

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

# record type tags
TAG_NULL, TAG_INT, TAG_FLOAT, TAG_TEXT, TAG_BLOB = range(5)

# type affinities
INTEGER, REAL, TEXT, BLOB, NUMERIC = "INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"

_U64_MASK = (1 << 64) - 1


# --------------------------------------------------------------------------- #
# varints: unsigned LEB128 for lengths, zig-zag LEB128 for signed integers
# --------------------------------------------------------------------------- #

def _write_varint(out: bytearray, n: int) -> None:
    """Signed 64-bit value as zig-zag LEB128."""
    z = (n << 1) ^ (n >> 63)
    while True:
        b = z & 0x7F
        z >>= 7
        if z:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _read_varint(buf: Sequence[int], off: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    result &= _U64_MASK
    return (result >> 1) ^ -(result & 1), off


def _write_uvarint(out: bytearray, n: int) -> None:
    """Unsigned value (lengths) as plain LEB128."""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _read_uvarint(buf: Sequence[int], off: int) -> Tuple[int, int]:
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


# --------------------------------------------------------------------------- #
# record serialization
# --------------------------------------------------------------------------- #

def _append_value(out: bytearray, v: Any) -> None:
    if v is None:
        out.append(TAG_NULL)
    elif isinstance(v, bool):
        out.append(TAG_INT)
        _write_varint(out, int(v))
    elif isinstance(v, int):
        if not (INT64_MIN <= v <= INT64_MAX):
            raise ConstraintError("integer value out of 64-bit range")
        out.append(TAG_INT)
        _write_varint(out, v)
    elif isinstance(v, float):
        out.append(TAG_FLOAT)
        out += struct.pack(">d", v)
    elif isinstance(v, str):
        raw = v.encode("utf-8")
        out.append(TAG_TEXT)
        _write_uvarint(out, len(raw))
        out += raw
    elif isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        out.append(TAG_BLOB)
        _write_uvarint(out, len(raw))
        out += raw
    else:
        raise TypeMismatchError(f"cannot store value of Python type {type(v).__name__}")


def serialize_value(v: Any) -> bytes:
    out = bytearray()
    _append_value(out, v)
    return bytes(out)


def deserialize_value(buf: Sequence[int], off: int = 0) -> Tuple[Any, int]:
    tag = buf[off]
    off += 1
    if tag == TAG_NULL:
        return None, off
    if tag == TAG_INT:
        return _read_varint(buf, off)
    if tag == TAG_FLOAT:
        return struct.unpack_from(">d", buf, off)[0], off + 8
    if tag == TAG_TEXT:
        n, off = _read_uvarint(buf, off)
        return bytes(buf[off:off + n]).decode("utf-8"), off + n
    if tag == TAG_BLOB:
        n, off = _read_uvarint(buf, off)
        return bytes(buf[off:off + n]), off + n
    raise ValueError(f"corrupt record: unknown type tag {tag}")


def serialize_row(values: Sequence[Any]) -> bytes:
    out = bytearray()
    for v in values:
        _append_value(out, v)
    return bytes(out)


def deserialize_row(buf: Sequence[int]) -> List[Any]:
    values: List[Any] = []
    off = 0
    end = len(buf)
    while off < end:
        v, off = deserialize_value(buf, off)
        values.append(v)
    return values


# --------------------------------------------------------------------------- #
# comparison / truthiness (SQL semantics)
# --------------------------------------------------------------------------- #

def type_rank(v: Any) -> int:
    """Cross-type ordering classes: NULL < numeric < text < blob."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1
    if isinstance(v, (int, float)):
        return 1
    if isinstance(v, str):
        return 2
    return 3


def sql_compare(a: Any, b: Any) -> int:
    """Total order over SQL values; NULL sorts before everything.

    NULL == NULL is considered *equal* here (index/order use).  SQL
    expression semantics (comparisons yield NULL) are handled by the
    evaluator, not by this function.
    """
    if a is None or b is None:
        if a is None and b is None:
            return 0
        return -1 if a is None else 1
    ra, rb = type_rank(a), type_rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 1:
        return -1 if a < b else (1 if a > b else 0)
    return -1 if a < b else (1 if a > b else 0)


def truthy(v: Any) -> bool:
    """WHERE/HAVING truth test: NULL and non-numeric values are false."""
    if v is None or isinstance(v, (str, bytes)):
        return False
    if isinstance(v, bool):
        return v
    return v != 0


def group_key(v: Any) -> Tuple:
    """Exact hash/equality key so that 1, 1.0 and True group together."""
    if v is None:
        return ("null",)
    if isinstance(v, bool):
        return ("n", Fraction(int(v)))
    if isinstance(v, int):
        return ("n", Fraction(v))
    if isinstance(v, float):
        return ("n", Fraction(v))
    if isinstance(v, str):
        return ("t", v)
    return ("b", bytes(v))


# --------------------------------------------------------------------------- #
# LIKE
# --------------------------------------------------------------------------- #

def like_match(value: Any, pattern: Any) -> Optional[bool]:
    """SQLite-style LIKE: % and _ wildcards, ASCII-case-insensitive.

    Returns None (SQL NULL) when either operand is NULL or not text.
    """
    if not isinstance(value, str) or not isinstance(pattern, str):
        return None
    parts = ["^"]
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    parts.append("$")
    rx = re.compile("".join(parts), re.IGNORECASE | re.DOTALL)
    return rx.match(value) is not None


# --------------------------------------------------------------------------- #
# type affinity (SQLite rules, https://sqlite.org/datatype3.html §3.1)
# --------------------------------------------------------------------------- #

def affinity_of(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return INTEGER
    if "CHAR" in d or "CLOB" in d or "TEXT" in d:
        return TEXT
    if not d or "BLOB" in d:
        return BLOB
    if "REAL" in d or "FLOA" in d or "DOUB" in d:
        return REAL
    return NUMERIC


def coerce_for_column(value: Any, affinity: str, column: str) -> Any:
    """Validate/coerce a value for storage.

    nimbus-db is deliberately stricter than SQLite: numbers never
    silently become text and vice versa; only lossless int<->float
    conversions are performed.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        value = int(value)

    def fail() -> None:
        got = "NULL" if value is None else (
            "INTEGER" if isinstance(value, int) else
            "REAL" if isinstance(value, float) else
            "TEXT" if isinstance(value, str) else "BLOB")
        raise TypeMismatchError(
            f"cannot store {got} value in column '{column}' ({affinity})")

    if affinity == INTEGER:
        if isinstance(value, int):
            if not (INT64_MIN <= value <= INT64_MAX):
                raise ConstraintError("integer value out of 64-bit range")
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        fail()
    if affinity == REAL:
        if isinstance(value, (int, float)):
            return float(value)
        fail()
    if affinity == TEXT:
        if isinstance(value, str):
            return value
        fail()
    if affinity == BLOB:
        if isinstance(value, bytes):
            return value
        fail()
    # NUMERIC keeps what it is given
    if isinstance(value, (int, float, str, bytes)):
        return value
    fail()
    raise AssertionError("unreachable")
