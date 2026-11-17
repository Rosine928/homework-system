"""Verify imported demo records and compare original records with the pre-import backup."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import app
from flask import g
from models import Assignment, Submission, User, db


def verify():
    report = json.loads((ROOT / 'artifacts' / 'demo-data-import.json').read_text(encoding='utf-8'))
    preserved = {}
    with app.app_context():
        with closing(sqlite3.connect(Path(report['backup']) / 'homework.db')) as before:
            tables = before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            for (table,) in tables:
                # Quote introspected names, never interpolate user input into SQL.
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in before.execute(f'PRAGMA table_info({quoted})')]
                names = ','.join('"' + col.replace('"', '""') + '"' for col in columns)
                old = before.execute(f'SELECT {names} FROM {quoted}').fetchall()
                current = set(tuple(row) for row in db.session.execute(db.text(f'SELECT {names} FROM {quoted}')))
                assert all(row in current for row in old), f'Original records changed: {table}'
                preserved[table] = len(old)
        assert db.session.execute(db.text('PRAGMA integrity_check')).scalar() == 'ok'
        assert not db.session.execute(db.text('PRAGMA foreign_key_check')).fetchall()
        uploads = Path(app.config['UPLOAD_FOLDER'])
        for submission in Submission.query.all():
            assert (uploads / submission.file_path).is_file()
        for assignment in Assignment.query.filter(Assignment.reference_path.is_not(None)).all():
            assert (uploads / assignment.reference_path).is_file()
        users = User.query.filter(User.id.in_(report['existing_student_ids'] + [report['existing_teacher_id']])
            | User.username.in_(['demo-teacher01', 'demo-student01'])).all()
        checked = 0
        for user in users:
            # This verifier shares an app context; clear Flask-Login's cached user.
            g.pop('_login_user', None)
            client = app.test_client()
            with client.session_transaction() as session:
                session['_user_id'] = user.get_id()
                session['_fresh'] = True
            paths = ['/dashboard', '/course/list', '/assignment/list', '/profile', '/notifications',
                     '/gradebook' if user.role == 'TEACHER' else '/my-grades']
            for path in paths:
                response = client.get(path)
                assert response.status_code == 200, (user.id, path, response.status_code)
                checked += 1
            if user.id in report['existing_student_ids'] or user.id == report['existing_teacher_id']:
                assert '（演示）'.encode() in client.get('/course/list').data
        result = {'status': 'passed', 'preserved_original_records': preserved,
                  'checked_pages': checked, 'integrity': 'ok', 'attachments': 'ok'}
        (ROOT / 'artifacts' / 'demo-data-verification.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    verify()
