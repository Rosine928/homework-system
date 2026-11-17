"""Account routes and student identity validation."""
import re
import hmac
import json
from hashlib import sha256
from datetime import timedelta

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from models import User, AccountEvent, LoginAttempt, beijing_now, db


def password_matches(user, password):
    if not 1 <= len(password) <= 128:
        return False
    if user.password.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(user.password, password)
    return hmac.compare_digest(user.password.encode("utf-8"), password.encode("utf-8"))


def account_event(user, action, details=None):
    db.session.add(AccountEvent(user_id=user.id, action=action, details=json.dumps(details or {}, ensure_ascii=False)))


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
        if not 2 <= len(username) <= 40 or not 8 <= len(password) <= 128:
            flash("用户名需为 2-40 个字符，密码需为 8-128 位。", "error")
        elif role not in {"STUDENT", "TEACHER"}:
            flash("请选择有效的账号角色。", "error")
        elif role == "TEACHER" and (not current_app.config["TEACHER_REGISTRATION_CODE"] or not hmac.compare_digest(
            request.form.get("teacher_code", "").encode("utf-8"), current_app.config["TEACHER_REGISTRATION_CODE"].encode("utf-8"))):
            flash("教师注册需要有效邀请码，请联系系统维护人员。", "error")
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
        username = request.form.get("username", "").strip()
        now = beijing_now()
        keys = [sha256(value.encode("utf-8")).hexdigest() for value in (
            "account:" + username[:80], "ip:" + (request.remote_addr or "unknown"))]
        attempts = [db.session.get(LoginAttempt, key) for key in keys]
        if any(attempt and attempt.locked_until and attempt.locked_until > now for attempt in attempts):
            flash("登录失败次数过多，请 15 分钟后重试。", "error")
            return render_template("login.html"), 429
        user = User.query.filter_by(username=username).first()
        password = request.form.get("password", "")
        hashed = user and user.password.startswith(("scrypt:", "pbkdf2:"))
        if user and user.enabled and password_matches(user, password):
            if not hashed:
                user.password = generate_password_hash(password)
            if attempts[0]:
                db.session.delete(attempts[0])
            db.session.commit()
            session.clear()
            login_user(user)
            return redirect(url_for("dashboard"))
        for key, attempt in zip(keys, attempts):
            if not attempt:
                attempt = LoginAttempt(key=key, failures=0, window_started=now)
                db.session.add(attempt)
            if attempt.window_started <= now - timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"]):
                attempt.failures, attempt.window_started = 0, now
            attempt.failures += 1
            if attempt.failures >= current_app.config["LOGIN_MAX_FAILURES"]:
                attempt.locked_until = now + timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])
        db.session.commit()
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
            previous = {"name": current_user.real_name, "student_number": current_user.student_number}
            updated = {"name": name, "student_number": number}
            if previous != updated:
                account_event(current_user, "IDENTITY_CHANGE", {"before": previous, "after": updated})
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
    events = AccountEvent.query.filter_by(user_id=current_user.id, action="IDENTITY_CHANGE").order_by(AccountEvent.id.desc()).limit(20).all()
    for event in events:
        event.change = json.loads(event.details)
    return render_template("profile.html", identity_events=events)


@login_required
def change_password():
    password = request.form.get("new_password", "")
    if not password_matches(current_user, request.form.get("current_password", "")):
        flash("当前密码不正确。", "error")
        return redirect(url_for("profile"))
    if not 8 <= len(password) <= 128 or password != request.form.get("confirm_password"):
        flash("新密码需为 8-128 位，且两次输入一致。", "error")
        return redirect(url_for("profile"))
    if password_matches(current_user, password):
        flash("新密码不能与当前密码相同。", "error")
        return redirect(url_for("profile"))
    user = db.session.get(User, current_user.id)
    user.password = generate_password_hash(password)
    user.auth_version += 1
    account_event(user, "PASSWORD_CHANGE")
    db.session.commit()
    logout_user()
    session.clear()
    flash("密码已修改，所有旧会话已失效，请重新登录。", "success")
    return redirect(url_for("login"))


def register_account_routes(app):
    for path, view, methods in (("/register", register, ["GET", "POST"]), ("/login", login, ["GET", "POST"]),
                                ("/logout", logout, ["POST"]), ("/profile", profile, ["GET", "POST"]),
                                ("/account/password", change_password, ["POST"])):
        app.add_url_rule(path, view_func=view, methods=methods)
