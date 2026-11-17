"""Shared rules: one effective submission per student and assignment."""
from sqlalchemy import exists, or_, case, func
from sqlalchemy.orm import aliased, joinedload

from models import Assignment, Enrollment, Notification, ReviewEvent, Submission, beijing_now, db


def effective_submissions():
    newer = aliased(Submission)
    return Submission.query.filter(~exists().where(
        newer.assignment_id == Submission.assignment_id,
        newer.student_id == Submission.student_id,
        newer.id > Submission.id,
    ))


def latest_submission(assignment_id, student_id):
    return Submission.query.filter_by(assignment_id=assignment_id, student_id=student_id).order_by(
        Submission.id.desc()).first()


def assignment_status(assignment, student_id):
    submission = latest_submission(assignment.id, student_id)
    return submission_status(assignment, submission)


def submission_status(assignment, submission):
    if submission:
        if submission.review_state == "RETURNED":
            return "RETURNED"
        return "GRADED" if submission.score is not None else "TO_GRADE"
    return "OVERDUE" if assignment.due_at and assignment.due_at <= beijing_now() else "PENDING"


def submission_deadline(assignment, submission):
    if submission and submission.redo_due_at:
        if assignment.due_at is None:
            return None
        return max(assignment.due_at, submission.redo_due_at)
    return assignment.due_at


def can_submit(assignment, submission):
    deadline = submission_deadline(assignment, submission)
    return teaching_open(assignment) and (deadline is None or deadline > beijing_now())


def teaching_open(assignment):
    return not assignment.archived and (not assignment.course or not assignment.course.archived) and (
        not assignment.teaching_class or not assignment.teaching_class.archived)


def deadline_label(assignment, submission):
    deadline = submission_deadline(assignment, submission)
    return deadline.strftime("%Y-%m-%d %H:%M（北京时间）") if deadline else "不限时"


def assignment_cards(assignments, student_id=None):
    assignments = list(assignments)
    if not assignments:
        return []
    query = effective_submissions().filter(Submission.assignment_id.in_([a.id for a in assignments]))
    if student_id is None:
        totals = {row[0]: row for row in query.with_entities(Submission.assignment_id, func.count(Submission.id),
            func.sum(case(((Submission.score.is_(None)) & (Submission.review_state != "RETURNED"), 1), else_=0))
            ).group_by(Submission.assignment_id).all()}
        return [{"assignment": assignment, "submission_count": totals.get(assignment.id, (None, 0, 0))[1],
                 "pending_count": totals.get(assignment.id, (None, 0, 0))[2]} for assignment in assignments]
    if student_id is not None:
        query = query.filter(Submission.student_id == student_id)
    grouped = {}
    for submission in query.all():
        grouped.setdefault(submission.assignment_id, []).append(submission)
    cards = []
    for assignment in assignments:
        submissions = grouped.get(assignment.id, [])
        if student_id is not None:
            submission = submissions[0] if submissions else None
            cards.append({"assignment": assignment, "submission": submission, "status": submission_status(assignment, submission)})
        else:
            cards.append({"assignment": assignment, "submission_count": len(submissions),
                          "pending_count": sum(s.score is None and s.review_state != "RETURNED" for s in submissions)})
    return cards


def filter_assignment_status(query, status, user):
    if not status:
        return query
    effective = effective_submissions().filter(Submission.assignment_id == Assignment.id)
    if user.role == "TEACHER":
        return query.filter(effective.filter(Submission.score.is_(None), Submission.review_state != "RETURNED").exists()) if status == "TO_GRADE" else query.filter(db.false())
    effective = effective.filter(Submission.student_id == user.id)
    if status == "SUBMITTED":
        return query.filter(effective.exists())
    if status in {"GRADED", "TO_GRADE", "RETURNED"}:
        if status == "RETURNED":
            effective = effective.filter(Submission.review_state == "RETURNED")
        else:
            effective = effective.filter(Submission.review_state != "RETURNED",
                Submission.score.is_not(None) if status == "GRADED" else Submission.score.is_(None))
        return query.filter(effective.exists())
    if status == "OVERDUE":
        return query.filter(~effective.exists(), Assignment.due_at <= beijing_now())
    if status == "PENDING":
        return query.filter(~effective.exists(), or_(Assignment.due_at.is_(None), Assignment.due_at > beijing_now()))
    return query.filter(db.false())


def record_review(submission, teacher_id, action, *, score=None, comment=None, deadline=None, reason=None):
    db.session.add(ReviewEvent(submission_id=submission.id, teacher_id=teacher_id, action=action,
        old_score=submission.score, new_score=score if action == "GRADE" else submission.score,
        old_comment=submission.comment, new_comment=comment if action == "GRADE" else submission.comment,
        old_deadline=submission.redo_due_at, new_deadline=deadline if deadline is not None else submission.redo_due_at,
        reason=reason))


def notify(user_id, assignment, title, message):
    db.session.add(Notification(user_id=user_id, assignment_id=assignment.id, title=title, message=message))


def notify_assignment(assignment, student_id=None):
    recipients = [student_id] if student_id else [e.student_id for e in
        Enrollment.query.filter_by(class_id=assignment.class_id, active=True).all()]
    for recipient in recipients:
        notify(recipient, assignment, "新作业发布", f"{assignment.title}：请查看作业要求与截止时间。")


def grade_rows(teaching_class, assignments, student_id=None, *, enrollments=None):
    assignment_ids = [a.id for a in assignments]
    query = effective_submissions().filter(Submission.assignment_id.in_(assignment_ids))
    if student_id is not None:
        query = query.filter(Submission.student_id == student_id)
    if enrollments is not None:
        enrollments = list(enrollments)
        query = query.filter(Submission.student_id.in_([e.student_id for e in enrollments]))
    submissions = query.all()
    lookup = {(s.student_id, s.assignment_id): s for s in submissions}
    rows = []
    if enrollments is None:
        query = Enrollment.query.options(joinedload(Enrollment.student)).filter_by(class_id=teaching_class.id, active=True)
        if student_id is not None:
            query = query.filter_by(student_id=student_id)
        enrollments = query.order_by(Enrollment.student_id).all()
    for enrollment in enrollments:
        if student_id is not None and enrollment.student_id != student_id:
            continue
        cells = []
        scores = []
        for assignment in assignments:
            submission = lookup.get((enrollment.student_id, assignment.id))
            status = submission_status(assignment, submission)
            score = submission.score if status == "GRADED" else None
            if score is not None:
                scores.append(score)
            cells.append({"assignment": assignment, "submission": submission, "status": status, "score": score})
        rows.append({"student": enrollment.student, "cells": cells,
                     "average": round(sum(scores) / len(scores), 2) if scores else None,
                     "submitted": sum(c["submission"] is not None for c in cells)})
    return rows


def assignment_statistics(rows, assignment):
    cells = [c for row in rows for c in row["cells"] if c["assignment"].id == assignment.id]
    scores = [c["score"] for c in cells if c["score"] is not None]
    submitted = sum(c["submission"] is not None for c in cells)
    distribution = [{"label": label, "count": sum(low <= score <= high for score in scores)}
                    for label, low, high in [("0–59", 0, 59), ("60–69", 60, 69),
                                              ("70–79", 70, 79), ("80–89", 80, 89), ("90–100", 90, 100)]]
    return {"assignment": assignment, "members": len(cells), "submitted": submitted,
            "missing": len(cells) - submitted, "graded": len(scores),
            "returned": sum(c["status"] == "RETURNED" for c in cells),
            "rate": round(submitted / len(cells) * 100, 1) if cells else 0,
            "average": round(sum(scores) / len(scores), 2) if scores else None,
            "highest": max(scores) if scores else None, "lowest": min(scores) if scores else None,
            "distribution": distribution}


def class_statistics(teaching_class, assignments):
    """Aggregate whole-class statistics without constructing a student/assignment matrix."""
    members = Enrollment.query.filter_by(class_id=teaching_class.id, active=True).count()
    graded = (Submission.score.is_not(None)) & (Submission.review_state != "RETURNED")
    score = case((graded, Submission.score), else_=None)
    buckets = [("0–59", 0, 59), ("60–69", 60, 69), ("70–79", 70, 79), ("80–89", 80, 89), ("90–100", 90, 100)]
    query = effective_submissions().join(Enrollment, (Enrollment.student_id == Submission.student_id) &
        (Enrollment.class_id == teaching_class.id) & Enrollment.active.is_(True)).filter(
        Submission.assignment_id.in_([a.id for a in assignments]))
    aggregates = query.with_entities(Submission.assignment_id, func.count(Submission.id), func.count(score),
        func.avg(score), func.max(score), func.min(score),
        func.sum(case((Submission.review_state == "RETURNED", 1), else_=0)),
        *[func.sum(case((graded & Submission.score.between(low, high), 1), else_=0)) for _, low, high in buckets]
        ).group_by(Submission.assignment_id).all()
    lookup = {row[0]: row for row in aggregates}
    result = []
    for assignment in assignments:
        row = lookup.get(assignment.id, (assignment.id, 0, 0, None, None, None, 0, 0, 0, 0, 0, 0))
        result.append({"assignment": assignment, "members": members, "submitted": row[1],
            "missing": members-row[1], "graded": row[2], "average": round(row[3], 2) if row[3] is not None else None,
            "highest": row[4], "lowest": row[5], "returned": row[6], "rate": round(row[1]/members*100, 1) if members else 0,
            "distribution": [{"label": label, "count": row[7+i]} for i, (label, _, _) in enumerate(buckets)]})
    return result
