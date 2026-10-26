"""Stage-two integration tests. All data and files live in a temporary directory."""
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta


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

from app import app, assignment_status, migrate_legacy_schema, student_can_view
from models import Assignment, Course, Enrollment, Submission, TeachingClass, User, beijing_now, db
from werkzeug.security import generate_password_hash

app.config.update(TESTING=True)


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
            migrate_legacy_schema()
            self.assertEqual(Course.query.count(), 1)
            self.assertEqual(TeachingClass.query.count(), 1)
            self.assertEqual(Enrollment.query.count(), 1)
        with closing(sqlite3.connect(str(_database) + ".stage1.bak")) as backup:
            columns = {row[1] for row in backup.execute("PRAGMA table_info(assignment)")}
            self.assertNotIn("class_id", columns)
            self.assertEqual(backup.execute("SELECT score FROM submission").fetchone()[0], 88)


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
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def login(self, username="student"):
        self.client.get("/logout")
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


if __name__ == "__main__":
    unittest.main()
