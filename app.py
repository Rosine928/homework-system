import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import LoginManager, current_user, login_required
from sqlalchemy import inspect, or_, text, event
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import joinedload, selectinload, aliased

from models import Assignment, Course, Enrollment, Submission, TeachingClass, User, beijing_now, db
from models import Notification, ReviewEvent
from security import csrf_token, protect_csrf, secret_key, valid_attachment
from migrations import backup_before_upgrade, upgrade, finish_upgrade, upgrade_email_schema
from workflow import (assignment_status, can_submit, effective_submissions, latest_submission,
                      notify, notify_assignment, submission_deadline, assignment_cards,
                      filter_assignment_status, record_review)
from workflow import teaching_open

BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt", "zip", "rar", "py", "java", "c", "cpp", "ppt", "pptx", "xls", "xlsx", "jpg", "jpeg", "png"}
STATUS_LABELS = {"PENDING": "待提交", "OVERDUE": "已逾期", "TO_GRADE": "待批改", "GRADED": "已评分", "RETURNED": "退回重做"}

app = Flask(__name__)
from operations import configure_logging
configure_logging(app)
app.config.update(
    SECRET_KEY=secret_key(app.instance_path),
    SQLALCHEMY_DATABASE_URI=os.environ.get("HOMEWORK_DATABASE_URI", f"sqlite:///{BASE_DIR / 'homework.db'}"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.environ.get("HOMEWORK_UPLOAD_FOLDER", str(BASE_DIR / "uploads")),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HOMEWORK_HTTPS", "0") == "1",
    LOGIN_MAX_FAILURES=5,
    LOGIN_LOCK_MINUTES=15,
    ALLOWED_EXTENSIONS=ALLOWED_EXTENSIONS,
)
app.before_request(protect_csrf)
from email_recovery import configure_email
configure_email(app)
app.jinja_env.globals["csrf_token"] = csrf_token
Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
db.init_app(app)

with app.app_context():
    if db.engine.dialect.name == "sqlite":
        @event.listens_for(db.engine, "connect")
        def sqlite_constraints(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")


@app.before_request
def serialize_sqlite_writes():
    # Serialize check-and-write operations, including uploads versus grading.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


@app.teardown_request
def release_request_transaction(error):
    # GETs and rejected/no-op mutations must release their read/write locks too.
    db.session.rollback()

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录后继续。"
login_manager.login_message_category = "error"


@login_manager.user_loader
def load_user(user_id):
    try:
        parts = user_id.split(":")
        user = db.session.get(User, int(parts[0]))
        version = int(parts[1]) if len(parts) == 2 else 1
        return user if user and user.enabled and user.auth_version == version else None
    except (ValueError, TypeError):
        return None


def migrate_final_schema():
    columns = {column["name"] for column in inspect(db.engine).get_columns("submission")}
    for name, declaration in (("review_state", "VARCHAR(20) NOT NULL DEFAULT 'PENDING'"),
                              ("return_reason", "TEXT"), ("redo_due_at", "DATETIME"),
                              ("reviewed_at", "DATETIME")):
        if name not in columns:
            db.session.execute(text(f"ALTER TABLE submission ADD COLUMN {name} {declaration}"))
    if "review_state" not in columns:
        db.session.execute(text("UPDATE submission SET review_state = 'GRADED' WHERE score IS NOT NULL"))
    db.session.commit()


def migrate_polish_schema():
    additions = {
        "user": (("real_name", "VARCHAR(40) NOT NULL DEFAULT ''"), ("student_number", "VARCHAR(32)")),
        "assignment": (("archived", "BOOLEAN NOT NULL DEFAULT 0"),),
    }
    for table, fields in additions.items():
        columns = {column["name"] for column in inspect(db.engine).get_columns(table)}
        for name, declaration in fields:
            if name not in columns:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {declaration}'))
    db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_student_number ON user (student_number)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_submission_assignment_student_id ON submission (assignment_id, student_id, id)"))
    db.session.commit()


def migrate_legacy_schema():
    """Add stage-two columns without destroying the stage-one SQLite database."""
    inspector = inspect(db.engine)
    assignment_columns = {column["name"] for column in inspector.get_columns("assignment")}
    for name, declaration in (("course_id", "INTEGER"), ("class_id", "INTEGER"), ("due_at", "DATETIME")):
        if name not in assignment_columns:
            db.session.execute(text(f"ALTER TABLE assignment ADD COLUMN {name} {declaration}"))
    db.session.commit()
    # Old assignments become private class assignments; only their former submitters
    # are enrolled automatically. Other students can join using the generated code.
    legacy = Assignment.query.filter(Assignment.class_id.is_(None)).all()
    class_by_teacher = {}
    for assignment in legacy:
        if assignment.teacher_id not in class_by_teacher:
            course = Course(name="第一阶段历史课程", description="由第一阶段数据自动归档，可修改课程信息。",
                            teacher_id=assignment.teacher_id)
            db.session.add(course)
            db.session.flush()
            teaching_class = TeachingClass(name="第一阶段历史班级", course_id=course.id,
                                           teacher_id=assignment.teacher_id, join_code=uuid4().hex[:8].upper())
            db.session.add(teaching_class)
            db.session.flush()
            class_by_teacher[assignment.teacher_id] = teaching_class
        teaching_class = class_by_teacher[assignment.teacher_id]
        assignment.course_id = teaching_class.course_id
        assignment.class_id = teaching_class.id
        for submission in assignment.submissions:
            if not Enrollment.query.filter_by(class_id=teaching_class.id, student_id=submission.student_id).first():
                db.session.add(Enrollment(class_id=teaching_class.id, student_id=submission.student_id))
    db.session.commit()


with app.app_context():
    backup_before_upgrade()
    # Back up the existing SQLite file before the first schema change.
    database_path = db.engine.url.database
    if db.engine.dialect.name == "sqlite" and database_path and Path(database_path).is_file():
        with closing(sqlite3.connect(database_path)) as source:
            columns = {row[1] for row in source.execute("PRAGMA table_info(assignment)")}
            if columns and "class_id" not in columns:
                backup_path = Path(database_path).with_name(Path(database_path).name + ".stage1.bak")
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as target:
                        source.backup(target)
            submission_columns = {row[1] for row in source.execute("PRAGMA table_info(submission)")}
            if submission_columns and "review_state" not in submission_columns:
                backup_path = Path(database_path).with_name(Path(database_path).name + ".stage2.bak")
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as target:
                        source.backup(target)
            user_columns = {row[1] for row in source.execute("PRAGMA table_info(user)")}
            if user_columns and ("real_name" not in user_columns or "archived" not in columns):
                backup_path = Path(database_path).with_name(Path(database_path).name + ".stage3.bak")
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as target:
                        source.backup(target)
    db.create_all()
    migrate_final_schema()
    migrate_polish_schema()
    upgrade()
    upgrade_email_schema()
    migrate_legacy_schema()
    finish_upgrade()


@app.context_processor
def inject_helpers():
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count() if current_user.is_authenticated else 0
    return {"now": beijing_now, "status_labels": STATUS_LABELS, "unread_count": unread}


def parse_due_at(raw):
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(raw)
        return result if result.tzinfo is None else "invalid"
    except ValueError:
        return "invalid"


def is_enrolled(student_id, class_id):
    return db.session.query(Enrollment.id).filter_by(student_id=student_id, class_id=class_id, active=True).first() is not None


def student_can_view(assignment, student_id):
    return assignment.class_id is not None and is_enrolled(student_id, assignment.class_id)


def assignment_card(assignment, student_id=None):
    return assignment_cards([assignment], student_id)[0]


def teacher_course(course_id):
    course = db.get_or_404(Course, course_id)
    if current_user.role != "TEACHER" or course.teacher_id != current_user.id:
        abort(403)
    return course


def teacher_class(class_id):
    teaching_class = db.get_or_404(TeachingClass, class_id)
    if current_user.role != "TEACHER" or teaching_class.teacher_id != current_user.id:
        abort(403)
    return teaching_class


@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user.is_authenticated else "login"))


from accounts import register_account_routes
register_account_routes(app)
from email_recovery import register_email_routes
register_email_routes(app)


@app.route("/dashboard")
@login_required
def dashboard():
    active_assignments = Assignment.query.join(Course, Course.id == Assignment.course_id).join(
        TeachingClass, TeachingClass.id == Assignment.class_id).filter(Assignment.archived.is_(False),
        Course.archived.is_(False), TeachingClass.archived.is_(False))
    if current_user.role == "TEACHER":
        courses = Course.query.options(selectinload(Course.classes)).filter_by(teacher_id=current_user.id).all()
        classes = TeachingClass.query.filter_by(teacher_id=current_user.id).all()
        assignments = active_assignments.filter(Assignment.teacher_id == current_user.id).all()
        pending_cards = assignment_cards(assignments)
        pending_cards = [c for c in pending_cards if c["pending_count"]]
        pending_grades = sum(c["pending_count"] for c in pending_cards)
        return render_template("dashboard.html", courses=courses, classes=classes, assignments=assignments,
                               pending_grades=pending_grades,
                               pending_cards=pending_cards)
    enrollments = Enrollment.query.filter_by(student_id=current_user.id, active=True).all()
    enrolled_ids = [e.class_id for e in enrollments]
    assignments = active_assignments.filter(Assignment.class_id.in_(enrolled_ids)).order_by(Assignment.id.desc()).all()
    cards = assignment_cards(assignments, current_user.id)
    for card in cards:
        card["deadline"] = submission_deadline(card["assignment"], card["submission"])
    upcoming = sorted([c for c in cards if c["status"] in {"PENDING", "RETURNED"} and c["deadline"]
                       and beijing_now() < c["deadline"] <= beijing_now() + timedelta(days=3)],
                      key=lambda c: c["deadline"])
    graded = sorted([c for c in cards if c["status"] == "GRADED"],
                    key=lambda c: (c["submission"].reviewed_at or c["submission"].created_at, c["submission"].id), reverse=True)[:5]
    return render_template("dashboard.html", enrollments=enrollments, cards=cards, upcoming=upcoming, graded=graded)


@app.route("/course/list")
@app.route("/course")
@login_required
def course_list():
    if current_user.role == "TEACHER":
        courses = Course.query.filter_by(teacher_id=current_user.id).order_by(Course.id.desc()).all()
    else:
        courses = Course.query.join(TeachingClass).join(Enrollment).filter(
            Enrollment.student_id == current_user.id, Enrollment.active.is_(True)).distinct().order_by(Course.id.desc()).all()
    return render_template("course_list.html", courses=courses)


@app.route("/course/create", methods=["POST"])
@login_required
def create_course():
    if current_user.role != "TEACHER":
        abort(403)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name or len(name) > 120 or len(description) > 10000:
        flash("课程名称需为 1-120 个字符，简介最多 10000 字。", "error")
    else:
        db.session.add(Course(name=name, description=description, teacher_id=current_user.id))
        db.session.commit()
        flash("课程创建成功。", "success")
    return redirect(url_for("course_list"))


@app.route("/course/<int:course_id>")
@login_required
def course_detail(course_id):
    course = db.get_or_404(Course, course_id)
    if current_user.role == "TEACHER" and course.teacher_id != current_user.id:
        abort(403)
    if current_user.role == "STUDENT" and not any(is_enrolled(current_user.id, c.id) for c in course.classes):
        abort(403)
    visible_classes = course.classes if current_user.role == "TEACHER" else [
        c for c in course.classes if is_enrolled(current_user.id, c.id)]
    return render_template("course_detail.html", course=course, visible_classes=visible_classes)


@app.route("/course/<int:course_id>/edit", methods=["POST"])
@login_required
def edit_course(course_id):
    course = teacher_course(course_id)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name or len(name) > 120 or len(description) > 10000:
        flash("课程名称需为 1-120 个字符，简介最多 10000 字。", "error")
    else:
        course.name = name
        course.description = description
        db.session.commit()
        flash("课程信息已更新。", "success")
    return redirect(url_for("course_detail", course_id=course.id))


def new_join_code():
    while True:
        code = token_urlsafe(5).replace("-", "").replace("_", "").upper()[:8]
        if not TeachingClass.query.filter_by(join_code=code).first():
            return code


@app.route("/class/create", methods=["POST"])
@login_required
def create_class():
    if current_user.role != "TEACHER":
        abort(403)
    course = teacher_course(request.form.get("course_id", type=int))
    if course.archived:
        abort(409, description="请先恢复课程再创建教学班。")
    name = request.form.get("name", "").strip()
    if not name or len(name) > 120:
        flash("教学班名称需为 1-120 个字符。", "error")
    else:
        db.session.add(TeachingClass(name=name, course_id=course.id, teacher_id=current_user.id, join_code=new_join_code()))
        db.session.commit()
        flash("教学班创建成功，已生成加入码。", "success")
    return redirect(url_for("course_detail", course_id=course.id))


@app.route("/class/join", methods=["POST"])
@login_required
def join_class():
    if current_user.role != "STUDENT":
        abort(403)
    code = request.form.get("join_code", "").strip().upper()
    teaching_class = TeachingClass.query.filter_by(join_code=code).first()
    if not teaching_class:
        flash("加入码不存在，请检查后重试。", "error")
    elif teaching_class.archived or teaching_class.course.archived or not teaching_class.join_enabled:
        flash("该班级已关闭加入或已归档，请联系教师。", "error")
    elif Enrollment.query.filter_by(class_id=teaching_class.id, student_id=current_user.id, active=False).first():
        flash("你的班级成员资格已停用，请联系教师恢复。", "error")
    elif is_enrolled(current_user.id, teaching_class.id):
        flash("你已经加入这个教学班。", "error")
    else:
        db.session.add(Enrollment(class_id=teaching_class.id, student_id=current_user.id))
        for assignment in teaching_class.assignments:
            if not assignment.archived:
                notify_assignment(assignment, current_user.id)
        db.session.commit()
        flash(f"已加入 {teaching_class.course.name} · {teaching_class.name}。", "success")
    return redirect(url_for("dashboard"))


@app.route("/class/<int:class_id>/members")
@login_required
def class_members(class_id):
    teaching_class = teacher_class(class_id)
    page = Enrollment.query.options(joinedload(Enrollment.student)).filter_by(class_id=class_id).order_by(
        Enrollment.student_id).paginate(per_page=20, error_out=False)
    return render_template("class_members.html", teaching_class=teaching_class, page=page)


@app.route("/assignment/list")
@login_required
def assignment_list():
    keyword = request.args.get("q", "").strip()
    course_id = request.args.get("course_id", type=int)
    class_id = request.args.get("class_id", type=int)
    status = request.args.get("status", "")
    archived = request.args.get("archived") == "1"
    query = Assignment.query
    if current_user.role == "TEACHER":
        query = query.filter_by(teacher_id=current_user.id)
    else:
        enrolled_ids = [e.class_id for e in Enrollment.query.filter_by(student_id=current_user.id, active=True).all()]
        query = query.filter(Assignment.class_id.in_(enrolled_ids))
    if keyword:
        query = query.filter(or_(Assignment.title.contains(keyword), Assignment.description.contains(keyword)))
    if course_id:
        query = query.filter_by(course_id=course_id)
    if class_id:
        query = query.filter_by(class_id=class_id)
    query = query.join(Course, Course.id == Assignment.course_id).join(TeachingClass, TeachingClass.id == Assignment.class_id)
    if archived:
        query = query.filter(or_(Assignment.archived.is_(True), Course.archived.is_(True), TeachingClass.archived.is_(True)))
    else:
        query = query.filter(Assignment.archived.is_(False), Course.archived.is_(False), TeachingClass.archived.is_(False))
    query = filter_assignment_status(query, status, current_user)
    query = query.options(joinedload(Assignment.course), joinedload(Assignment.teaching_class), joinedload(Assignment.teacher))
    page = query.order_by(Assignment.due_at.is_(None), Assignment.due_at.asc(), Assignment.id.desc()).paginate(per_page=12, error_out=False)
    cards = assignment_cards(page.items, current_user.id if current_user.role == "STUDENT" else None)
    if current_user.role == "TEACHER":
        courses = Course.query.filter_by(teacher_id=current_user.id).all()
        classes = TeachingClass.query.filter_by(teacher_id=current_user.id).all()
    else:
        courses = Course.query.join(TeachingClass).join(Enrollment).filter(
            Enrollment.student_id == current_user.id, Enrollment.active.is_(True)).distinct().order_by(Course.id).all()
        classes = [e.teaching_class for e in Enrollment.query.filter_by(student_id=current_user.id, active=True).all()]
    return render_template("assignment_list.html", cards=cards, assignments=page.items, courses=courses, page=page,
                           classes=classes, filters={"q": keyword, "course_id": course_id, "class_id": class_id, "status": status, "archived": "1" if archived else "0"})


@app.route("/assignment/create", methods=["POST"])
@login_required
def create_assignment():
    if current_user.role != "TEACHER":
        abort(403)
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    course_id = request.form.get("course_id", type=int)
    class_id = request.form.get("class_id", type=int)
    due_at = parse_due_at(request.form.get("due_at", ""))
    if not title or len(title) > 200 or not description or len(description) > 20000:
        flash("请填写 1-200 字的作业标题和作业要求。", "error")
    elif due_at == "invalid":
        flash("截止时间格式不正确。", "error")
    elif not course_id or not class_id or not due_at:
        flash("请选择所属课程、教学班并设置截止时间。", "error")
    elif due_at <= beijing_now():
        flash("新作业的截止时间必须晚于当前时间。", "error")
    else:
        course = teacher_course(course_id)
        teaching_class = teacher_class(class_id)
        if course.archived or teaching_class.archived:
            abort(409, description="请先恢复课程和教学班再发布作业。")
        if teaching_class.course_id != course.id:
            flash("所选班级不属于该课程。", "error")
            return redirect(url_for("assignment_list"))
        assignment = Assignment(title=title, description=description, teacher_id=current_user.id,
                                  course_id=course.id if course else None, class_id=teaching_class.id if teaching_class else None,
                                  due_at=due_at)
        db.session.add(assignment)
        db.session.flush()
        notify_assignment(assignment)
        db.session.commit()
        flash("作业已发布。", "success")
        return redirect(url_for("assignment_detail", id=assignment.id))
    return redirect(url_for("assignment_list"))


@app.route("/assignment/detail/<int:id>")
@login_required
def assignment_detail(id):
    assignment = db.get_or_404(Assignment, id)
    if current_user.role == "TEACHER" and assignment.teacher_id != current_user.id:
        abort(403)
    if current_user.role == "STUDENT" and not student_can_view(assignment, current_user.id):
        abort(403)
    current_query = effective_submissions().filter(Submission.assignment_id == id)
    history_query = Submission.query.filter_by(assignment_id=id).filter(~Submission.id.in_(
        current_query.with_entities(Submission.id)))
    if current_user.role == "STUDENT":
        current_query = current_query.filter_by(student_id=current_user.id)
        history_query = history_query.filter_by(student_id=current_user.id)
    keyword = request.args.get("student", "").strip()[:80]
    review_filter = request.args.get("review", "")
    if current_user.role == "TEACHER" and keyword:
        current_query = current_query.join(User, Submission.student_id == User.id).filter(or_(
            User.username.contains(keyword), User.real_name.contains(keyword), User.student_number.contains(keyword)))
    if review_filter in {"PENDING", "GRADED", "RETURNED"} and current_user.role == "TEACHER":
        current_query = current_query.filter(Submission.review_state == review_filter)
    options = (joinedload(Submission.student), selectinload(Submission.review_events).joinedload(ReviewEvent.teacher))
    current_page = current_query.options(*options).order_by(Submission.id.desc()).paginate(per_page=20, error_out=False)
    history_count = history_query.count()
    history_page = history_query.options(*options).order_by(Submission.id.desc()).paginate(
        page=request.args.get("history_page", 1, type=int), per_page=20, error_out=False) if request.args.get("history") == "1" else None
    submissions = current_page.items + (history_page.items if history_page else [])
    active = {s.id for s in current_page.items}
    older = aliased(Submission)
    version_count = db.select(db.func.count(older.id)).where(older.assignment_id == Submission.assignment_id,
        older.student_id == Submission.student_id, older.id <= Submission.id).correlate(Submission).scalar_subquery()
    versions = dict(db.session.query(Submission.id, version_count).filter(Submission.id.in_([s.id for s in submissions])).all())
    latest = latest_submission(id, current_user.id) if current_user.role == "STUDENT" else None
    from workflow import grade_rows
    progress_page = Enrollment.query.options(joinedload(Enrollment.student)).filter_by(class_id=assignment.class_id, active=True).order_by(
        Enrollment.student_id).paginate(page=request.args.get("roster_page", 1, type=int), per_page=20, error_out=False) if current_user.role == "TEACHER" and assignment.teaching_class else None
    progress = [{"student": row["student"], "status": row["cells"][0]["status"]}
                for row in grade_rows(assignment.teaching_class, [assignment], enrollments=progress_page.items)] if progress_page else []
    return render_template("assignment_detail.html", assignment=assignment, submissions=submissions, progress=progress,
                           current_page=current_page, history_page=history_page, history_count=history_count, progress_page=progress_page,
                           student_filter=keyword, review_filter=review_filter, teaching_open=teaching_open(assignment),
                           upload_token=token_urlsafe(32),
                           active_ids=active, versions=versions, current_submission=latest,
                           can_upload=can_submit(assignment, latest), deadline=submission_deadline(assignment, latest),
                           student_status=assignment_status(assignment, current_user.id) if current_user.role == "STUDENT" else None)


@app.route("/assignment/submit", methods=["POST"])
@login_required
def submit_homework():
    if current_user.role != "STUDENT":
        abort(403)
    assignment = db.get_or_404(Assignment, request.form.get("assignment_id", type=int))
    if not student_can_view(assignment, current_user.id):
        abort(403)
    previous = latest_submission(assignment.id, current_user.id)
    upload_token = request.form.get("request_token", "")
    if not 16 <= len(upload_token) <= 64 or not all(c.isalnum() or c in "-_" for c in upload_token):
        abort(400, description="上传表单已失效，请刷新后重试。")
    duplicate = Submission.query.filter_by(student_id=current_user.id, request_token=upload_token).first()
    if duplicate:
        if duplicate.assignment_id != assignment.id:
            abort(409, description="上传表单已用于其他作业，请刷新页面。")
        flash("这次提交已经保存，无需重复上传。", "info")
        return redirect(url_for("assignment_detail", id=assignment.id))
    if not can_submit(assignment, previous):
        flash("该作业已超过截止时间，系统不再接受提交。", "error")
        return redirect(url_for("assignment_detail", id=assignment.id))
    file = request.files.get("file")
    original_name = file.filename.replace("\\", "/").split("/")[-1] if file and file.filename else ""
    suffix = Path(original_name).suffix.lower().lstrip(".")
    if not file or not original_name:
        flash("请选择要提交的文件。", "error")
    elif suffix not in ALLOWED_EXTENSIONS:
        flash("暂不支持该文件类型，请上传文档、图片、代码或压缩包。", "error")
    elif len(original_name) > 255 or any(ord(char) < 32 for char in original_name) or not valid_attachment(file, suffix):
        flash("文件为空、内容与扩展名不符或文件名无效，请检查后重新上传。", "error")
    else:
        stored_name = f"{uuid4().hex}.{suffix}"
        stored_path = Path(app.config["UPLOAD_FOLDER"]) / stored_name
        try:
            file.save(stored_path)
            db.session.add(Submission(assignment_id=assignment.id, student_id=current_user.id,
                                      file_path=stored_name, original_name=original_name, created_at=beijing_now(),
                                      request_token=upload_token,
                                      redo_due_at=previous.redo_due_at if previous else None))
            db.session.commit()
        except Exception:
            db.session.rollback()
            stored_path.unlink(missing_ok=True)
            raise
        flash("作业提交成功。再次提交会保留历史记录。", "success")
    return redirect(url_for("assignment_detail", id=assignment.id))


@app.route("/submission/<int:id>/download")
@login_required
def download_submission(id):
    submission = db.get_or_404(Submission, id)
    assignment = db.session.get(Assignment, submission.assignment_id)
    allowed = (current_user.role == "STUDENT" and submission.student_id == current_user.id
               and assignment and student_can_view(assignment, current_user.id)) or (
        current_user.role == "TEACHER" and assignment and assignment.teacher_id == current_user.id)
    if not allowed:
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], submission.file_path,
                               as_attachment=True, download_name=submission.original_name)


@app.route("/assignment/grade", methods=["POST"])
@login_required
def grade_homework():
    if current_user.role != "TEACHER":
        abort(403)
    submission = db.get_or_404(Submission, request.form.get("submission_id", type=int))
    assignment = db.session.get(Assignment, submission.assignment_id)
    if not assignment or assignment.teacher_id != current_user.id:
        abort(403)
    if latest_submission(assignment.id, submission.student_id).id != submission.id:
        abort(409, description="该提交已成为历史版本，请刷新页面批改当前有效版本。")
    if submission.review_state == "RETURNED":
        abort(409, description="该版本已退回，请等待学生重新提交。")
    if not teaching_open(assignment):
        abort(409, description="该作业已归档，请恢复后再批改。")
    from security import require_revision
    require_revision(submission)
    score = request.form.get("score", type=int)
    if score is None or not 0 <= score <= 100 or len(request.form.get("comment", "")) > 2000:
        flash("分数必须为 0-100 的整数，评语最多 2000 字。", "error")
    else:
        comment = request.form.get("comment", "").strip()
        if submission.score == score and submission.comment == comment and submission.review_state == "GRADED":
            flash("评分与评语没有变化。", "info")
            return redirect(url_for("assignment_detail", id=assignment.id))
        record_review(submission, current_user.id, "GRADE", score=score, comment=request.form.get("comment", "").strip())
        submission.score = score
        submission.comment = request.form.get("comment", "").strip()
        submission.review_state = "GRADED"
        submission.reviewed_at = beijing_now()
        notify(submission.student_id, assignment, "作业评分完成", f"{assignment.title}：{score} 分，请查看教师评语。")
        db.session.commit()
        flash("评分已保存。", "success")
    return redirect(url_for("assignment_detail", id=assignment.id))


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


def error_page(error):
    messages = {400: "请求无效，或表单验证已失效，请刷新页面重试。", 403: "你没有权限访问此内容。",
                404: "页面或文件不存在。", 405: "该操作不支持当前请求方式。", 409: "数据已更新，请刷新页面后重试。",
                413: "文件及表单内容不能超过 20 MB。", 500: "服务暂时无法处理请求，请稍后重试。"}
    if error.code in {409, 500}:
        db.session.rollback()
    message = error.description if error.code in {400, 409} else messages[error.code]
    return render_template("error.html", code=error.code, message=message), error.code


for error_code in (400, 403, 404, 405, 409, 413, 500):
    app.register_error_handler(error_code, error_page)


@app.errorhandler(StaleDataError)
def stale_write(error):
    db.session.rollback()
    from werkzeug.exceptions import Conflict
    return error_page(Conflict(description="数据已被其他页面更新，请刷新后重试。"))

from final_stage import final_stage
app.register_blueprint(final_stage)
from class_management import management
app.register_blueprint(management)
from teaching_tools import teaching_tools
app.register_blueprint(teaching_tools)
from maintenance import register_maintenance
register_maintenance(app)


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
