"""Authorized reference materials and current-submission exports."""
import csv
import io
import re
import tempfile
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from flask import Blueprint, abort, current_app, flash, redirect, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from models import Assignment, Enrollment, Submission, db
from security import require_revision, valid_attachment
from workflow import effective_submissions, notify, teaching_open
from final_stage import csv_cell

teaching_tools = Blueprint("teaching_tools", __name__)


def assignment_access(id, teacher=False):
    assignment = db.get_or_404(Assignment, id)
    allowed = current_user.role == "TEACHER" and assignment.teacher_id == current_user.id
    if not teacher and current_user.role == "STUDENT":
        allowed = Enrollment.query.filter_by(class_id=assignment.class_id, student_id=current_user.id, active=True).first() is not None
    if not allowed:
        abort(403)
    return assignment


@teaching_tools.route("/assignment/<int:id>/reference", methods=["POST"])
@login_required
def update_reference(id):
    assignment = assignment_access(id, teacher=True)
    require_revision(assignment)
    if not teaching_open(assignment):
        abort(400, description="归档后不能修改教学资料。")
    rubric = request.form.get("rubric", "").strip()
    if len(rubric) > 5000:
        abort(400, description="评分标准最多 5000 字。")
    file = request.files.get("reference")
    saved = None
    if file and file.filename:
        name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if len(name) > 200 or any(ord(c) < 32 for c in name) or suffix not in current_app.config["ALLOWED_EXTENSIONS"] or not valid_attachment(file, suffix):
            abort(400, description="参考附件格式或内容无效。")
        saved = Path(current_app.config["UPLOAD_FOLDER"]) / f"{uuid4().hex}.{suffix}"
    if not saved and rubric == assignment.rubric and not (request.form.get("remove_reference") and assignment.reference_path):
        flash("教学资料没有变化。", "info")
        return redirect(url_for("assignment_detail", id=id))
    try:
        if saved:
            file.save(saved)
            assignment.reference_path, assignment.reference_name = saved.name, name
        elif request.form.get("remove_reference"):
            assignment.reference_path = assignment.reference_name = None
        assignment.rubric = rubric
        for enrollment in Enrollment.query.filter_by(class_id=assignment.class_id, active=True):
            notify(enrollment.student_id, assignment, "教学资料更新", "教师已更新评分标准或参考附件，请查看作业详情。")
        db.session.commit()
    except Exception:
        db.session.rollback()
        if saved:
            saved.unlink(missing_ok=True)
        raise
    flash("教学资料已保存。", "success")
    return redirect(url_for("assignment_detail", id=id))


@teaching_tools.route("/assignment/<int:id>/reference")
@login_required
def download_reference(id):
    assignment = assignment_access(id)
    if not assignment.reference_path:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], assignment.reference_path, as_attachment=True, download_name=assignment.reference_name)


def safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:100] or "attachment"


@teaching_tools.route("/assignment/<int:id>/current.zip")
@login_required
def download_current(id):
    assignment_access(id, teacher=True)
    submissions = effective_submissions().filter_by(assignment_id=id).options(joinedload(Submission.student)).all()
    if not submissions:
        abort(404, description="暂无可下载的提交。")
    root = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    files = []
    for submission in submissions:
        path = root / submission.file_path
        if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
            abort(404, description="部分附件缺失，请联系维护人员检查数据。")
        files.append(path)
    if sum(p.stat().st_size for p in files) > 500 * 1024 * 1024:
        abort(400, description="附件总量超过 500 MB，请分次下载单份附件。")
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        roster = io.StringIO(newline="")
        writer = csv.writer(roster)
        writer.writerow(["账号", "姓名", "学号", "提交编号", "附件路径"])
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for submission, path in zip(submissions, files):
                student = submission.student
                original = Path(submission.original_name.replace('\\', '/')).name
                filename = safe_name(Path(original).stem) + re.sub(r'[^A-Za-z0-9.]', '_', Path(original).suffix)[:12]
                name = f"{student.id}_{safe_name(student.display_name)}/{submission.id}_{filename}"
                archive.write(path, name)
                writer.writerow([csv_cell(v) for v in (student.username, student.display_name, student.student_number or "", submission.id, name)])
            archive.writestr("students.csv", "\ufeff" + roster.getvalue())
        output.seek(0)
        response = send_file(output, as_attachment=True, download_name=f"assignment-{id}-current.zip", mimetype="application/zip")
        response.call_on_close(output.close)
        return response
    except Exception:
        output.close()
        raise
