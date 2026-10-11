"""Exception hierarchy for nimbus-db.

Every error raised intentionally by the engine derives from :class:`NimbusError`
so callers can distinguish engine errors from internal bugs.
"""
from __future__ import annotations


class NimbusError(Exception):
    """Base class for all nimbus-db errors."""


class ParseError(NimbusError):
    """SQL text could not be tokenized or parsed."""


class SchemaError(NimbusError):
    """A statement references a table/column/index that does not exist."""


class ConstraintError(NimbusError):
    """A constraint was violated: PRIMARY KEY, UNIQUE, NOT NULL, range, ..."""


class TypeMismatchError(NimbusError):
    """A value does not fit the column's type affinity."""


class UnsupportedError(NimbusError):
    """Valid SQL that this engine deliberately does not implement (yet)."""


class OperationalError(NimbusError):
    """Runtime failure: I/O, corrupted file, transaction misuse, ..."""
