"""Stage-two and final-stage integration tests, using isolated data and files."""
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from secrets import token_urlsafe
from flask.testing import FlaskClient
import csv
import re
import subprocess
import sys
from html.parser import HTMLParser
from unittest.mock import patch


_temporary = tempfile.TemporaryDirectory(prefix="homework-stage2-")
_root = Path(_temporary.name)
_database = _root / "test.db"
_previous_environment = {key: os.environ.get(key) for key in ("HOMEWORK_DATABASE_URI", "HOMEWORK_UPLOAD_FOLDER")}
os.environ["HOMEWORK_DATABASE_URI"] = f"sqlite:///{_database.as_posix()}"
os.environ["HOMEWORK_UPLOAD_FOLDER"] = str(_root / "uploads")

# Begin with a real stage-one schema to exercise startup migration and backup.
with closing(sqlite3.connect(_database)) as connection:
    connection.executescript("""
        CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
        CREATE TABLE assignment (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, teacher_id INTEGER NOT NULL);
        CREATE TABLE submission (id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
            file_path TEXT NOT NULL, original_name TEXT NOT NULL, score INTEGER, comment TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL);
        INSERT INTO user VALUES (1, 'legacy-teacher', 'password', 'TEACHER');
        INSERT INTO user VALUES (2, 'legacy-student', 'password', 'STUDENT');
        INSERT INTO user VALUES (3, 'legacy-outsider', 'password', 'STUDENT');
        INSERT INTO assignment VALUES (1, 'legacy-task', 'keep this task', 1);
        INSERT INTO submission (id, assignment_id, student_id, file_path, original_name, score, comment)
            VALUES (1, 1, 2, 'old.txt', 'old.txt', 88, 'keep this grade');
    """)

from app import app, assignment_card, assignment_status, migrate_final_schema, migrate_legacy_schema, student_can_view
from models import Assignment, Course, Enrollment, Notification, ReviewEvent, Submission, TeachingClass, User, beijing_now, db
from workflow import assignment_statistics, grade_rows, latest_submission
from werkzeug.security import generate_password_hash

app.config.update(TESTING=True)


class CSRFClient(FlaskClient):
    """Submit normal test forms with a session-bound token, as a browser would."""
    def post(self, *args, **kwargs):
        with self.session_transaction() as session:
            token = session.setdefault("csrf_token", token_urlsafe(32))
        data = dict(kwargs.pop("data", {}) or {})
        data.setdefault("csrf_token", token)
        return super().post(*args, data=data, **kwargs)


def tearDownModule():
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    _temporary.cleanup()
    for key, previous in _previous_environment.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


class MigrationTests(unittest.TestCase):
    def test_legacy_data_backup_private_class_and_idempotence(self):
        with app.app_context():
            assignment = db.session.get(Assignment, 1)
            self.assertEqual(assignment.title, "legacy-task")
            self.assertIsNotNone(assignment.class_id)
            self.assertEqual(assignment.course.teacher_id, 1)
            self.assertTrue(student_can_view(assignment, 2))
            self.assertFalse(student_can_view(assignment, 3))
            self.assertEqual(db.session.get(Submission, 1).score, 88)
            self.assertEqual(db.session.get(Submission, 1).review_state, "GRADED")
            migrate_legacy_schema()
            migrate_final_schema()
            migrate_final_schema()
            self.assertEqual(Course.query.count(), 1)
            self.assertEqual(TeachingClass.query.count(), 1)
            self.assertEqual(Enrollment.query.count(), 1)
        with closing(sqlite3.connect(str(_database) + ".stage1.bak")) as backup:
            columns = {row[1] for row in backup.execute("PRAGMA table_info(assignment)")}
            self.assertNotIn("class_id", columns)
            self.assertEqual(backup.execute("SELECT score FROM submission").fetchone()[0], 88)
        self.assertTrue(Path(str(_database) + ".stage2.bak").is_file())

    def test_real_stage_two_upgrade_backup_and_repeat_start(self):
        with tempfile.TemporaryDirectory(prefix="homework-stage2-upgrade-") as temporary:
            database = Path(temporary) / "stage2.db"
            with closing(sqlite3.connect(str(_database) + ".stage1.bak")) as source:
                with closing(sqlite3.connect(database)) as target:
                    source.backup(target)
                    target.executescript("""
                        ALTER TABLE assignment ADD COLUMN course_id INTEGER;
                        ALTER TABLE assignment ADD COLUMN class_id INTEGER;
                        ALTER TABLE assignment ADD COLUMN due_at DATETIME;
                        CREATE TABLE course (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                            teacher_id INTEGER NOT NULL, created_at DATETIME NOT NULL);
                        CREATE TABLE teaching_class (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                            join_code TEXT UNIQUE NOT NULL, course_id INTEGER NOT NULL, teacher_id INTEGER NOT NULL,
                            created_at DATETIME NOT NULL);
                        CREATE TABLE enrollment (id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL,
                            student_id INTEGER NOT NULL, joined_at DATETIME NOT NULL, UNIQUE(class_id, student_id));
                        INSERT INTO course VALUES (1, 'Stage two course', 'Keep description', 1, '2026-01-01 00:00:00');
                        INSERT INTO teaching_class VALUES (1, 'Stage two class', 'KEEP123', 1, 1, '2026-01-01 00:00:00');
                        INSERT INTO enrollment VALUES (1, 1, 2, '2026-01-01 00:00:00');
                        UPDATE assignment SET course_id=1, class_id=1, due_at='2030-01-01 00:00:00';
                    """)
            environment = dict(os.environ, HOMEWORK_DATABASE_URI=f"sqlite:///{database.as_posix()}",
                               HOMEWORK_UPLOAD_FOLDER=str(Path(temporary) / "uploads"))
            script = '''
from pathlib import Path
import sqlite3
from contextlib import closing
from app import app, migrate_final_schema
from models import Assignment, Submission, Course, TeachingClass, Enrollment, User, ReviewEvent, db
with app.app_context():
    migrate_final_schema()
    assert db.session.get(Submission, 1).score == 88
    assert db.session.get(Submission, 1).comment == 'keep this grade'
    assert db.session.get(Submission, 1).review_state == 'GRADED'
    assert db.session.get(User, 2).real_name == ''
    assert db.session.get(User, 2).student_number is None
    assert db.session.get(Assignment, 1).archived is False
    assert ReviewEvent.query.count() == 0
    assert db.session.get(Assignment, 1).class_id == 1
    assert db.session.get(TeachingClass, 1).join_code == 'KEEP123'
    assert Course.query.count() == TeachingClass.query.count() == Enrollment.query.count() == 1
    assert Path(db.engine.url.database + '.stage2.bak').is_file()
    assert Path(db.engine.url.database + '.stage3.bak').is_file()
    with closing(sqlite3.connect(db.engine.url.database + '.stage2.bak')) as backup:
        assert 'review_state' not in {row[1] for row in backup.execute('PRAGMA table_info(submission)')}
        assert backup.execute('SELECT score FROM submission').fetchone()[0] == 88
    assert db.session.execute(db.text('PRAGMA integrity_check')).scalar() == 'ok'
'''
            for _ in range(2):
                result = subprocess.run([sys.executable, "-c", script], env=environment,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)


class StageTwoTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        for username, role in (("teacher", "TEACHER"), ("other-teacher", "TEACHER"),
                               ("student", "STUDENT"), ("outsider", "STUDENT")):
            db.session.add(User(username=username, password=generate_password_hash("password"), role=role))
        db.session.flush()
        self.teacher = User.query.filter_by(username="teacher").one()
        self.other_teacher = User.query.filter_by(username="other-teacher").one()
        self.student = User.query.filter_by(username="student").one()
        self.outsider = User.query.filter_by(username="outsider").one()
        self.course = Course(name="Database", description="SQL", teacher_id=self.teacher.id)
        self.other_course = Course(name="Web", description="HTTP", teacher_id=self.other_teacher.id)
        db.session.add_all([self.course, self.other_course])
        db.session.flush()
        self.teaching_class = TeachingClass(name="Class A", course_id=self.course.id,
            teacher_id=self.teacher.id, join_code="ABCD1234")
        self.other_class = TeachingClass(name="Class B", course_id=self.other_course.id,
            teacher_id=self.other_teacher.id, join_code="EFGH5678")
        db.session.add_all([self.teaching_class, self.other_class])
        db.session.flush()
        db.session.add(Enrollment(class_id=self.teaching_class.id, student_id=self.student.id))
        self.assignment = Assignment(title="SQL task", description="Write SQL", teacher_id=self.teacher.id,
            course_id=self.course.id, class_id=self.teaching_class.id, due_at=beijing_now() + timedelta(days=1))
        self.private_assignment = Assignment(title="Secret web task", description="Private", teacher_id=self.other_teacher.id,
            course_id=self.other_course.id, class_id=self.other_class.id, due_at=beijing_now() + timedelta(days=5))
        db.session.add_all([self.assignment, self.private_assignment])
        db.session.commit()
        self.client = CSRFClient(app, response_wrapper=app.response_class)

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def login(self, username="student"):
        self.client.post("/logout")
        response = self.client.post("/login", data={"username": username, "password": "password"})
        self.assertEqual(response.status_code, 302)
        return response

    def upload(self, assignment=None, filename="homework.txt"):
        return self.client.post("/assignment/submit", data={
            "assignment_id": (assignment or self.assignment).id,
            "file": (io.BytesIO(b"my homework"), filename)}, content_type="multipart/form-data")

    def test_login_redirects_to_dashboard(self):
        response = self.login()
        self.assertTrue(response.location.endswith("/dashboard"))
        for route in ("/dashboard", "/course/list", "/course", "/assignment/list"):
            self.assertEqual(self.client.get(route).status_code, 200)

    def test_teacher_pages_render_and_own_scope(self):
        self.login("teacher")
        for route in ("/dashboard", "/course/list", f"/course/{self.course.id}",
                      f"/class/{self.teaching_class.id}/members", "/assignment/list",
                      f"/assignment/detail/{self.assignment.id}"):
            self.assertEqual(self.client.get(route).status_code, 200, route)
        self.assertNotIn(b"Secret web task", self.client.get("/assignment/list").data)

    def test_course_create_edit_and_class_code(self):
        self.login("teacher")
        self.client.post("/course/create", data={"name": "New course", "description": "Intro"})
        course = Course.query.filter_by(name="New course").one()
        self.assertEqual(course.teacher_id, self.teacher.id)
        self.client.post(f"/course/{course.id}/edit", data={"name": "Updated", "description": "Changed"})
        self.assertEqual(course.name, "Updated")
        self.client.post("/class/create", data={"course_id": course.id, "name": "New class"})
        teaching_class = TeachingClass.query.filter_by(course_id=course.id).one()
        self.assertTrue(teaching_class.join_code)
        self.assertEqual(teaching_class.teacher_id, self.teacher.id)

    def test_join_code_normalization_invalid_and_duplicate(self):
        self.login("outsider")
        before = Enrollment.query.count()
        self.client.post("/class/join", data={"join_code": "wrong"})
        self.assertEqual(Enrollment.query.count(), before)
        self.client.post("/class/join", data={"join_code": " abcd1234 "})
        self.assertEqual(Enrollment.query.count(), before + 1)
        self.client.post("/class/join", data={"join_code": "ABCD1234"})
        self.assertEqual(Enrollment.query.count(), before + 1)

    def test_non_member_cannot_list_view_or_submit(self):
        self.login("outsider")
        self.assertNotIn(b"SQL task", self.client.get("/assignment/list").data)
        self.assertEqual(self.client.get(f"/assignment/detail/{self.assignment.id}").status_code, 403)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(Submission.query.count(), 0)

    def test_student_sees_joined_classes_only(self):
        extra = TeachingClass(name="Unjoined sibling", course_id=self.course.id,
                              teacher_id=self.teacher.id, join_code="SIBLING1")
        db.session.add(extra)
        db.session.commit()
        self.login()
        page = self.client.get(f"/course/{self.course.id}")
        self.assertIn(b"Class A", page.data)
        self.assertNotIn(b"Unjoined sibling", page.data)
        self.assertEqual(self.client.get(f"/course/{self.other_course.id}").status_code, 403)

    def test_cross_teacher_permissions(self):
        self.login("other-teacher")
        routes = [("get", f"/course/{self.course.id}"),
                  ("get", f"/class/{self.teaching_class.id}/members"),
                  ("get", f"/assignment/detail/{self.assignment.id}")]
        for method, route in routes:
            self.assertEqual(getattr(self.client, method)(route).status_code, 403)
        self.assertEqual(self.client.post(f"/course/{self.course.id}/edit", data={"name": "hacked"}).status_code, 403)
        self.assertEqual(self.client.post("/class/create", data={"course_id": self.course.id, "name": "hacked"}).status_code, 403)

    def test_student_cannot_use_teacher_mutations_or_roster(self):
        self.login()
        self.assertEqual(self.client.post("/course/create", data={"name": "hacked"}).status_code, 403)
        self.assertEqual(self.client.post(f"/course/{self.course.id}/edit", data={"name": "hacked"}).status_code, 403)
        self.assertEqual(self.client.get(f"/class/{self.teaching_class.id}/members").status_code, 403)
        self.assertEqual(self.client.post("/assignment/create").status_code, 403)

    def test_assignment_requires_owned_matching_class_and_future_deadline(self):
        self.login("teacher")
        payload = {"title": "New task", "description": "Requirements", "course_id": self.course.id,
                   "class_id": self.teaching_class.id, "due_at": (beijing_now() + timedelta(days=2)).isoformat(timespec="minutes")}
        for field in ("course_id", "class_id", "due_at"):
            invalid = dict(payload, **{field: ""})
            self.client.post("/assignment/create", data=invalid)
        for due_at in ("not-a-date", (beijing_now() - timedelta(days=1)).isoformat(), "2030-01-01T01:00:00+08:00"):
            self.client.post("/assignment/create", data=dict(payload, due_at=due_at))
        self.assertEqual(Assignment.query.count(), 2)
        sibling_course = Course(name="Sibling", teacher_id=self.teacher.id)
        db.session.add(sibling_course)
        db.session.commit()
        self.client.post("/assignment/create", data=dict(payload, course_id=sibling_course.id))
        self.assertEqual(Assignment.query.count(), 2)
        response = self.client.post("/assignment/create", data=dict(payload, class_id=self.other_class.id))
        self.assertEqual(response.status_code, 403)
        self.client.post("/assignment/create", data=payload)
        created = Assignment.query.filter_by(title="New task").one()
        self.assertEqual(created.class_id, self.teaching_class.id)
        self.assertIsNotNone(created.due_at)

    def test_upload_grade_feedback_download_and_statuses(self):
        self.login()
        self.assertEqual(assignment_status(self.assignment, self.student.id), "PENDING")
        self.upload()
        submission = Submission.query.one()
        self.assertEqual(assignment_status(self.assignment, self.student.id), "TO_GRADE")
        self.assertTrue((Path(app.config["UPLOAD_FOLDER"]) / submission.file_path).is_file())
        response = self.client.get(f"/submission/{submission.id}/download")
        self.assertEqual(response.data, b"my homework")
        response.close()
        self.login("teacher")
        self.assertIn(b"SQL task", self.client.get("/dashboard").data)
        self.assertIn(b"SQL task", self.client.get("/assignment/list?status=TO_GRADE").data)
        self.client.post("/assignment/grade", data={"submission_id": submission.id, "score": 0})
        self.assertEqual(assignment_status(self.assignment, self.student.id), "GRADED")
        self.client.post("/assignment/grade", data={"submission_id": submission.id, "score": 95, "comment": "Well done"})
        self.assertEqual(assignment_status(self.assignment, self.student.id), "GRADED")
        self.login()
        page = self.client.get(f"/assignment/detail/{self.assignment.id}")
        self.assertIn(b"95", page.data)
        self.assertIn(b"Well done", page.data)
        self.assertIn(b"SQL task", self.client.get("/dashboard").data)

    def test_deadline_blocks_upload_and_overdue_filter(self):
        self.assignment.due_at = beijing_now() - timedelta(seconds=1)
        db.session.commit()
        self.login()
        self.assertEqual(assignment_status(self.assignment, self.student.id), "OVERDUE")
        self.upload()
        self.assertEqual(Submission.query.count(), 0)
        self.assertIn(b"SQL task", self.client.get("/assignment/list?status=OVERDUE").data)
        self.assertNotIn(b"enctype=", self.client.get(f"/assignment/detail/{self.assignment.id}").data)

    def test_filters_keep_full_course_options_and_enforce_scope(self):
        self.login()
        self.assertIn(b"SQL task", self.client.get(f"/assignment/list?course_id={self.course.id}&q=SQL&status=PENDING").data)
        self.assertNotIn(b"SQL task", self.client.get("/assignment/list?q=nomatch").data)
        self.assertIn(b"Database", self.client.get("/assignment/list?q=nomatch").data)
        self.assertNotIn(b"Secret web task", self.client.get(f"/assignment/list?class_id={self.other_class.id}").data)
        self.assertNotIn(b"SQL task", self.client.get("/assignment/list?status=SUBMITTED").data)
        self.upload()
        self.assertIn(b"SQL task", self.client.get("/assignment/list?status=SUBMITTED").data)

    def test_near_deadline_excludes_submitted_and_far_future(self):
        db.session.add(Assignment(title="Far future", description="Later", teacher_id=self.teacher.id,
            course_id=self.course.id, class_id=self.teaching_class.id, due_at=beijing_now() + timedelta(days=8)))
        db.session.commit()
        self.login()
        page = self.client.get("/dashboard")
        self.assertIn(b"SQL task", page.data)
        self.assertNotIn(b"Far future", page.data)
        self.upload()
        self.assertNotIn(b"SQL task", self.client.get("/dashboard").data)

    def test_download_and_grade_ownership(self):
        self.login()
        self.upload()
        submission = Submission.query.one()
        self.login("outsider")
        self.assertEqual(self.client.get(f"/submission/{submission.id}/download").status_code, 403)
        self.login("other-teacher")
        self.assertEqual(self.client.get(f"/submission/{submission.id}/download").status_code, 403)
        self.assertEqual(self.client.post("/assignment/grade", data={"submission_id": submission.id, "score": 100}).status_code, 403)
        self.login("teacher")
        self.client.post("/assignment/grade", data={"submission_id": submission.id, "score": -1})
        self.assertIsNone(submission.score)

    def test_new_submission_resets_student_status_without_destroying_history(self):
        self.login()
        self.upload()
        first = Submission.query.one()
        first.score = 90
        db.session.commit()
        self.upload()
        self.assertEqual(Submission.query.count(), 2)
        self.assertEqual(first.score, 90)
        self.assertEqual(assignment_status(self.assignment, self.student.id), "TO_GRADE")

    def test_invalid_file_and_unauthenticated_requests(self):
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.login()
        self.upload(filename="payload.exe")
        self.assertEqual(Submission.query.count(), 0)


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"method": attrs.get("method", "get").lower(), "token": None}
            self.forms.append(self.current)
        if tag == "input" and self.current is not None and attrs.get("name") == "csrf_token":
            self.current["token"] = attrs.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class StageThreeTests(unittest.TestCase):
    setUp = StageTwoTests.setUp
    tearDown = StageTwoTests.tearDown
    login = StageTwoTests.login
    upload = StageTwoTests.upload

    def submitted(self):
        self.login()
        self.upload()
        return latest_submission(self.assignment.id, self.student.id)

    def grade(self, submission, score=80):
        self.login("teacher")
        return self.client.post("/assignment/grade", data={"submission_id": submission.id,
                                                          "score": score, "comment": "Good work"})

    def return_work(self, submission, **overrides):
        self.login("teacher")
        payload = {"reason": "Please improve SQL", "redo_due_at": (beijing_now() + timedelta(days=2)).isoformat()}
        payload.update(overrides)
        return self.client.post(f"/submission/{submission.id}/return", data=payload)

    def test_versions_default_grading_and_stale_requests(self):
        first = self.submitted()
        self.login()
        self.upload()
        second = latest_submission(self.assignment.id, self.student.id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(assignment_card(self.assignment)["pending_count"], 1)
        self.assertEqual(assignment_card(self.assignment)["submission_count"], 1)
        self.assertEqual(self.grade(first).status_code, 409)
        self.assertEqual(self.return_work(first).status_code, 409)
        self.assertIsNone(first.score)
        page = self.client.get(f"/assignment/detail/{self.assignment.id}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(f'name="submission_id" value="{first.id}"'.encode(), page.data)
        self.assertIn(f'name="submission_id" value="{second.id}"'.encode(), page.data)
        self.assertEqual(self.grade(second).status_code, 302)
        self.assertEqual(assignment_card(self.assignment)["pending_count"], 0)

    def test_return_after_deadline_resubmit_again_and_grade(self):
        first = self.submitted()
        self.grade(first, 60)
        self.assignment.due_at = beijing_now() - timedelta(days=1)
        db.session.commit()
        self.assertEqual(self.return_work(first).status_code, 302)
        self.assertEqual(assignment_status(self.assignment, self.student.id), "RETURNED")
        self.assertEqual(first.score, 60)
        self.assertEqual(assignment_card(self.assignment)["pending_count"], 0)
        self.assertIsNone(grade_rows(self.teaching_class, [self.assignment])[0]["average"])
        self.assertEqual(self.grade(first).status_code, 409)
        self.login()
        for route in ("/dashboard", "/my-grades", "/assignment/list?status=RETURNED", "/assignment/list?status=SUBMITTED"):
            self.assertIn(b"SQL task", self.client.get(route).data, route)
        page = self.client.get(f"/assignment/detail/{self.assignment.id}")
        self.assertIn(b"Please improve SQL", page.data)
        self.assertIn(b'enctype="multipart/form-data"', page.data)
        self.upload()
        second = latest_submission(self.assignment.id, self.student.id)
        self.assertEqual(second.redo_due_at, first.redo_due_at)
        self.assertEqual(assignment_status(self.assignment, self.student.id), "TO_GRADE")
        self.upload()
        third = latest_submission(self.assignment.id, self.student.id)
        self.assertNotEqual(third.id, second.id)
        self.grade(third, 95)
        self.assertEqual(assignment_status(self.assignment, self.student.id), "GRADED")
        self.assertEqual(grade_rows(self.teaching_class, [self.assignment])[0]["average"], 95)
        self.assertEqual(first.return_reason, "Please improve SQL")

    def test_redo_expired_and_validation(self):
        submission = self.submitted()
        for payload in ({"reason": ""}, {"reason": "x" * 2001}, {"redo_due_at": "bad"},
                        {"redo_due_at": (beijing_now() - timedelta(days=1)).isoformat()},
                        {"redo_due_at": "2030-01-01T12:00+08:00"}):
            self.return_work(submission, **payload)
            self.assertNotEqual(submission.review_state, "RETURNED")
        self.return_work(submission)
        self.assertEqual(self.return_work(submission).status_code, 409)
        self.assignment.due_at = beijing_now() - timedelta(days=2)
        submission.redo_due_at = beijing_now() - timedelta(seconds=1)
        db.session.commit()
        self.login()
        self.upload()
        self.assertEqual(Submission.query.count(), 1)
        self.assertNotIn(b'enctype="multipart/form-data"', self.client.get(f"/assignment/detail/{self.assignment.id}").data)

    def test_return_does_not_shorten_original_deadline_or_open_for_other_students(self):
        submission = self.submitted()
        self.assignment.due_at = beijing_now() + timedelta(days=10)
        db.session.commit()
        self.return_work(submission, redo_due_at=(beijing_now() + timedelta(days=1)).isoformat())
        from workflow import submission_deadline
        self.assertEqual(submission_deadline(self.assignment, submission), self.assignment.due_at)
        db.session.add(Enrollment(class_id=self.teaching_class.id, student_id=self.outsider.id))
        self.assignment.due_at = beijing_now() - timedelta(seconds=1)
        db.session.commit()
        self.login("outsider")
        self.upload()
        self.assertEqual(Submission.query.count(), 1)

    def test_gradebook_statistics_effective_versions_zero_and_missing(self):
        submission = self.submitted()
        self.grade(submission, 100)
        self.login()
        self.upload()
        current = latest_submission(self.assignment.id, self.student.id)
        self.grade(current, 0)
        db.session.add(Enrollment(class_id=self.teaching_class.id, student_id=self.outsider.id))
        db.session.commit()
        rows = grade_rows(self.teaching_class, [self.assignment])
        stat = assignment_statistics(rows, self.assignment)
        self.assertEqual((stat["members"], stat["submitted"], stat["missing"], stat["graded"]), (2, 1, 1, 1))
        self.assertEqual((stat["rate"], stat["average"], stat["highest"], stat["lowest"]), (50, 0, 0, 0))
        self.assertEqual([b["count"] for b in stat["distribution"]], [1, 0, 0, 0, 0])
        page = self.client.get(f"/gradebook?course_id={self.course.id}&class_id={self.teaching_class.id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"50.0%", page.data)
        self.assertNotIn(b"Secret web task", page.data)
        self.return_work(current)
        stat = assignment_statistics(grade_rows(self.teaching_class, [self.assignment]), self.assignment)
        self.assertEqual(stat["returned"], 1)
        self.assertIsNone(stat["average"])

    def test_csv_all_or_single_assignment_utf8_and_formula_escape(self):
        self.student.username = "=HYPERLINK(1)"
        self.course.name = "+danger"
        self.assignment.title = "@unsafe,\"title\""
        db.session.commit()
        self.login("=HYPERLINK(1)")
        self.upload()
        current = latest_submission(self.assignment.id, self.student.id)
        self.grade(current, 0)
        for suffix in ("", f"?assignment_id={self.assignment.id}"):
            response = self.client.get(f"/class/{self.teaching_class.id}/grades.csv{suffix}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
            self.assertTrue(rows[0][3].startswith("'@"))
            self.assertEqual(rows[1][0], "'=HYPERLINK(1)")
            self.assertEqual(rows[1][1], "'+danger")
            self.assertEqual(rows[1][4], "0")
            self.assertEqual(rows[1][-1], "0.0")
            self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_reports_exports_returns_and_student_grades_permissions(self):
        submission = self.submitted()
        for username in ("student", "outsider", "other-teacher"):
            self.login(username)
            for route in (f"/class/{self.teaching_class.id}/grades.csv", f"/gradebook?class_id={self.teaching_class.id}"):
                self.assertEqual(self.client.get(route).status_code, 403)
            self.assertEqual(self.client.post(f"/submission/{submission.id}/return", data={"reason": "hack"}).status_code, 403)
        self.login("teacher")
        for route in (f"/gradebook?course_id={self.other_course.id}",
                      f"/gradebook?class_id={self.teaching_class.id}&assignment_id={self.private_assignment.id}",
                      f"/class/{self.teaching_class.id}/grades.csv?assignment_id={self.private_assignment.id}", "/my-grades"):
            self.assertEqual(self.client.get(route).status_code, 403)
        self.login()
        self.assertEqual(self.client.get(f"/my-grades?course_id={self.other_course.id}").status_code, 403)

    def test_personal_average_and_grade_trend_only_current_grades(self):
        first = self.submitted()
        self.grade(first, 0)
        other = Assignment(title="Second task", description="SQL", course_id=self.course.id,
                           class_id=self.teaching_class.id, teacher_id=self.teacher.id)
        db.session.add(other)
        db.session.commit()
        self.login()
        self.upload(assignment=other)
        second = latest_submission(other.id, self.student.id)
        self.grade(second, 100)
        self.login()
        page = self.client.get("/my-grades")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"50.0", page.data)
        self.assertIn(b"+100", page.data)
        self.assertIn(b"Good work", page.data)
        self.upload()
        page = self.client.get("/my-grades")
        self.assertIn(b"100.0", page.data)
        self.assertNotIn(b"+100", page.data)

    def test_notification_publication_join_grading_return_and_read_scope(self):
        self.login("teacher")
        self.client.post("/assignment/create", data={"title": "Notice task", "description": "SQL",
            "course_id": self.course.id, "class_id": self.teaching_class.id,
            "due_at": (beijing_now() + timedelta(days=1)).isoformat()})
        assignment = Assignment.query.filter_by(title="Notice task").one()
        notice = Notification.query.filter_by(assignment_id=assignment.id).one()
        self.assertEqual(notice.user_id, self.student.id)
        self.login("outsider")
        self.assertNotIn(b"Notice task", self.client.get("/notifications").data)
        self.assertEqual(self.client.post(f"/notifications/{notice.id}/read").status_code, 403)
        self.client.post("/class/join", data={"join_code": self.teaching_class.join_code})
        self.assertEqual(Notification.query.filter_by(user_id=self.outsider.id).count(), 2)
        self.login()
        self.assertIn(b"Notice task", self.client.get("/notifications?unread=1").data)
        self.assertFalse(notice.is_read)
        self.client.post(f"/notifications/{notice.id}/read")
        self.assertTrue(notice.is_read)
        self.upload()
        submission = latest_submission(self.assignment.id, self.student.id)
        self.grade(submission, 80)
        self.return_work(submission)
        self.assertEqual(Notification.query.filter_by(user_id=self.student.id, is_read=False).count(), 2)
        self.login()
        self.client.post("/notifications/read-all")
        self.assertEqual(Notification.query.filter_by(user_id=self.student.id, is_read=False).count(), 0)
        self.assertEqual(Notification.query.filter_by(user_id=self.outsider.id, is_read=False).count(), 2)

    def test_notification_pagination(self):
        for i in range(25):
            db.session.add(Notification(user_id=self.student.id, assignment_id=self.assignment.id,
                                        title=f"Message {i:02}", message="Feedback"))
        db.session.commit()
        self.login()
        self.assertIn(b"Message 24", self.client.get("/notifications").data)
        self.assertNotIn(b"Message 00", self.client.get("/notifications").data)
        self.assertIn(b"Message 00", self.client.get("/notifications?page=2").data)

    def test_csrf_missing_wrong_foreign_and_unicode_tokens_rejected(self):
        raw = app.test_client()
        self.assertEqual(raw.post("/login", data={"username": "student", "password": "password"}).status_code, 400)
        page = raw.get("/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        for wrong in ("wrong", "无效令牌", ""):
            self.assertEqual(raw.post("/login", data={"username": "student", "password": "password", "csrf_token": wrong}).status_code, 400)
        foreign = app.test_client()
        self.assertEqual(foreign.post("/login", data={"csrf_token": token}).status_code, 400)
        self.assertEqual(raw.post("/login", data={"username": "student", "password": "password", "csrf_token": token}).status_code, 302)
        self.assertEqual(raw.post("/class/join", data={"join_code": "ABCD1234", "csrf_token": token}).status_code, 400)
        self.assertEqual(raw.get("/logout").status_code, 405)
        self.assertEqual(raw.get("/dashboard").status_code, 200)
        self.assertEqual(raw.post("/logout").status_code, 400)

    def test_all_rendered_mutation_forms_have_tokens(self):
        submission = self.submitted()
        for username, routes in (("student", ["/dashboard", "/course/list", "/my-grades", "/notifications", f"/assignment/detail/{self.assignment.id}"]),
                                 ("teacher", ["/dashboard", "/assignment/list", "/gradebook", "/notifications", f"/course/{self.course.id}", f"/assignment/detail/{self.assignment.id}"])):
            self.login(username)
            for route in routes:
                page = self.client.get(route)
                self.assertEqual(page.status_code, 200, route)
                parser = FormParser()
                parser.feed(page.get_data(as_text=True))
                for form in parser.forms:
                    if form["method"] == "post":
                        self.assertTrue(form["token"], route)

    def test_bad_empty_disguised_and_oversized_attachments(self):
        self.login()
        for content, name in ((b"", "empty.txt"), (b"MZexecutable", "fake.txt"),
                              (b"plain text", "fake.pdf"), (b"plain text", "fake.png"),
                              (b"plain text", "fake.docx"), (b"hello\x00world", "binary.py")):
            self.client.post("/assignment/submit", data={"assignment_id": self.assignment.id,
                "file": (io.BytesIO(content), name)}, content_type="multipart/form-data")
            self.assertEqual(Submission.query.count(), 0, name)
        response = self.client.post("/assignment/submit", data={"assignment_id": self.assignment.id,
            "file": (io.BytesIO(b"x" * (20 * 1024 * 1024 + 1)), "large.txt")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 413)
        response.request.environ["wsgi.input"].close()
        response.request.close()
        response.close()
        self.assertNotIn(b"Internal Server Error", response.data)
        self.assertEqual(Submission.query.count(), 0)
        self.assertEqual(list(Path(app.config["UPLOAD_FOLDER"]).glob("*large*")), [])

    def test_xss_escaped_errors_and_security_headers(self):
        self.assignment.title = '<script>alert("xss")</script>'
        db.session.commit()
        self.login()
        page = self.client.get(f"/assignment/detail/{self.assignment.id}")
        self.assertIn(b"&lt;script&gt;", page.data)
        self.assertNotIn(b'<script>alert(', page.data)
        for route, code in (("/missing-page", 404), (f"/assignment/detail/{self.private_assignment.id}", 403)):
            response = self.client.get(route)
            self.assertEqual(response.status_code, code)
            self.assertIn(str(code).encode(), response.data)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_failed_upload_rolls_back_file_and_database(self):
        self.login()
        before = set(Path(app.config["UPLOAD_FOLDER"]).iterdir())
        with patch.object(db.session, "commit", side_effect=RuntimeError("simulated database failure")):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertEqual(Submission.query.count(), 0)
        self.assertEqual(set(Path(app.config["UPLOAD_FOLDER"]).iterdir()), before)

    def test_empty_reports_and_distributions(self):
        self.login("teacher")
        self.assertEqual(self.client.get("/gradebook").status_code, 200)
        stat = assignment_statistics(grade_rows(self.teaching_class, [self.assignment]), self.assignment)
        self.assertEqual(stat["rate"], 0)
        self.assertIsNone(stat["average"])
        self.assertEqual(sum(b["count"] for b in stat["distribution"]), 0)
        self.login("other-teacher")
        self.assertEqual(self.client.get("/gradebook").status_code, 200)
        self.login("outsider")
        self.assertEqual(self.client.get("/my-grades").status_code, 200)


class RefinementTests(unittest.TestCase):
    setUp = StageTwoTests.setUp
    tearDown = StageTwoTests.tearDown
    login = StageTwoTests.login
    upload = StageTwoTests.upload
    submitted = StageThreeTests.submitted
    grade = StageThreeTests.grade
    return_work = StageThreeTests.return_work

    def test_expired_redo_can_extend_and_resubmit_with_audit(self):
        submission = self.submitted()
        self.return_work(submission)
        old_deadline = beijing_now() - timedelta(days=1)
        submission.redo_due_at = old_deadline
        self.assignment.due_at = old_deadline
        db.session.commit()
        self.login()
        self.upload()
        self.assertEqual(Submission.query.count(), 1)
        self.login("teacher")
        new_deadline = beijing_now() + timedelta(days=1)
        response = self.client.post(f"/submission/{submission.id}/extend", data={
            "reason": "Student requested more time", "redo_due_at": new_deadline.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(submission.redo_due_at, new_deadline)
        audit = ReviewEvent.query.filter_by(action="EXTEND").one()
        self.assertEqual((audit.old_deadline, audit.new_deadline), (old_deadline, new_deadline))
        self.assertEqual(audit.teacher_id, self.teacher.id)
        self.assertIn("more time", audit.reason)
        self.assertTrue(Notification.query.filter_by(user_id=self.student.id).count())
        self.login()
        self.upload()
        self.assertEqual(Submission.query.count(), 2)

    def test_extension_validation_permissions_and_stale_version(self):
        submission = self.submitted()
        self.return_work(submission)
        old_deadline = submission.redo_due_at
        for payload in ({"reason": ""}, {"redo_due_at": "bad"},
                        {"redo_due_at": old_deadline.isoformat()},
                        {"redo_due_at": "2030-01-01T00:00+08:00"}):
            data = {"reason": "extend", "redo_due_at": (old_deadline + timedelta(days=1)).isoformat()}
            data.update(payload)
            self.client.post(f"/submission/{submission.id}/extend", data=data)
            self.assertEqual(submission.redo_due_at, old_deadline)
        for user in ("student", "other-teacher"):
            self.login(user)
            self.assertEqual(self.client.post(f"/submission/{submission.id}/extend").status_code, 403)
        self.login()
        self.upload()
        self.login("teacher")
        self.assertEqual(self.client.post(f"/submission/{submission.id}/extend").status_code, 409)

    def test_edit_assignment_validation_notification_and_ownership(self):
        self.login("teacher")
        deadline = beijing_now() + timedelta(days=5)
        payload = {"title": "Updated SQL task", "description": "Updated requirements", "due_at": deadline.isoformat()}
        self.client.post(f"/assignment/{self.assignment.id}/edit", data=payload)
        self.assertEqual(self.assignment.title, "Updated SQL task")
        self.assertEqual(self.assignment.due_at, deadline)
        self.assertEqual(self.assignment.class_id, self.teaching_class.id)
        self.assertEqual(Notification.query.filter_by(user_id=self.student.id).count(), 1)
        for invalid in ({"title": ""}, {"description": "x" * 20001}, {"due_at": ""},
                        {"due_at": "bad"}, {"due_at": (beijing_now() - timedelta(days=1)).isoformat()}):
            self.client.post(f"/assignment/{self.assignment.id}/edit", data=dict(payload, **invalid))
            self.assertEqual(self.assignment.due_at, deadline)
            self.assertEqual(self.assignment.title, payload["title"])
        for username in ("student", "other-teacher"):
            self.login(username)
            self.assertEqual(self.client.post(f"/assignment/{self.assignment.id}/edit", data=payload).status_code, 403)

    def test_archive_preserves_grades_downloads_and_restore(self):
        submission = self.submitted()
        self.grade(submission, 90)
        self.client.post(f"/assignment/{self.assignment.id}/archive", data={"action": "archive"})
        self.assertTrue(self.assignment.archived)
        self.assertNotIn(b"SQL task", self.client.get("/assignment/list").data)
        self.assertIn(b"SQL task", self.client.get("/assignment/list?archived=1").data)
        self.assertNotIn(b"SQL task", self.client.get("/dashboard").data)
        self.assertEqual(self.grade(submission).status_code, 409)
        self.assertEqual(self.return_work(submission).status_code, 409)
        self.assertEqual(self.client.post(f"/assignment/{self.assignment.id}/edit").status_code, 409)
        self.login()
        self.upload()
        self.assertEqual(Submission.query.count(), 1)
        self.assertIn(b"SQL task", self.client.get("/my-grades").data)
        response = self.client.get(f"/submission/{submission.id}/download")
        self.assertEqual(response.data, b"my homework")
        response.close()
        self.assertEqual(self.client.post(f"/assignment/{self.assignment.id}/archive", data={"action": "restore"}).status_code, 403)
        self.login("teacher")
        self.assertEqual(self.client.post(f"/assignment/{self.assignment.id}/archive", data={"action": "delete"}).status_code, 400)
        self.client.post(f"/assignment/{self.assignment.id}/archive", data={"action": "restore"})
        self.assertFalse(self.assignment.archived)
        self.assertEqual(submission.score, 90)
        self.assertIn(b"SQL task", self.client.get("/assignment/list").data)

    def test_grade_history_keeps_previous_scores_comments_and_no_fake_legacy_events(self):
        submission = self.submitted()
        self.grade(submission, 0)
        self.client.post("/assignment/grade", data={"submission_id": submission.id, "score": 95, "comment": "Rechecked"})
        events = ReviewEvent.query.filter_by(submission_id=submission.id).order_by(ReviewEvent.id).all()
        self.assertEqual(len(events), 2)
        self.assertIsNone(events[0].old_score)
        self.assertEqual((events[1].old_score, events[1].new_score), (0, 95))
        self.assertEqual((events[1].old_comment, events[1].new_comment), ("Good work", "Rechecked"))
        self.assertEqual(events[1].teacher_id, self.teacher.id)
        self.login()
        page = self.client.get(f"/assignment/detail/{self.assignment.id}")
        self.assertIn(b"Rechecked", page.data)
        self.assertIn(b"Good work", page.data)
        self.assertEqual(self.client.get(f"/assignment/detail/{self.private_assignment.id}").status_code, 403)

    def test_profile_identity_unique_student_number_and_teacher(self):
        self.login()
        payload = {"real_name": "Test Student", "student_number": "20260001"}
        self.assertEqual(self.client.post("/profile", data=payload).status_code, 302)
        self.assertEqual((self.student.real_name, self.student.student_number), ("Test Student", "20260001"))
        self.login("outsider")
        self.assertEqual(self.client.post("/profile", data=payload).status_code, 400)
        self.assertIsNone(self.outsider.student_number)
        for invalid in ({"real_name": ""}, {"student_number": "<script>"}, {"real_name": "a" * 41}):
            self.assertEqual(self.client.post("/profile", data=dict(payload, **invalid)).status_code, 400)
        self.login("teacher")
        self.client.post("/profile", data={"real_name": "Test Teacher", "student_number": "ignored"})
        self.assertIsNone(self.teacher.student_number)
        self.assertEqual(self.teacher.real_name, "Test Teacher")

    def test_registration_identity_required_and_account_routes(self):
        payload = {"username": "new-student", "password": "password", "role": "STUDENT"}
        self.assertEqual(self.client.post("/register", data=payload).status_code, 400)
        payload.update(real_name="New Student", student_number="20260002")
        self.assertEqual(self.client.post("/register", data=payload).status_code, 302)
        created = User.query.filter_by(username="new-student").one()
        self.assertEqual(created.student_number, "20260002")
        self.assertEqual(self.client.post("/register", data=dict(payload, username="duplicate-id")).status_code, 400)
        self.login("new-student")
        self.assertEqual(self.client.get("/profile").status_code, 200)

    def test_identity_shown_in_roster_gradebook_and_csv(self):
        self.student.real_name = "Student Name"
        self.student.student_number = "20261234"
        db.session.commit()
        self.login("teacher")
        for route in (f"/class/{self.teaching_class.id}/members", "/gradebook"):
            response = self.client.get(route)
            self.assertIn(b"Student Name", response.data)
            self.assertIn(b"20261234", response.data)
        response = self.client.get(f"/class/{self.teaching_class.id}/grades.csv")
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertIn("Student Name", rows[1])
        self.assertIn("20261234", rows[1])

    def test_pagination_filters_before_count_and_preserves_scope(self):
        for i in range(25):
            db.session.add(Assignment(title=f"Pagination {i:02}", description="Searchable", teacher_id=self.teacher.id,
                course_id=self.course.id, class_id=self.teaching_class.id, due_at=beijing_now() + timedelta(days=2)))
        db.session.commit()
        self.login()
        page = self.client.get(f"/assignment/list?q=Pagination&status=PENDING&course_id={self.course.id}")
        self.assertIn(b"Pagination 00", page.data)
        self.assertNotIn(b"Pagination 24", page.data)
        self.assertIn(b"page=2", page.data)
        self.assertIn(b"status=PENDING", page.data)
        last = self.client.get("/assignment/list?q=Pagination&status=PENDING&page=3")
        self.assertIn(b"Pagination 24", last.data)
        self.assertNotIn(b"Pagination 00", last.data)
        self.assertNotIn(b"Secret web task", last.data)
        self.assertNotIn(b"Pagination", self.client.get("/assignment/list?q=Pagination&status=GRADED").data.split(b'<main>')[-1].split(b'</form>')[-1])
        self.login("outsider")
        self.assertNotIn(b"Pagination 00", self.client.get("/assignment/list?q=Pagination").data)

    def test_batch_cards_query_count_does_not_grow_per_assignment(self):
        from sqlalchemy import event
        from workflow import assignment_cards
        for i in range(20):
            db.session.add(Assignment(title=f"Batch {i}", description="SQL", teacher_id=self.teacher.id,
                course_id=self.course.id, class_id=self.teaching_class.id))
        db.session.commit()
        assignments = Assignment.query.all()
        # Load attributes before measuring the effective-submission query itself.
        for assignment in assignments:
            _ = assignment.due_at
        student_id = self.student.id
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            cards = assignment_cards(assignments, student_id)
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        self.assertEqual(len(cards), len(assignments))
        self.assertEqual(len(statements), 1)


if __name__ == "__main__":
    unittest.main()
