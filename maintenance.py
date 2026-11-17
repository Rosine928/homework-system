"""Local operator commands; backups always restore into a new directory."""
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import shutil
import sqlite3
import click
from werkzeug.security import generate_password_hash
from models import User, AccountEvent, LoginAttempt, db
from migrations import integrity_issues


def digest(path):
    result = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def checked_file(root, name):
    path = root / name
    if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
        raise click.ClickException(f"Invalid backup file: {name}")
    return path


def verify_backup(directory):
    root = Path(directory).resolve()
    try:
        manifest = json.loads(checked_file(root, "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise click.ClickException("Missing or invalid backup manifest.") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise click.ClickException("Invalid backup manifest structure.")
    if manifest.get("format") != 1 or "homework.db" not in manifest.get("files", {}):
        raise click.ClickException("Unsupported or incomplete backup manifest.")
    for name, expected in manifest["files"].items():
        if name != "homework.db" and (not name.startswith("uploads/") or len(Path(name).parts) != 2):
            raise click.ClickException("Unexpected backup path.")
        if digest(checked_file(root, name)) != expected:
            raise click.ClickException(f"Backup checksum mismatch: {name}")
    with closing(sqlite3.connect((root / "homework.db").as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise click.ClickException("Backup database integrity check failed.")
        references = [row[0] for row in connection.execute("SELECT file_path FROM submission")]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(assignment)")}
        if "reference_path" in columns:
            references += [row[0] for row in connection.execute("SELECT reference_path FROM assignment WHERE reference_path IS NOT NULL")]
        for name in references:
            if f"uploads/{name}" not in manifest["files"]:
                raise click.ClickException(f"Missing referenced attachment: {name}")
        for table, column, parent in (("assignment", "teacher_id", "user"), ("assignment", "course_id", "course"),
            ("assignment", "class_id", "teaching_class"), ("submission", "student_id", "user"), ("submission", "assignment_id", "assignment")):
            invalid = connection.execute(f'SELECT 1 FROM "{table}" c LEFT JOIN "{parent}" p ON c."{column}"=p.id WHERE c."{column}" IS NOT NULL AND p.id IS NULL LIMIT 1').fetchone()
            if invalid:
                raise click.ClickException(f"Invalid legacy reference: {table}.{column}")
    return manifest


def register_maintenance(app):
    def lock_account_mutation():
        if db.engine.dialect.name == "sqlite":
            db.session.execute(db.text("BEGIN IMMEDIATE"))

    @app.cli.command("check-data")
    def check_data():
        """Check foreign keys and attachment references without modifying data."""
        issues = integrity_issues()
        from models import Submission, Assignment
        for name in [s.file_path for s in Submission.query.all()] + [a.reference_path for a in Assignment.query.filter(Assignment.reference_path.is_not(None)).all()]:
            path = Path(app.config["UPLOAD_FOLDER"]) / name
            if not path.is_file() or path.is_symlink() or Path(app.config["UPLOAD_FOLDER"]).resolve() not in path.resolve().parents:
                issues.append(f"missing or unsafe attachment: {name}")
        if issues:
            raise click.ClickException("; ".join(issues))
        click.echo("Foreign keys and attachments: OK")

    @app.cli.command("backup")
    @click.option("--destination", type=click.Path(path_type=Path), default=None)
    def backup(destination):
        """Back up SQLite and uploads while holding the application's write lock."""
        if db.engine.dialect.name != "sqlite":
            raise click.ClickException("This backup command supports SQLite only.")
        target = (destination or Path(app.root_path) / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
        uploads = Path(app.config["UPLOAD_FOLDER"]).resolve()
        if target == uploads or uploads in target.parents:
            raise click.ClickException("Backup destination must be outside uploads.")
        if target.exists():
            raise click.ClickException("Destination already exists; choose a new directory.")
        db.session.remove()
        source_path = Path(db.engine.url.database).resolve()
        target.mkdir(parents=True)
        (target / "uploads").mkdir()
        with closing(sqlite3.connect(source_path, timeout=30)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            try:
                with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target / "homework.db")) as destination_db:
                    source.backup(destination_db)
                for path in uploads.iterdir():
                    if path.is_symlink() or path.is_dir():
                        raise click.ClickException("Uploads contains unexpected directories or symbolic links.")
                    if path.is_file():
                        shutil.copy2(path, target / "uploads" / path.name)
                files = {"homework.db": digest(target / "homework.db")}
                files.update({f"uploads/{p.name}": digest(p) for p in (target / "uploads").iterdir()})
                (target / "manifest.json").write_text(json.dumps({"format": 1, "created_at": datetime.now().isoformat(),
                    "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
                verify_backup(target)
            finally:
                lock.rollback()
        click.echo(f"Verified backup: {target}")

    @app.cli.command("verify-backup")
    @click.argument("directory", type=click.Path(exists=True, path_type=Path))
    def verify(directory):
        """Check every checksum, database and referenced attachment."""
        manifest = verify_backup(directory)
        click.echo(f"Backup verified: {len(manifest['files'])} files")

    @app.cli.command("restore-backup")
    @click.argument("directory", type=click.Path(exists=True, path_type=Path))
    @click.option("--destination", type=click.Path(path_type=Path), required=True)
    def restore(directory, destination):
        """Verify and restore into a NEW directory, preserving any live database."""
        manifest = verify_backup(directory)
        destination = destination.resolve()
        if destination.exists():
            raise click.ClickException("Restore destination must not exist.")
        destination.mkdir(parents=True)
        (destination / "uploads").mkdir()
        for name in manifest["files"]:
            shutil.copy2(checked_file(directory.resolve(), name), destination / name)
        shutil.copy2(directory / "manifest.json", destination / "manifest.json")
        verify_backup(destination)
        click.echo(f"Restored and verified: {destination}. Stop the server before switching database/upload configuration.")

    @app.cli.command("create-teacher")
    @click.option("--username", required=True)
    @click.option("--name", required=True)
    @click.password_option()
    def create_teacher(username, name, password):
        """Create a teacher locally; password is prompted without echo."""
        lock_account_mutation()
        if not 2 <= len(username.strip()) <= 40 or not 1 <= len(name.strip()) <= 40 or not 8 <= len(password) <= 128:
            raise click.ClickException("Invalid username, name or password length (password: 8-128).")
        if User.query.filter_by(username=username.strip()).first():
            raise click.ClickException("Username already exists.")
        user = User(username=username.strip(), real_name=name.strip(), role="TEACHER", password=generate_password_hash(password))
        db.session.add(user)
        db.session.flush()
        db.session.add(AccountEvent(user_id=user.id, action="TEACHER_CREATED", details="local operator"))
        db.session.commit()
        click.echo("Teacher created.")

    @app.cli.command("reset-password")
    @click.option("--username", required=True)
    @click.password_option()
    def reset_password(username, password):
        """Reset an account after verifying its owner's identity outside the app."""
        lock_account_mutation()
        user = User.query.filter_by(username=username).first()
        if not user or not 8 <= len(password) <= 128:
            raise click.ClickException("Account missing or password length outside 8-128.")
        user.password = generate_password_hash(password)
        user.auth_version += 1
        from hashlib import sha256
        attempt = db.session.get(LoginAttempt, sha256(("account:" + username).encode("utf-8")).hexdigest())
        if attempt:
            db.session.delete(attempt)
        db.session.add(AccountEvent(user_id=user.id, action="PASSWORD_RESET", details="local operator"))
        db.session.commit()
        click.echo("Password reset; previous sessions revoked.")

    @app.cli.command("set-account")
    @click.option("--username", required=True)
    @click.option("--enabled/--disabled", default=True)
    def set_account(username, enabled):
        """Enable or disable an account; retain all submissions and grades."""
        lock_account_mutation()
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException("Account not found.")
        if not enabled and user.enabled and user.role == "TEACHER" and User.query.filter_by(role="TEACHER", enabled=True).count() <= 1:
            raise click.ClickException("Cannot disable the last active teacher.")
        user.enabled = enabled
        user.auth_version += 1
        db.session.add(AccountEvent(user_id=user.id, action="ACCOUNT_ENABLED" if enabled else "ACCOUNT_DISABLED", details="local operator"))
        db.session.commit()
        click.echo("Account status updated; previous sessions revoked.")
