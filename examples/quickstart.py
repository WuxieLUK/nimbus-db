"""nimbus-db quickstart: a tiny blog schema exercising the main features.

Run:  python examples/quickstart.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nimbus_db import Database

db = Database(os.path.join(tempfile.mkdtemp(), "blog.ndb"))

# --- schema -----------------------------------------------------------
db.execute("""
    CREATE TABLE authors (
        id      INTEGER PRIMARY KEY,
        name    TEXT NOT NULL UNIQUE,
        joined  TEXT DEFAULT '2024-01-01'
    )
""")
db.execute("""
    CREATE TABLE posts (
        id      INTEGER PRIMARY KEY,
        title   TEXT NOT NULL,
        body    TEXT,
        author  INT NOT NULL,
        tags    TEXT,
        rating  REAL
    )
""")
db.execute("CREATE INDEX idx_posts_author ON posts (author)")
db.execute("CREATE INDEX idx_posts_rating ON posts (rating)")

# --- data -------------------------------------------------------------
db.execute("BEGIN")
db.execute("INSERT INTO authors (name) VALUES ('ada'), ('grace'), ('linus')")
db.execute("""
    INSERT INTO posts (title, body, author, tags, rating) VALUES
        ('B-trees explained',      'Pages all the way down...', 1, 'db,storage',  4.8),
        ('WAL durability',         'fsync or it did not happen', 1, 'db,wal',     4.5),
        ('Compilers 101',          'Lex, parse, evaluate',       2, 'lang',        4.1),
        ('Garbage collection',     'Mark and sweep',             2, 'lang,memory', 3.9),
        ('Version control models', 'DAGs everywhere',            3, 'vcs',         4.3),
        ('unix philosophy',        'Do one thing well',          3, 'unix',        4.9)
""")
db.execute("COMMIT")

# a rollback that leaves nothing behind
db.execute("BEGIN")
db.execute("INSERT INTO posts (title, author) VALUES ('draft', 1)")
db.execute("ROLLBACK")

# --- queries ----------------------------------------------------------
def show(title, sql):
    res = db.execute(sql)
    print(f"-- {title}")
    print("   " + " | ".join(res.columns))
    for row in res.rows:
        print("   " + " | ".join("-" if v is None else str(v) for v in row))
    print()


show("join + aggregate + having", """
    SELECT a.name, COUNT(*) AS posts, ROUND(AVG(p.rating), 2) AS avg_rating
    FROM posts p JOIN authors a ON p.author = a.id
    GROUP BY a.name
    HAVING COUNT(*) >= 2
    ORDER BY avg_rating DESC
""")

show("subquery + between", """
    SELECT title FROM posts
    WHERE author IN (SELECT id FROM authors WHERE joined = '2024-01-01')
      AND rating BETWEEN 4.0 AND 4.6
    ORDER BY title
""")

show("case + group_concat", """
    SELECT tags,
           GROUP_CONCAT(title, ' + ') AS titles,
           CASE WHEN MAX(rating) >= 4.5 THEN 'gold' ELSE 'silver' END AS tier
    FROM posts
    GROUP BY tags
    ORDER BY tier, tags
    LIMIT 5
""")

show("indexed point lookup (plan shown)", """
    EXPLAIN SELECT title FROM posts WHERE rating = 4.9
""")
show("...and the query itself",
     "SELECT title FROM posts WHERE rating = 4.9")

show("the schema table", """
    SELECT type, name FROM nimbus_master ORDER BY type, name
""")

db.close()
print("quickstart complete (database was temporary).")
