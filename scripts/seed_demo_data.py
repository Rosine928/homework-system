"""Append a backed-up, repeat-safe demonstration dataset to the configured database."""
from datetime import timedelta
import json
from pathlib import Path
from secrets import token_urlsafe
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app
from models import (AccountEvent, Assignment, Course, Enrollment, Notification,
                    ReviewEvent, Submission, TeachingClass, User, beijing_now, db)
from werkzeug.security import generate_password_hash
from workflow import assignment_status

BATCH = 'classroom-demo-2026-v1'
MODELS = (User, Course, TeachingClass, Enrollment, Assignment, Submission, Notification, ReviewEvent)


def seed():
    with app.app_context():
        imported = AccountEvent.query.filter_by(action='DEMO_DATA_IMPORTED', details=BATCH).first()
        if imported:
            print(json.dumps({'status': 'already_imported', 'batch': BATCH}, ensure_ascii=False))
            return
        # Run the application's verified full database-and-attachments backup first.
        db.session.remove()
        backup = ROOT / 'backups' / ('before-demo-' + beijing_now().strftime('%Y%m%d-%H%M%S-%f'))
        result = app.test_cli_runner().invoke(args=['backup', '--destination', str(backup)])
        if result.exit_code:
            raise RuntimeError(result.output) from result.exception
        previous = {model: model.query.count() for model in MODELS}
        original_teachers = User.query.filter_by(role='TEACHER', enabled=True).order_by(User.id).all()
        original_students = User.query.filter_by(role='STUDENT', enabled=True).order_by(User.id).all()
        teacher_names = ['demo-teacher01', 'demo-teacher02']
        if not original_teachers:
            teacher_names.append('demo-teacher03')
        student_names = [f'demo-student{i:02}' for i in range(1, 25)]
        if User.query.filter(User.username.in_(teacher_names + student_names)).first():
            raise RuntimeError('Demo usernames already exist; nothing has been overwritten.')
        password = token_urlsafe(12)
        now = beijing_now().replace(microsecond=0)
        files = []
        committed = False
        try:
            teachers = list(original_teachers[:1])
            for i, username in enumerate(teacher_names):
                user = User(username=username, real_name=['陈老师（演示）', '王老师（演示）', '张老师（演示）'][i],
                            role='TEACHER', password=generate_password_hash(password))
                db.session.add(user)
                teachers.append(user)
            names = ['林晨', '陈雨', '王浩', '李欣', '张宇', '刘悦', '赵明', '周宁',
                     '吴桐', '郑晓', '孙睿', '徐佳', '马涛', '朱琳', '胡杰', '郭晴',
                     '何轩', '高远', '罗敏', '梁辰', '宋妍', '唐磊', '许诺', '邓思']
            students = []
            for index, username in enumerate(student_names):
                student = User(username=username, real_name=names[index] + '（演示）',
                               student_number='DEMO-' + uuid4().hex[:16], role='STUDENT',
                               password=generate_password_hash(password))
                db.session.add(student)
                students.append(student)
            db.session.flush()
            uploads = Path(app.config['UPLOAD_FOLDER'])

            def attachment(content):
                name = 'demo_' + uuid4().hex + '.txt'
                path = uploads / name
                with path.open('x', encoding='utf-8') as handle:
                    files.append(path)
                    handle.write(content)
                return name

            themes = [
                ('大数据 Web 应用开发（演示）', ['Flask 路由与模板实验', '数据库 CRUD 实验', '用户认证与权限设计', '课程作业管理综合实践']),
                ('Python 数据分析（演示）', ['缺失值与异常值处理', 'Pandas 分组聚合', '数据可视化报告', '校园学习数据分析']),
                ('数据库原理与应用（演示）', ['关系模型与建表', '多表关联查询', '事务与索引分析', '成绩管理数据库设计']),
            ]
            created_assignments = []
            for course_index, (name, titles) in enumerate(themes):
                teacher = teachers[course_index]
                course = Course(name=name, description='虚构教学演示数据，用于查看作业状态、成绩统计、历史版本和通知。',
                                teacher_id=teacher.id, created_at=now - timedelta(days=30))
                db.session.add(course)
                db.session.flush()
                for cohort in range(2):
                    join_code = uuid4().hex[:8].upper()
                    while TeachingClass.query.filter_by(join_code=join_code).first():
                        join_code = uuid4().hex[:8].upper()
                    classroom = TeachingClass(name=f'2026 级演示 {cohort + 1} 班', join_code=join_code,
                        course_id=course.id, teacher_id=teacher.id, created_at=now - timedelta(days=28))
                    db.session.add(classroom)
                    db.session.flush()
                    members = students[cohort * 12:(cohort + 1) * 12]
                    if cohort == 0:
                        members = members + original_students
                    for student in members:
                        db.session.add(Enrollment(class_id=classroom.id, student_id=student.id,
                                                  joined_at=now - timedelta(days=25)))
                    for task_index, title in enumerate(titles):
                        due = now + timedelta(days=[2, 5, -2, -5][task_index])
                        assignment = Assignment(title=title + '（演示）', teacher_id=teacher.id, course_id=course.id,
                            class_id=classroom.id, due_at=due,
                            description='请完成实验，提交实验步骤、关键代码、运行结果及不少于 200 字的分析。\n本任务为系统演示样例。',
                            rubric='功能正确性 40 分；分析说明 30 分；代码规范 20 分；文档完整性 10 分。',
                            reference_path=attachment(f'{title}\n实验目标：掌握基本概念并完成实践。\n提交要求：步骤、代码、结果与总结。\n'),
                            reference_name=title + '_实验指导.txt')
                        db.session.add(assignment)
                        db.session.flush()
                        created_assignments.append(assignment)
                        submitted_at = min(now - timedelta(days=1), due - timedelta(days=2))
                        for member_index, student in enumerate(members):
                            db.session.add(Notification(user_id=student.id, assignment_id=assignment.id,
                                title='新作业发布', message=f'{assignment.title}：请查看要求和截止时间。',
                                created_at=submitted_at - timedelta(days=2), is_read=member_index % 3 == 0))
                            # Every student sees all five workflow states across the dataset.
                            state = (member_index + task_index + course_index) % 5
                            if state == 0:
                                continue
                            # Include a previous reviewed version on selected submissions.
                            if member_index % 4 == 0:
                                old = Submission(assignment_id=assignment.id, student_id=student.id,
                                    file_path=attachment(f'{title}\n{student.display_name}\n初稿：包含实验步骤，分析有待补充。\n'),
                                    original_name=f'{student.username}_初稿.txt', created_at=submitted_at - timedelta(hours=8),
                                    review_state='RETURNED', return_reason='请补充结果分析后重新提交。',
                                    reviewed_at=submitted_at - timedelta(hours=6), redo_due_at=due + timedelta(days=1))
                                db.session.add(old)
                                db.session.flush()
                                db.session.add(ReviewEvent(submission_id=old.id, teacher_id=teacher.id, action='RETURN',
                                    reason=old.return_reason, new_deadline=old.redo_due_at, created_at=old.reviewed_at))
                            score = ([0, 55, 64, 73, 82, 91, 100][(member_index + task_index) % 7] if state in (1, 2) else None)
                            reviewed = submitted_at + timedelta(hours=3)
                            submission = Submission(assignment_id=assignment.id, student_id=student.id,
                                file_path=attachment(f'{title}\n提交人：{student.display_name}\n一、实验步骤\n已完成数据准备、实现与结果检查。\n二、结果分析\n针对实验结果说明实现过程，并提出改进方向。\n'),
                                original_name=f'{student.username}_{title}.txt', created_at=submitted_at,
                                review_state='GRADED' if score is not None else ('RETURNED' if state == 4 else 'PENDING'),
                                score=score, comment='已完成主要要求，请继续完善分析细节。' if score is not None else None,
                                reviewed_at=reviewed if state != 3 else None,
                                return_reason='请补充数据来源、运行截图与结果分析。' if state == 4 else None,
                                redo_due_at=now + timedelta(days=3) if state == 4 else None)
                            db.session.add(submission)
                            db.session.flush()
                            if state != 3:
                                db.session.add(ReviewEvent(submission_id=submission.id, teacher_id=teacher.id,
                                    action='GRADE' if score is not None else 'RETURN', new_score=score,
                                    new_comment=submission.comment, reason=submission.return_reason,
                                    new_deadline=submission.redo_due_at, created_at=reviewed))
                                db.session.add(Notification(user_id=student.id, assignment_id=assignment.id,
                                    title='作业已评分' if score is not None else '作业需重做',
                                    message=f'{assignment.title}：' + (f'得分 {score}，请查看反馈。' if score is not None else submission.return_reason),
                                    created_at=reviewed))
            db.session.add(AccountEvent(user_id=teachers[0].id, action='DEMO_DATA_IMPORTED', details=BATCH))
            db.session.commit()
            committed = True
            statuses = {}
            for assignment in created_assignments:
                status = assignment_status(assignment, students[0].id)
                # Only count classes this student actually attends.
                if Enrollment.query.filter_by(class_id=assignment.class_id, student_id=students[0].id).first():
                    statuses[status] = statuses.get(status, 0) + 1
            report = {'status': 'imported', 'batch': BATCH, 'backup': str(backup),
                'added': {m.__tablename__: m.query.count() - previous[m] for m in MODELS},
                'student_example_states': statuses, 'new_files': len(files),
                'teacher_accounts': teacher_names, 'student_accounts': student_names,
                'existing_teacher_id': teachers[0].id, 'existing_student_ids': [s.id for s in original_students]}
            output = ROOT / 'artifacts' / 'demo-data-import.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({**report, 'new_accounts_password': password}, ensure_ascii=False))
        except Exception:
            db.session.rollback()
            if not committed:
                for path in files:
                    path.unlink(missing_ok=True)
            raise


if __name__ == '__main__':
    seed()
