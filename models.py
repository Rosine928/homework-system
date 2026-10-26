from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()


def beijing_now():
    """Store stage-two business times consistently as naive Beijing time."""
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)


class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=beijing_now)

    teacher = db.relationship("User", backref=db.backref("courses", lazy=True))
    classes = db.relationship("TeachingClass", back_populates="course", cascade="all, delete-orphan")
    assignments = db.relationship("Assignment", back_populates="course")


class TeachingClass(db.Model):
    __tablename__ = "teaching_class"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    join_code = db.Column(db.String(20), unique=True, nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=beijing_now)

    course = db.relationship("Course", back_populates="classes")
    teacher = db.relationship("User", backref=db.backref("teaching_classes", lazy=True))
    enrollments = db.relationship("Enrollment", back_populates="teaching_class", cascade="all, delete-orphan")
    assignments = db.relationship("Assignment", back_populates="teaching_class")


class Enrollment(db.Model):
    __tablename__ = "enrollment"
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("teaching_class.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    joined_at = db.Column(db.DateTime, nullable=False, default=beijing_now)
    __table_args__ = (UniqueConstraint("class_id", "student_id", name="uq_enrollment_class_student"),)

    teaching_class = db.relationship("TeachingClass", back_populates="enrollments")
    student = db.relationship("User", backref=db.backref("enrollments", lazy=True))


class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # Nullable keeps assignments created in stage one working after the stage two migration.
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey("teaching_class.id"), nullable=True)
    due_at = db.Column(db.DateTime, nullable=True)

    teacher = db.relationship("User", backref="assignments")
    course = db.relationship("Course", back_populates="assignments")
    teaching_class = db.relationship("TeachingClass", back_populates="assignments")
    submissions = db.relationship("Submission", backref="assignment", cascade="all, delete-orphan")


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    file_path = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    score = db.Column(db.Integer)
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    student = db.relationship("User", backref="submissions")
