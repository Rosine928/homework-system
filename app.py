import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import inspect, or_, text
from werkzeug.security import check_password_hash, generate_password_hash

from models import Assignment, Course, Enrollment, Submission, TeachingClass, User, beijing_now, db

BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt", "zip", "rar", "py", "java", "c", "cpp", "ppt", "pptx", "xls", "xlsx", "jpg", "jpeg", "png"}
STATUS_LABELS = {"PENDING": "待提交", "OVERDUE": "已逾期", "TO_GRADE": "待批改", "GRADED": "已评分"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "coursework-demo-change-this-key"),
    SQLALCHEMY_DATABASE_URI=os.environ.get("HOMEWORK_DATABASE_URI", f"sqlite:///{BASE_DIR / 'homework.db'}"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.environ.get("HOMEWORK_UPLOAD_FOLDER", str(BASE_DIR / "uploads")),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)
Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录后继续。"
login_manager.login_message_category = "error"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


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
    db.create_all()
    migrate_legacy_schema()


@app.context_processor
def inject_helpers():
    return {"now": beijing_now, "status_labels": STATUS_LABELS}


def parse_due_at(raw):
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(raw)
        return result if result.tzinfo is None else "invalid"
    except ValueError:
        return "invalid"


def is_enrolled(student_id, class_id):
    return db.session.query(Enrollment.id).filter_by(student_id=student_id, class_id=class_id).first() is not None


def student_can_view(assignment, student_id):
    return assignment.class_id is not None and is_enrolled(student_id, assignment.class_id)


def latest_submission(assignment_id, student_id):
    return Submission.query.filter_by(assignment_id=assignment_id, student_id=student_id).order_by(
        Submission.created_at.desc(), Submission.id.desc()
    ).first()


def assignment_status(assignment, student_id):
    submission = latest_submission(assignment.id, student_id)
    if submission:
        return "GRADED" if submission.score is not None else "TO_GRADE"
    return "OVERDUE" if assignment.due_at and assignment.due_at <= beijing_now() else "PENDING"


def assignment_card(assignment, student_id=None):
    if student_id:
        submission = latest_submission(assignment.id, student_id)
        return {"assignment": assignment, "status": assignment_status(assignment, student_id), "submission": submission}
    pending = Submission.query.filter_by(assignment_id=assignment.id, score=None).count()
    total = Submission.query.filter_by(assignment_id=assignment.id).count()
    return {"assignment": assignment, "pending_count": pending, "submission_count": total}


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


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "STUDENT")
        if len(username) < 2 or len(username) > 40 or len(password) < 6:
            flash("用户名需为 2-40 个字符，密码至少 6 位。", "error")
            return redirect(url_for("register"))
        elif role not in {"STUDENT", "TEACHER"}:
            flash("请选择有效的账号角色。", "error")
            return redirect(url_for("register"))
        elif User.query.filter_by(username=username).first():
            flash("该用户名已被使用。", "error")
            return redirect(url_for("register"))
        else:
            db.session.add(User(username=username, password=generate_password_hash(password), role=role))
            db.session.commit()
            flash("注册成功，请登录。", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        password = request.form.get("password", "")
        if user and (check_password_hash(user.password, password) or user.password == password):
            if user.password == password:
                user.password = generate_password_hash(password)
                db.session.commit()
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("用户名或密码不正确。", "error")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已安全退出。", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.role == "TEACHER":
        courses = Course.query.filter_by(teacher_id=current_user.id).all()
        classes = TeachingClass.query.filter_by(teacher_id=current_user.id).all()
        assignments = Assignment.query.filter_by(teacher_id=current_user.id).all()
        pending_grades = Submission.query.join(Assignment).filter(
            Assignment.teacher_id == current_user.id, Submission.score.is_(None)
        ).count()
        pending_assignments = Assignment.query.join(Submission).filter(
            Assignment.teacher_id == current_user.id, Submission.score.is_(None)
        ).distinct().order_by(Assignment.id.desc()).all()
        return render_template("dashboard.html", courses=courses, classes=classes, assignments=assignments,
                               pending_grades=pending_grades,
                               pending_cards=[assignment_card(a) for a in pending_assignments])
    enrollments = Enrollment.query.filter_by(student_id=current_user.id).all()
    assignments = [a for a in Assignment.query.order_by(Assignment.id.desc()).all() if student_can_view(a, current_user.id)]
    cards = [assignment_card(a, current_user.id) for a in assignments]
    upcoming = sorted([c for c in cards if c["status"] == "PENDING" and c["assignment"].due_at
                       and beijing_now() < c["assignment"].due_at <= beijing_now() + timedelta(days=3)],
                      key=lambda c: c["assignment"].due_at)
    graded = sorted([c for c in cards if c["status"] == "GRADED"],
                    key=lambda c: (c["submission"].created_at, c["submission"].id), reverse=True)[:5]
    return render_template("dashboard.html", enrollments=enrollments, cards=cards, upcoming=upcoming, graded=graded)


@app.route("/course/list")
@app.route("/course")
@login_required
def course_list():
    if current_user.role == "TEACHER":
        courses = Course.query.filter_by(teacher_id=current_user.id).order_by(Course.id.desc()).all()
    else:
        courses = Course.query.join(TeachingClass).join(Enrollment).filter(
            Enrollment.student_id == current_user.id).distinct().order_by(Course.id.desc()).all()
    return render_template("course_list.html", courses=courses)


@app.route("/course/create", methods=["POST"])
@login_required
def create_course():
    if current_user.role != "TEACHER":
        abort(403)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name or len(name) > 120:
        flash("课程名称需为 1-120 个字符。", "error")
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
    if not name or len(name) > 120:
        flash("课程名称需为 1-120 个字符。", "error")
    else:
        course.name = name
        course.description = request.form.get("description", "").strip()
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
    elif is_enrolled(current_user.id, teaching_class.id):
        flash("你已经加入这个教学班。", "error")
    else:
        db.session.add(Enrollment(class_id=teaching_class.id, student_id=current_user.id))
        db.session.commit()
        flash(f"已加入 {teaching_class.course.name} · {teaching_class.name}。", "success")
    return redirect(url_for("dashboard"))


@app.route("/class/<int:class_id>/members")
@login_required
def class_members(class_id):
    teaching_class = teacher_class(class_id)
    return render_template("class_members.html", teaching_class=teaching_class)


@app.route("/assignment/list")
@login_required
def assignment_list():
    keyword = request.args.get("q", "").strip()
    course_id = request.args.get("course_id", type=int)
    class_id = request.args.get("class_id", type=int)
    status = request.args.get("status", "")
    query = Assignment.query
    if current_user.role == "TEACHER":
        query = query.filter_by(teacher_id=current_user.id)
    else:
        enrolled_ids = [e.class_id for e in Enrollment.query.filter_by(student_id=current_user.id).all()]
        query = query.filter(Assignment.class_id.in_(enrolled_ids))
    if keyword:
        query = query.filter(or_(Assignment.title.contains(keyword), Assignment.description.contains(keyword)))
    if course_id:
        query = query.filter_by(course_id=course_id)
    if class_id:
        query = query.filter_by(class_id=class_id)
    assignments = query.order_by(Assignment.due_at.is_(None), Assignment.due_at.asc(), Assignment.id.desc()).all()
    cards = [assignment_card(a, current_user.id if current_user.role == "STUDENT" else None) for a in assignments]
    if status:
        cards = [c for c in cards if (c.get("status") == status
                 or (status == "SUBMITTED" and c.get("status") in {"TO_GRADE", "GRADED"})
                 or (current_user.role == "TEACHER" and status == "TO_GRADE" and c["pending_count"] > 0))]
    if current_user.role == "TEACHER":
        courses = Course.query.filter_by(teacher_id=current_user.id).all()
        classes = TeachingClass.query.filter_by(teacher_id=current_user.id).all()
    else:
        courses = Course.query.join(TeachingClass).join(Enrollment).filter(
            Enrollment.student_id == current_user.id).distinct().order_by(Course.id).all()
        classes = [e.teaching_class for e in Enrollment.query.filter_by(student_id=current_user.id).all()]
    return render_template("assignment_list.html", cards=cards, assignments=[c["assignment"] for c in cards], courses=courses,
                           classes=classes, filters={"q": keyword, "course_id": course_id, "class_id": class_id, "status": status})


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
    if not title or len(title) > 200 or not description:
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
        if teaching_class.course_id != course.id:
            flash("所选班级不属于该课程。", "error")
            return redirect(url_for("assignment_list"))
        db.session.add(Assignment(title=title, description=description, teacher_id=current_user.id,
                                  course_id=course.id if course else None, class_id=teaching_class.id if teaching_class else None,
                                  due_at=due_at))
        db.session.commit()
        flash("作业已发布。", "success")
    return redirect(url_for("assignment_list"))


@app.route("/assignment/detail/<int:id>")
@login_required
def assignment_detail(id):
    assignment = db.get_or_404(Assignment, id)
    if current_user.role == "TEACHER" and assignment.teacher_id != current_user.id:
        abort(403)
    if current_user.role == "STUDENT" and not student_can_view(assignment, current_user.id):
        abort(403)
    query = Submission.query.filter_by(assignment_id=id)
    if current_user.role == "STUDENT":
        query = query.filter_by(student_id=current_user.id)
    submissions = query.order_by(Submission.created_at.desc(), Submission.id.desc()).all()
    progress = [{"student": e.student, "status": assignment_status(assignment, e.student_id)}
                for e in assignment.teaching_class.enrollments] if current_user.role == "TEACHER" and assignment.teaching_class else []
    return render_template("assignment_detail.html", assignment=assignment, submissions=submissions, progress=progress,
                           student_status=assignment_status(assignment, current_user.id) if current_user.role == "STUDENT" else None)


@app.route("/assignment/submit", methods=["POST"])
@login_required
def submit_homework():
    if current_user.role != "STUDENT":
        abort(403)
    assignment = db.get_or_404(Assignment, request.form.get("assignment_id", type=int))
    if not student_can_view(assignment, current_user.id):
        abort(403)
    if assignment.due_at and assignment.due_at <= beijing_now():
        flash("该作业已超过截止时间，系统不再接受提交。", "error")
        return redirect(url_for("assignment_detail", id=assignment.id))
    file = request.files.get("file")
    original_name = file.filename.replace("\\", "/").split("/")[-1] if file and file.filename else ""
    suffix = Path(original_name).suffix.lower().lstrip(".")
    if not file or not original_name:
        flash("请选择要提交的文件。", "error")
    elif suffix not in ALLOWED_EXTENSIONS:
        flash("暂不支持该文件类型，请上传文档、图片、代码或压缩包。", "error")
    else:
        stored_name = f"{uuid4().hex}.{suffix}"
        file.save(Path(app.config["UPLOAD_FOLDER"]) / stored_name)
        db.session.add(Submission(assignment_id=assignment.id, student_id=current_user.id,
                                  file_path=stored_name, original_name=original_name, created_at=beijing_now()))
        db.session.commit()
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
    score = request.form.get("score", type=int)
    if score is None or not 0 <= score <= 100:
        flash("分数必须在 0 到 100 之间。", "error")
    else:
        submission.score = score
        submission.comment = request.form.get("comment", "").strip()
        db.session.commit()
        flash("评分已保存。", "success")
    return redirect(url_for("assignment_detail", id=assignment.id))


@app.errorhandler(413)
def file_too_large(_error):
    flash("文件不能超过 20 MB。", "error")
    return redirect(request.referrer or url_for("assignment_list"))


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
