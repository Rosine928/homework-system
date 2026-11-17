"""Non-destructive course, class and membership maintenance."""
from secrets import token_urlsafe
from flask import Blueprint, abort, flash, redirect, request, url_for
from flask_login import current_user, login_required
from models import Course, TeachingClass, Enrollment, AccountEvent, db
import json

management = Blueprint("management", __name__)


def owned(model, identifier):
    row = db.get_or_404(model, identifier)
    if current_user.role != "TEACHER" or row.teacher_id != current_user.id:
        abort(403)
    return row


@management.post("/course/<int:id>/archive")
@login_required
def archive_course(id):
    course = owned(Course, id)
    action = request.form.get("action")
    if action not in {"archive", "restore"}:
        abort(400)
    course.archived = action == "archive"
    db.session.commit()
    flash("课程状态已更新。归档期间停止加入、发布、提交和批改，已有资料及成绩保留。", "success")
    return redirect(url_for("course_detail", course_id=id))


@management.post("/class/<int:id>/manage")
@login_required
def manage_class(id):
    teaching_class = owned(TeachingClass, id)
    action = request.form.get("action")
    if action == "rename":
        name = request.form.get("name", "").strip()
        if not 1 <= len(name) <= 120:
            abort(400, description="班级名称需为 1-120 字。")
        teaching_class.name = name
    elif action == "rotate":
        code = token_urlsafe(8).replace("-", "").replace("_", "").upper()[:10]
        while TeachingClass.query.filter_by(join_code=code).first():
            code = token_urlsafe(8).replace("-", "").replace("_", "").upper()[:10]
        teaching_class.join_code = code
    elif action in {"open", "close"}:
        teaching_class.join_enabled = action == "open"
    elif action in {"archive", "restore"}:
        teaching_class.archived = action == "archive"
    else:
        abort(400)
    db.session.commit()
    flash("班级设置已保存。", "success")
    return redirect(url_for("course_detail", course_id=teaching_class.course_id))


@management.post("/class/<int:id>/member/<int:student_id>")
@login_required
def manage_member(id, student_id):
    owned(TeachingClass, id)
    enrollment = Enrollment.query.filter_by(class_id=id, student_id=student_id).first_or_404()
    action = request.form.get("action")
    reason = request.form.get("reason", "").strip()
    if action not in {"disable", "restore"} or not 1 <= len(reason) <= 500:
        abort(400, description="请选择停用或恢复，并填写 1-500 字原因。")
    enabled = action == "restore"
    if enrollment.active != enabled:
        enrollment.active = enabled
        db.session.add(AccountEvent(user_id=student_id, action="MEMBERSHIP_CHANGE", details=json.dumps(
            {"class_id": id, "teacher_id": current_user.id, "active": enabled, "reason": reason}, ensure_ascii=False)))
        db.session.commit()
    flash("成员状态已更新，历史提交与成绩保留。", "success")
    return redirect(url_for("class_members", class_id=id))
