"""Create a fresh, isolated demonstration database for browser acceptance."""
import json
import os
from pathlib import Path
import sys
from datetime import timedelta

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
target = Path(sys.argv[1]).resolve()
if root / "artifacts" not in target.parents:
    raise SystemExit("Browser fixture must be inside project artifacts.")
target.mkdir(parents=True, exist_ok=True)
database = target / "demo.db"
if database.exists():
    raise SystemExit("Use a new directory for each run; existing data is never overwritten.")
os.environ["HOMEWORK_DATABASE_URI"] = f"sqlite:///{database.as_posix()}"
os.environ["HOMEWORK_UPLOAD_FOLDER"] = str(target / "uploads")

from app import app
from models import Assignment, Course, Enrollment, Submission, TeachingClass, User, beijing_now, db
from werkzeug.security import generate_password_hash

with app.app_context():
    teacher = User(username="demo-teacher", real_name="刘老师", role="TEACHER", password=generate_password_hash("DemoPass123"))
    student = User(username="demo-student", real_name="林同学", student_number="20260001", role="STUDENT", password=generate_password_hash("DemoPass123"))
    other = User(username="demo-missing", real_name="陈同学", student_number="20260002", role="STUDENT", password=generate_password_hash("DemoPass123"))
    db.session.add_all([teacher, student, other])
    db.session.flush()
    course = Course(name="大数据 Web 应用开发", description="Flask 应用开发、数据分析与课程实践", teacher_id=teacher.id)
    db.session.add(course)
    db.session.flush()
    teaching_class = TeachingClass(name="2026 级 1 班", join_code="DEMO2026", course_id=course.id, teacher_id=teacher.id)
    db.session.add(teaching_class)
    db.session.flush()
    db.session.add_all([Enrollment(class_id=teaching_class.id, student_id=user.id) for user in (student, other)])
    expired = Assignment(title="数据清洗实验 · 待延期演示", description="请清洗数据并说明缺失值处理方法。", course_id=course.id,
                         class_id=teaching_class.id, teacher_id=teacher.id, due_at=beijing_now() - timedelta(days=3))
    db.session.add(expired)
    db.session.flush()
    (target / "uploads" / "original.txt").write_text("Original homework", encoding="utf-8")
    db.session.add(Submission(assignment_id=expired.id, student_id=student.id, file_path="original.txt", original_name="数据清洗实验.txt",
                             review_state="RETURNED", return_reason="请补充缺失值处理说明。", redo_due_at=beijing_now() - timedelta(days=1)))
    for i in range(13):
        db.session.add(Assignment(title=f"分页演示任务 {i + 1:02}", description="用于验证搜索筛选与分页。", course_id=course.id,
            class_id=teaching_class.id, teacher_id=teacher.id, due_at=beijing_now() + timedelta(days=30)))
    db.session.commit()
    print(json.dumps({"course_id": course.id, "class_id": teaching_class.id, "expired_id": expired.id}))
