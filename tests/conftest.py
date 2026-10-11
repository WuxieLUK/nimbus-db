import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nimbus_db import Database  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.ndb"))
    yield database
    database.close()


@pytest.fixture()
def emp_db(db):
    db.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT, location TEXT)")
    db.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, "
               "salary REAL, dept_id INT)")
    db.execute("INSERT INTO dept (name, location) VALUES "
               "('Engineering', 'BJ'), ('Sales', 'SH'), ('HR', 'SZ')")
    db.execute("""INSERT INTO emp (name, salary, dept_id) VALUES
        ('alice', 120000.0, 1), ('bob', 95000.0, 1), ('carol', 88000.0, 2),
        ('dave', 75000.0, 2), ('erin', 105000.0, 1), ('frank', 60000.0, NULL)""")
    return db
