import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from models import db, User, Assignment, Submission

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
# 使用 SQLite 数据库，无需安装 MySQL，文件会自动生成
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///homework.db'
app.config['UPLOAD_FOLDER'] = 'uploads'

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all() # 第一次运行自动建表

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('assignment_list'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:
            login_user(user)
            return redirect(url_for('assignment_list'))
        flash('用户名或密码错误')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        role = request.form['role']
        if User.query.filter_by(username=username).first():
            flash('用户名已存在')
            return redirect(url_for('register'))
        db.session.add(User(username=username, password=password, role=role))
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/assignment/list')
@login_required
def assignment_list():
    assignments = Assignment.query.all()
    return render_template('assignment_list.html', assignments=assignments)


# ---------- 新增路由（作业发布、详情、上传、打分） ----------

# 1. 老师发布作业（表单提交）
@app.route('/assignment/create', methods=['POST'])
@login_required
def create_assignment():
    if current_user.role != 'TEACHER':
        return "只有老师才能发布作业", 403
    title = request.form['title']
    description = request.form['description']
    db.session.add(Assignment(title=title, description=description, teacher_id=current_user.id))
    db.session.commit()
    flash('作业发布成功！', 'success')
    return redirect(url_for('assignment_list'))


# 2. 查看作业详情（包含上传和打分功能）
@app.route('/assignment/detail/<int:id>')
@login_required
def assignment_detail(id):
    assignment = Assignment.query.get_or_404(id)
    # 如果是学生，查询自己的提交记录；如果是老师，查询所有提交记录
    if current_user.role == 'STUDENT':
        submissions = Submission.query.filter_by(assignment_id=id, student_id=current_user.id).all()
    else:
        submissions = Submission.query.filter_by(assignment_id=id).all()
    return render_template('assignment_detail.html', assignment=assignment, submissions=submissions)


# 3. 学生上传作业文件
@app.route('/assignment/submit', methods=['POST'])
@login_required
def submit_homework():
    if current_user.role != 'STUDENT':
        return "只有学生才能提交作业", 403
    file = request.files['file']
    assignment_id = request.form['assignment_id']

    if file and file.filename:
        # 安全处理文件名（防止中文乱码及路径穿越）
        filename = f"{int(time.time())}_{file.filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        sub = Submission(
            assignment_id=assignment_id,
            student_id=current_user.id,
            file_path=filepath,
            original_name=file.filename
        )
        db.session.add(sub)
        db.session.commit()
        flash('作业提交成功！', 'success')
    return redirect(url_for('assignment_detail', id=assignment_id))


# 4. 老师打分
@app.route('/assignment/grade', methods=['POST'])
@login_required
def grade_homework():
    if current_user.role != 'TEACHER':
        return "只有老师才能打分", 403
    submission_id = request.form['submission_id']
    score = int(request.form['score'])
    comment = request.form['comment']

    sub = Submission.query.get_or_404(submission_id)
    sub.score = score
    sub.comment = comment
    db.session.commit()
    flash('评分提交成功！', 'success')
    return redirect(url_for('assignment_detail', id=sub.assignment_id))

if __name__ == '__main__':
    if not os.path.exists('uploads'):
        os.makedirs('uploads')
    app.run(debug=True, port=5000)