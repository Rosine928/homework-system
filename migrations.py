"""Numbered, additive SQLite migrations; legacy upgrade remains in app.py."""
from pathlib import Path
from contextlib import closing
import sqlite3

from sqlalchemy import inspect, text
from models import SchemaMigration, db


def backup_before_upgrade():
    path = db.engine.url.database
    if db.engine.dialect.name != "sqlite" or not path or not Path(path).is_file():
        return
    tables = inspect(db.engine).get_table_names()
    if "user" not in tables:
        return
    columns = {c["name"] for c in inspect(db.engine).get_columns("user")}
    if "email" not in columns:
        backup = Path(str(path) + ".stage5.bak")
        if not backup.exists():
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
    if "auth_version" not in columns:
        backup = Path(str(path) + ".stage4.bak")
        if not backup.exists():
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)


def integrity_issues():
    issues = []
    if db.engine.dialect.name == "sqlite":
        issues.extend(str(tuple(row)) for row in db.session.execute(text("PRAGMA foreign_key_check")))
    # Early project schemas did not declare SQL foreign keys. Check those too.
    for table, column, parent in (
        ("course", "teacher_id", "user"), ("teaching_class", "course_id", "course"),
        ("teaching_class", "teacher_id", "user"), ("assignment", "teacher_id", "user"),
        ("assignment", "course_id", "course"), ("assignment", "class_id", "teaching_class"),
        ("submission", "assignment_id", "assignment"), ("submission", "student_id", "user"),
        ("enrollment", "student_id", "user"), ("enrollment", "class_id", "teaching_class"),
    ):
        count = db.session.execute(text(
            f'SELECT count(*) FROM "{table}" c LEFT JOIN "{parent}" p ON c."{column}"=p.id '
            f'WHERE c."{column}" IS NOT NULL AND p.id IS NULL')).scalar()
        if count:
            issues.append(f"{table}.{column}: {count} invalid references")
    return issues


def upgrade():
    if db.session.get(SchemaMigration, 4):
        return
    additions = {
        "user": (("enabled", "BOOLEAN NOT NULL DEFAULT 1"), ("auth_version", "INTEGER NOT NULL DEFAULT 1")),
        "course": (("archived", "BOOLEAN NOT NULL DEFAULT 0"),),
        "teaching_class": (("archived", "BOOLEAN NOT NULL DEFAULT 0"), ("join_enabled", "BOOLEAN NOT NULL DEFAULT 1")),
        "enrollment": (("active", "BOOLEAN NOT NULL DEFAULT 1"),),
        "assignment": (("revision", "INTEGER NOT NULL DEFAULT 1"), ("reference_path", "VARCHAR(255)"),
                       ("reference_name", "VARCHAR(255)"), ("rubric", "TEXT NOT NULL DEFAULT ''")),
        "submission": (("revision", "INTEGER NOT NULL DEFAULT 1"), ("request_token", "VARCHAR(64)")),
    }
    for table, fields in additions.items():
        columns = {c["name"] for c in inspect(db.engine).get_columns(table)}
        for name, declaration in fields:
            if name not in columns:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'))
    db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_request ON submission(student_id, request_token)"))
    db.session.commit()


def upgrade_email_schema():
    if db.session.get(SchemaMigration, 5):
        return
    columns = {c["name"] for c in inspect(db.engine).get_columns("user")}
    for name, declaration in (("email", "VARCHAR(254)"), ("email_verified_at", "DATETIME")):
        if name not in columns:
            db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN "{name}" {declaration}'))
    db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email ON "user" (email)'))
    db.session.add(SchemaMigration(id=5))
    db.session.commit()


def finish_upgrade():
    if db.session.get(SchemaMigration, 4):
        return
    issues = integrity_issues()
    if issues:
        raise RuntimeError("Database upgrade stopped; restore or repair references: " + "; ".join(issues))
    if db.engine.dialect.name == "sqlite":
        # Enforce links on databases created before foreign keys were declared.
        for table, column, parent in (
            ("assignment", "teacher_id", "user"), ("assignment", "course_id", "course"),
            ("assignment", "class_id", "teaching_class"), ("submission", "assignment_id", "assignment"),
            ("submission", "student_id", "user"),
        ):
            for operation in ("INSERT", "UPDATE"):
                name = f"fk_{table}_{column}_{operation.lower()}"
                db.session.execute(text(
                    f'CREATE TRIGGER IF NOT EXISTS "{name}" BEFORE {operation} ON "{table}" '
                    f'WHEN NEW."{column}" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "{parent}" WHERE id=NEW."{column}") '
                    "BEGIN SELECT RAISE(ABORT, 'invalid legacy foreign key'); END"))
            name = f"fk_{table}_{column}_delete"
            db.session.execute(text(
                f'CREATE TRIGGER IF NOT EXISTS "{name}" BEFORE DELETE ON "{parent}" '
                f'WHEN EXISTS (SELECT 1 FROM "{table}" WHERE "{column}"=OLD.id) '
                "BEGIN SELECT RAISE(ABORT, 'referenced legacy row'); END"))
    db.session.add(SchemaMigration(id=4))
    db.session.commit()
