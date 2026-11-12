"""Upgrade the local database, comparing every pre-existing field and uploaded file."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
database = root / "homework.db"
if not database.exists():
    raise SystemExit("No existing database to verify.")


def snapshots(connection):
    result = {}
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for (table,) in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        sql = 'SELECT ' + ','.join(f'"{column}"' for column in columns) + f' FROM "{table}" ORDER BY id'
        result[table] = (columns, connection.execute(sql).fetchall())
    return result


def upload_hashes():
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in (root / "uploads").glob("*") if path.is_file()}


with closing(sqlite3.connect(database)) as connection:
    original = snapshots(connection)
files = upload_hashes()

from app import app, migrate_polish_schema
from models import User, db

with app.app_context():
    migrate_polish_schema()
    migrate_polish_schema()
    users = [(u.id, u.role) for u in User.query.all()]
    db.session.remove()

pages = 0
for user_id, role in users:
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True
    for route in ("/dashboard", "/assignment/list", "/assignment/list?archived=1", "/profile", "/notifications",
                  "/gradebook" if role == "TEACHER" else "/my-grades"):
        assert client.get(route).status_code == 200, (user_id, route)
        pages += 1

with app.app_context():
    db.session.remove()
    db.engine.dispose()
with closing(sqlite3.connect(database)) as connection:
    for table, (columns, rows) in original.items():
        sql = 'SELECT ' + ','.join(f'"{column}"' for column in columns) + f' FROM "{table}" ORDER BY id'
        assert connection.execute(sql).fetchall() == rows, f"Original values changed: {table}"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert upload_hashes() == files, "Uploaded files changed"
assert (root / "homework.db.stage3.bak").is_file()
report = {"preserved_rows": {table: len(values[1]) for table, values in original.items()},
          "unchanged_uploads": len(files), "authenticated_pages": pages, "integrity": "ok",
          "backup": "homework.db.stage3.bak"}
(root / "artifacts").mkdir(exist_ok=True)
(root / "artifacts" / "upgrade-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
