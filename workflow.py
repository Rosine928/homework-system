"""Shared rules: one effective submission per student and assignment."""
from sqlalchemy import exists, or_
from sqlalchemy.orm import aliased

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
    return not assignment.archived and (deadline is None or deadline > beijing_now())


def assignment_cards(assignments, student_id=None):
    assignments = list(assignments)
    if not assignments:
        return []
    query = effective_submissions().filter(Submission.assignment_id.in_([a.id for a in assignments]))
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
        Enrollment.query.filter_by(class_id=assignment.class_id).all()]
    for recipient in recipients:
        notify(recipient, assignment, "新作业发布", f"{assignment.title}：请查看作业要求与截止时间。")


def grade_rows(teaching_class, assignments, student_id=None):
    assignment_ids = [a.id for a in assignments]
    query = effective_submissions().filter(Submission.assignment_id.in_(assignment_ids))
    if student_id is not None:
        query = query.filter(Submission.student_id == student_id)
    submissions = query.all()
    lookup = {(s.student_id, s.assignment_id): s for s in submissions}
    rows = []
    for enrollment in sorted(teaching_class.enrollments, key=lambda e: (e.student.username, e.student_id)):
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
