"""Repeatable isolated scale checks and a real Waitress HTTP smoke test."""
import io
import json
import logging
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from datetime import timedelta

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
target = Path(tempfile.mkdtemp(prefix='performance-', dir=root/'artifacts'))
os.environ['HOMEWORK_DATABASE_URI'] = f"sqlite:///{(target/'test.db').as_posix()}"
os.environ['HOMEWORK_UPLOAD_FOLDER'] = str(target/'uploads')
os.environ['HOMEWORK_LOG_DIR'] = str(target/'logs')
from app import app
from models import Assignment, Course, Enrollment, Submission, TeachingClass, User, db, beijing_now
from sqlalchemy import event, insert

app.logger.setLevel(logging.WARNING)
with app.app_context():
    teacher = User(username='scale-teacher', password='not-a-login-account', role='TEACHER')
    db.session.add(teacher)
    db.session.flush()
    teacher_id = teacher.id
    course = Course(name='Scale course', teacher_id=teacher_id)
    db.session.add(course)
    db.session.flush()
    group = TeachingClass(name='100 students', join_code='SCALE123', course_id=course.id, teacher_id=teacher_id)
    db.session.add(group)
    db.session.flush()
    class_id = group.id
    students = [User(username=f'scale-student-{i}', role='STUDENT', password='not-a-login-account') for i in range(100)]
    db.session.add_all(students)
    db.session.flush()
    student_ids = [s.id for s in students]
    db.session.add_all([Enrollment(class_id=class_id, student_id=s.id) for s in students])
    assignments = [Assignment(title=f'Task {i}', description='Scale fixture', teacher_id=teacher_id,
        class_id=class_id, course_id=course.id, due_at=beijing_now()+timedelta(days=1)) for i in range(50)]
    db.session.add_all(assignments)
    db.session.flush()
    assignment_id = assignments[0].id
    rows = [{'assignment_id':a.id,'student_id':s.id,'file_path':'fixture.txt','original_name':'fixture.txt',
             'score':80 if version==2 else None,'review_state':'GRADED' if version==2 else 'PENDING'}
            for a in assignments for s in students for version in range(3)]
    db.session.execute(insert(Submission), rows)
    db.session.commit()
    (target/'uploads'/'fixture.txt').write_text('scale fixture', encoding='utf-8')
    engine = db.engine
    db.session.remove()

metrics = []
for user_id, route in [(teacher_id,'/dashboard'),(teacher_id,'/assignment/list'),
    (teacher_id,f'/assignment/detail/{assignment_id}'),(teacher_id,f'/gradebook?class_id={class_id}'),
    (student_ids[0],'/my-grades')]:
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
        session['_fresh'] = True
    durations, counts = [], []
    for _ in range(3):
        statements = []
        def count(*args):
            statements.append(1)
        event.listen(engine, 'before_cursor_execute', count)
        started = time.perf_counter()
        try:
            response = client.get(route)
        finally:
            durations.append((time.perf_counter()-started)*1000)
            event.remove(engine, 'before_cursor_execute', count)
        assert response.status_code == 200, (route, response.status_code)
        counts.append(len(statements))
    metrics.append({'route':route,'median_ms':round(statistics.median(durations),2),
        'sql_queries':counts[-1],'response_bytes':len(response.data)})

with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
environment = {**os.environ,'PORT':str(port),'HOST':'127.0.0.1'}
with (target/'waitress.log').open('w', encoding='utf-8') as log:
    process = subprocess.Popen([sys.executable, str(root/'serve.py')], cwd=root, env=environment,
        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    try:
        for attempt in range(60):
            try:
                with urlopen(f'http://127.0.0.1:{port}/login', timeout=2) as response:
                    assert response.status == 200
                    assert response.headers['X-Content-Type-Options'] == 'nosniff'
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError('Waitress exited; inspect waitress.log')
                if attempt == 59:
                    raise
                time.sleep(0.2)
    finally:
        process.terminate()
        process.wait(timeout=10)
report = {'students':100,'assignments':50,'submission_versions':15000,'samples_per_route':3,
    'metrics':metrics,'waitress_http':'passed','scope':'Local sequential smoke measurements; not a concurrency capacity benchmark.'}
(target/'performance-results.json').write_text(json.dumps(report,indent=2), encoding='utf-8')
print(json.dumps({**report,'directory':str(target)},indent=2))
