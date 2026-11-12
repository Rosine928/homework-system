"""Gradebook, feedback, exports and personal notifications."""
import csv
import io
from datetime import datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models import Assignment, Course, Enrollment, Notification, Submission, TeachingClass, beijing_now, db
from workflow import (assignment_statistics, grade_rows, latest_submission, notify, record_review)

final_stage = Blueprint("final_stage", __name__)


def owned_class(class_id):
    if current_user.role != "TEACHER":
        abort(403)
    teaching_class = db.get_or_404(TeachingClass, class_id)
    if teaching_class.teacher_id != current_user.id:
        abort(403)
    return teaching_class


def class_report(teaching_class, assignment_id=None):
    query = Assignment.query.filter_by(class_id=teaching_class.id, teacher_id=current_user.id)
    if assignment_id is not None:
        assignment = db.get_or_404(Assignment, assignment_id)
        if assignment.class_id != teaching_class.id or assignment.teacher_id != current_user.id:
            abort(403)
        query = query.filter_by(id=assignment.id)
    assignments = query.order_by(Assignment.id).all()
    return assignments, grade_rows(teaching_class, assignments)


@final_stage.route("/submission/<int:id>/return", methods=["POST"])
@login_required
def return_submission(id):
    submission = db.get_or_404(Submission, id)
    assignment = submission.assignment
    if current_user.role != "TEACHER" or assignment.teacher_id != current_user.id:
        abort(403)
    if latest_submission(assignment.id, submission.student_id).id != submission.id:
        abort(409, description="该提交已成为历史版本，请刷新页面操作当前有效版本。")
    if submission.review_state == "RETURNED":
        abort(409, description="该版本已退回，请等待学生重新提交。")
    if assignment.archived:
        abort(409, description="该作业已归档，请恢复后操作。")
    reason = request.form.get("reason", "").strip()
    raw_deadline = request.form.get("redo_due_at", "")
    try:
        deadline = datetime.fromisoformat(raw_deadline)
    except ValueError:
        deadline = None
    if not 1 <= len(reason) <= 2000:
        flash("请填写 1-2000 字的退回原因。", "error")
    elif not deadline or deadline.tzinfo is not None or deadline <= beijing_now():
        flash("请设置晚于当前时间的重交截止时间（北京时间）。", "error")
    else:
        record_review(submission, current_user.id, "RETURN", deadline=deadline, reason=reason)
        submission.review_state = "RETURNED"
        submission.return_reason = reason
        submission.redo_due_at = deadline
        submission.reviewed_at = beijing_now()
        notify(submission.student_id, assignment, "作业退回重做",
               f"{assignment.title}：{reason}；重交截止：{deadline:%Y-%m-%d %H:%M}。")
        db.session.commit()
        flash("作业已退回，学生已收到重做通知。", "success")
    return redirect(url_for("assignment_detail", id=assignment.id))


def owned_assignment(id):
    assignment = db.get_or_404(Assignment, id)
    if current_user.role != "TEACHER" or assignment.teacher_id != current_user.id:
        abort(403)
    return assignment


@final_stage.route("/submission/<int:id>/extend", methods=["POST"])
@login_required
def extend_redo(id):
    submission = db.get_or_404(Submission, id)
    assignment = owned_assignment(submission.assignment_id)
    if assignment.archived or latest_submission(assignment.id, submission.student_id).id != id or submission.review_state != "RETURNED":
        abort(409, description="仅可为未归档作业的当前退回版本调整重交期限。")
    reason = request.form.get("reason", "").strip()
    try:
        deadline = datetime.fromisoformat(request.form.get("redo_due_at", ""))
    except ValueError:
        deadline = None
    if not 1 <= len(reason) <= 2000:
        flash("请填写 1-2000 字的延期原因。", "error")
    elif not deadline or deadline.tzinfo is not None or deadline <= beijing_now() or (submission.redo_due_at and deadline <= submission.redo_due_at):
        flash("新期限必须晚于当前时间和原重交期限。", "error")
    else:
        record_review(submission, current_user.id, "EXTEND", deadline=deadline, reason=reason)
        submission.redo_due_at = deadline
        notify(submission.student_id, assignment, "重交期限延长", f"{assignment.title}：延期至 {deadline:%Y-%m-%d %H:%M}。原因：{reason}")
        db.session.commit()
        flash("重交期限已延长，学生已收到通知。", "success")
    return redirect(url_for("assignment_detail", id=assignment.id))


@final_stage.route("/assignment/<int:id>/edit", methods=["POST"])
@login_required
def edit_assignment(id):
    assignment = owned_assignment(id)
    if assignment.archived:
        abort(409, description="请先恢复归档作业，再修改内容。")
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    raw = request.form.get("due_at", "")
    try:
        due_at = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        due_at = "invalid"
    if isinstance(due_at, datetime) and assignment.due_at and due_at == assignment.due_at.replace(microsecond=0):
        due_at = assignment.due_at
    if not 1 <= len(title) <= 200 or not 1 <= len(description) <= 20000:
        flash("标题需为 1-200 字，作业要求需为 1-20000 字。", "error")
    elif due_at == "invalid" or (due_at and due_at.tzinfo is not None):
        flash("请填写有效的北京时间。", "error")
    elif due_at != assignment.due_at and (due_at is None or due_at <= beijing_now()):
        flash("修改后的截止时间必须晚于当前时间，不能清空已有截止时间。", "error")
    elif title == assignment.title and description == assignment.description and due_at == assignment.due_at:
        flash("作业内容没有变化。", "info")
    else:
        assignment.title, assignment.description, assignment.due_at = title, description, due_at
        for enrollment in assignment.teaching_class.enrollments:
            notify(enrollment.student_id, assignment, "作业信息更新", f"{title}：教师更新了作业内容或截止时间，请查看最新要求。")
        db.session.commit()
        flash("作业信息已更新，并已通知班级成员。", "success")
    return redirect(url_for("assignment_detail", id=id))


@final_stage.route("/assignment/<int:id>/archive", methods=["POST"])
@login_required
def archive_assignment(id):
    assignment = owned_assignment(id)
    action = request.form.get("action")
    if action not in {"archive", "restore"}:
        abort(400, description="请选择归档或恢复操作。")
    target = action == "archive"
    if assignment.archived != target:
        assignment.archived = target
        for enrollment in assignment.teaching_class.enrollments:
            notify(enrollment.student_id, assignment, "作业已归档" if target else "作业已恢复",
                   f"{assignment.title}：{'已归档，保留附件与成绩，停止提交和批改。' if target else '已恢复，请按截止时间完成作业。'}")
        db.session.commit()
    flash("作业已归档，历史数据保留。" if target else "作业已恢复。", "success")
    return redirect(url_for("assignment_detail", id=id))


@final_stage.route("/gradebook")
@login_required
def gradebook():
    if current_user.role != "TEACHER":
        abort(403)
    courses = Course.query.filter_by(teacher_id=current_user.id).order_by(Course.id).all()
    course_id = request.args.get("course_id", type=int)
    if course_id:
        course = db.get_or_404(Course, course_id)
        if course.teacher_id != current_user.id:
            abort(403)
    classes_query = TeachingClass.query.filter_by(teacher_id=current_user.id)
    if course_id:
        classes_query = classes_query.filter_by(course_id=course_id)
    classes = classes_query.order_by(TeachingClass.id).all()
    class_id = request.args.get("class_id", type=int)
    selected = owned_class(class_id) if class_id else (classes[0] if classes else None)
    if selected and course_id and selected.course_id != course_id:
        abort(400, description="所选教学班不属于该课程，请重新筛选。")
    assignment_id = request.args.get("assignment_id", type=int)
    assignments, rows = class_report(selected, assignment_id) if selected else ([], [])
    all_assignments = Assignment.query.filter_by(class_id=selected.id).order_by(Assignment.id).all() if selected else []
    statistics = [assignment_statistics(rows, assignment) for assignment in assignments]
    return render_template("gradebook.html", courses=courses, classes=classes, selected=selected,
                           course_id=course_id, assignments=assignments, all_assignments=all_assignments,
                           assignment_id=assignment_id, rows=rows, statistics=statistics)


def csv_cell(value):
    text = str(value)
    # Excel interprets these prefixes as formulas, even inside quoted CSV cells.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@final_stage.route("/class/<int:class_id>/grades.csv")
@login_required
def export_grades(class_id):
    teaching_class = owned_class(class_id)
    assignment_id = request.args.get("assignment_id", type=int)
    assignments, rows = class_report(teaching_class, assignment_id)
    labels = {"PENDING": "待提交", "OVERDUE": "已逾期", "TO_GRADE": "待批改", "RETURNED": "退回重做", "GRADED": "已评分"}
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    header = ["学生", "课程", "教学班"]
    for assignment in assignments:
        header.extend([f"{assignment.title} · 状态", f"{assignment.title} · 成绩"])
    header.extend(["姓名", "学号", "已交作业数", "已评分平均分"])
    writer.writerow([csv_cell(cell) for cell in header])
    for row in rows:
        values = [row["student"].username, teaching_class.course.name, teaching_class.name]
        for cell in row["cells"]:
            values.extend([labels[cell["status"]], cell["score"] if cell["score"] is not None else ""])
        values.extend([row["student"].real_name, row["student"].student_number or "", row["submitted"], row["average"] if row["average"] is not None else ""])
        writer.writerow([csv_cell(value) for value in values])
    return Response("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="class-{class_id}-grades.csv"'})


@final_stage.route("/my-grades")
@login_required
def my_grades():
    if current_user.role != "STUDENT":
        abort(403)
    enrollments = Enrollment.query.filter_by(student_id=current_user.id).all()
    courses = Course.query.join(TeachingClass).join(Enrollment).filter(
        Enrollment.student_id == current_user.id).distinct().order_by(Course.id).all()
    course_id = request.args.get("course_id", type=int)
    if course_id and course_id not in {c.id for c in courses}:
        abort(403)
    reports = []
    for course in courses:
        if course_id and course.id != course_id:
            continue
        cells = []
        for enrollment in enrollments:
            if enrollment.teaching_class.course_id != course.id:
                continue
            assignments = Assignment.query.filter_by(class_id=enrollment.class_id).order_by(Assignment.id).all()
            row = next(row for row in grade_rows(enrollment.teaching_class, assignments, current_user.id)
                       if row["student"].id == current_user.id)
            cells.extend(row["cells"])
        scores = [cell["score"] for cell in cells if cell["score"] is not None]
        graded = sorted([c for c in cells if c["score"] is not None], key=lambda c: (
            c["submission"].reviewed_at or c["submission"].created_at, c["submission"].id))
        reports.append({"course": course, "cells": cells, "graded": graded,
                        "average": round(sum(scores) / len(scores), 2) if scores else None})
    return render_template("my_grades.html", reports=reports, courses=courses, course_id=course_id)


@final_stage.route("/notifications")
@login_required
def notifications():
    unread_only = request.args.get("unread") == "1"
    query = Notification.query.filter_by(user_id=current_user.id)
    if unread_only:
        query = query.filter_by(is_read=False)
    page = query.order_by(Notification.id.desc()).paginate(per_page=20, error_out=False)
    return render_template("notifications.html", page=page, unread_only=unread_only)


@final_stage.route("/notifications/<int:id>/read", methods=["POST"])
@login_required
def read_notification(id):
    notification = db.get_or_404(Notification, id)
    if notification.user_id != current_user.id:
        abort(403)
    notification.is_read = True
    db.session.commit()
    return redirect(url_for("assignment_detail", id=notification.assignment_id))


@final_stage.route("/notifications/read-all", methods=["POST"])
@login_required
def read_all_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    flash("所有消息已标为已读。", "success")
    return redirect(url_for("final_stage.notifications"))
