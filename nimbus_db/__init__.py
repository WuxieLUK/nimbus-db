"""nimbus-db: a small SQL database engine written from scratch in
pure Python -- recursive-descent parser, B+tree storage, WAL
durability, and a volcano-model execution pipeline.

Public API::

    from nimbus_db import Database

    db = Database("app.ndb")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO users (name) VALUES ('ada')")
    result = db.execute("SELECT * FROM users")
    db.close()
"""
from .db import Database
from .errors import (ConstraintError, NimbusError, OperationalError,
                     ParseError, SchemaError, TypeMismatchError,
                     UnsupportedError)
from .executor import Result

__version__ = "1.0.0"

__all__ = [
    "Database",
    "Result",
    "NimbusError",
    "ParseError",
    "SchemaError",
    "ConstraintError",
    "TypeMismatchError",
    "UnsupportedError",
    "OperationalError",
    "__version__",
]
