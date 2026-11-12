"""Account routes and student identity validation."""
import re

from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from models import User, db


def identity_fields(role):
    name = request.form.get("real_name", "").strip()
    number = request.form.get("student_number", "").strip() if role == "STUDENT" else None
    if len(name) > 40 or any(ord(c) < 32 for c in name):
        return None, None, "姓名最多 40 个字符，不能包含控制字符。"
    if role == "STUDENT" and (not name or not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", number or "")):
        return None, None, "请填写姓名和 2-32 位学号（字母、数字、下划线或短横线）。"
    return name, number or None, None


def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "STUDENT")
        name, number, error = identity_fields(role)
        if not 2 <= len(username) <= 40 or not 6 <= len(password) <= 128:
            flash("用户名需为 2-40 个字符，密码需为 6-128 位。", "error")
        elif role not in {"STUDENT", "TEACHER"}:
            flash("请选择有效的账号角色。", "error")
        elif error:
            flash(error, "error")
        elif User.query.filter_by(username=username).first():
            flash("该用户名已被使用。", "error")
        elif number and User.query.filter_by(student_number=number).first():
            flash("该学号已被使用。", "error")
        else:
            db.session.add(User(username=username, password=generate_password_hash(password), role=role,
                                real_name=name, student_number=number))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("用户名或学号已被使用。", "error")
            else:
                flash("注册成功，请登录。", "success")
                return redirect(url_for("login"))
        return render_template("register.html"), 400
    return render_template("register.html")


def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        password = request.form.get("password", "")
        hashed = user and user.password.startswith(("scrypt:", "pbkdf2:"))
        if user and len(password) <= 128 and (check_password_hash(user.password, password) if hashed else user.password == password):
            if not hashed:
                user.password = generate_password_hash(password)
                db.session.commit()
            session.clear()
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("用户名或密码不正确。", "error")
        return redirect(url_for("login"))
    return render_template("login.html")


@login_required
def logout():
    logout_user()
    session.clear()
    flash("已安全退出。", "success")
    return redirect(url_for("login"))


@login_required
def profile():
    if request.method == "POST":
        name, number, error = identity_fields(current_user.role)
        if error:
            flash(error, "error")
        elif number and User.query.filter(User.student_number == number, User.id != current_user.id).first():
            flash("该学号已被使用，请核对。", "error")
        else:
            current_user.real_name = name
            current_user.student_number = number
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("该学号已被使用，请核对。", "error")
            else:
                flash("个人资料已保存。", "success")
                return redirect(url_for("profile"))
        return render_template("profile.html"), 400
    return render_template("profile.html")


def register_account_routes(app):
    for path, view, methods in (("/register", register, ["GET", "POST"]), ("/login", login, ["GET", "POST"]),
                                ("/logout", logout, ["POST"]), ("/profile", profile, ["GET", "POST"])):
        app.add_url_rule(path, view_func=view, methods=methods)
