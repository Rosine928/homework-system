"""Verified email binding and one-time password reset using SMTP."""
from datetime import timedelta
from email.message import EmailMessage
from hashlib import sha256
import hmac
import os
import re
from secrets import randbelow
import smtplib
import ssl

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from accounts import account_event, password_matches
from models import EmailChallenge, LoginAttempt, User, beijing_now, db


def configure_email(app):
    app.config.update(
        MAIL_SERVER=os.environ.get('MAIL_SERVER', ''),
        MAIL_PORT=int(os.environ.get('MAIL_PORT', '465')),
        MAIL_USERNAME=os.environ.get('MAIL_USERNAME', ''),
        MAIL_PASSWORD=os.environ.get('MAIL_PASSWORD', ''),
        MAIL_FROM=os.environ.get('MAIL_FROM', '') or os.environ.get('MAIL_USERNAME', ''),
        MAIL_USE_SSL=os.environ.get('MAIL_USE_SSL', '1') == '1',
        MAIL_USE_TLS=os.environ.get('MAIL_USE_TLS', '0') == '1',
        MAIL_TIMEOUT=10,
    )
    app.jinja_env.globals['email_service_ready'] = email_service_ready


def email_service_ready():
    config = current_app.config
    return bool(config['MAIL_SERVER'] and normalize_email(config['MAIL_FROM'])
                and config['MAIL_USE_SSL'] != config['MAIL_USE_TLS'])


def normalize_email(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(
        r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
        r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        return None
    if len(value.split('@')[0]) > 64:
        return None
    return value


def send_verification_email(email, code, purpose):
    config = current_app.config
    message = EmailMessage()
    message['Subject'] = '知行课堂 · ' + ('绑定邮箱验证码' if purpose == 'bind' else '找回密码验证码')
    message['From'] = config['MAIL_FROM']
    message['To'] = email
    message.set_content(f'您的验证码是：{code}\n\n10 分钟内有效，只能使用一次。'
                        '\n请勿向他人提供验证码。如非本人操作，请忽略此邮件。')
    context = ssl.create_default_context()
    transport = smtplib.SMTP_SSL if config['MAIL_USE_SSL'] else smtplib.SMTP
    options = {'timeout': config['MAIL_TIMEOUT']}
    if config['MAIL_USE_SSL']:
        options['context'] = context
    with transport(config['MAIL_SERVER'], config['MAIL_PORT'], **options) as smtp:
        if config['MAIL_USE_TLS']:
            smtp.starttls(context=context)
        if config['MAIL_USERNAME']:
            smtp.login(config['MAIL_USERNAME'], config['MAIL_PASSWORD'])
        if smtp.send_message(message):
            raise smtplib.SMTPException('Recipient refused')


def challenge_key(purpose, subject):
    return sha256(f'{purpose}:{subject}'.encode()).hexdigest()


def code_hash(key, code):
    # A keyed digest protects low-entropy numeric codes in a database-only leak.
    return hmac.new(current_app.secret_key.encode(), f'{key}:{code}'.encode(), 'sha256').hexdigest()


def consume_limit(namespace, subject, *, maximum, seconds, cooldown=0):
    key = challenge_key(namespace, subject)
    item = db.session.get(LoginAttempt, key)
    now = beijing_now()
    if item and item.locked_until and item.locked_until > now:
        return False
    if item is None:
        item = LoginAttempt(key=key, failures=0, window_started=now)
        db.session.add(item)
    if item.window_started <= now - timedelta(seconds=seconds):
        item.failures, item.window_started = 0, now
    if item.failures >= maximum:
        return False
    item.failures += 1
    item.locked_until = now + timedelta(seconds=cooldown) if cooldown else None
    return True


def send_challenge(user, email, purpose):
    subject = str(user.id) if purpose == 'bind' else email
    key = challenge_key(purpose, subject)
    record = db.session.get(EmailChallenge, key)
    if record is None:
        record = EmailChallenge(key=key)
        db.session.add(record)
    code = f'{randbelow(1000000):06d}'
    digest = code_hash(key, code)
    record.user_id, record.purpose, record.email = user.id, purpose, email
    record.token_hash, record.auth_version = digest, user.auth_version
    record.failures, record.created_at = 0, beijing_now()
    record.expires_at = record.created_at + timedelta(minutes=10)
    # Release SQLite's write lock before waiting for the external SMTP service.
    db.session.commit()
    try:
        send_verification_email(email, code, purpose)
    except (OSError, smtplib.SMTPException, ValueError):
        # Do not log addresses, message bodies, credentials or verification codes.
        current_app.logger.warning('Verification email delivery failed purpose=%s', purpose)
        EmailChallenge.query.filter_by(key=key, token_hash=digest).delete()
        db.session.commit()
        return False
    return True


def valid_challenge(record, user, email, code):
    now = beijing_now()
    return bool(record and user and user.enabled and record.user_id == user.id
                and record.email == email and record.auth_version == user.auth_version
                and record.expires_at > now and record.failures < 5
                and re.fullmatch(r'[0-9]{6}', code)
                and hmac.compare_digest(record.token_hash, code_hash(record.key, code)))


def send_limit(email, purpose):
    ip = request.remote_addr or 'unknown'
    # Check both independently; never short circuit the IP accounting.
    address_ok = consume_limit('email-send-address', email, maximum=5, seconds=900)
    purpose_ok = consume_limit('email-send-' + purpose, email, maximum=5, seconds=900, cooldown=60)
    ip_ok = consume_limit('email-send-ip', ip, maximum=20, seconds=900)
    user_ok = True
    if purpose == 'bind':
        user_ok = consume_limit('email-bind-user', str(current_user.id), maximum=5, seconds=900, cooldown=60)
    return address_ok and purpose_ok and ip_ok and user_ok


def send_password_email():
    email = normalize_email(request.form.get('email', ''))
    if not email:
        flash('请输入有效的邮箱地址。', 'error')
        return render_template('forgot_password.html'), 400
    if not email_service_ready():
        flash('邮箱服务尚未配置，请联系系统维护人员。', 'error')
        return render_template('forgot_password.html'), 503
    allowed = send_limit(email, 'reset')
    db.session.commit()
    if not allowed:
        flash('发送过于频繁，请至少间隔 60 秒；多次发送后请等待 15 分钟。', 'error')
        return render_template('forgot_password.html'), 429
    user = User.query.filter_by(email=email, enabled=True).first()
    if user and user.email_verified_at:
        send_challenge(user, email, 'reset')
    session['reset_email'] = email
    # Identical responses for unknown, unverified, disabled and delivery failures.
    flash('如果该邮箱已绑定可用账号，您将收到验证码。请检查收件箱和垃圾邮件；未收到可稍后重试。', 'success')
    return redirect(url_for('forgot_password'))


def reset_password_by_email():
    email = normalize_email(request.form.get('email', ''))
    code = request.form.get('email_code', '').strip()
    ip_ok = consume_limit('email-verify-ip', request.remote_addr or 'unknown', maximum=30, seconds=900)
    address_ok = consume_limit('email-verify-address', email or '', maximum=10, seconds=900)
    if not ip_ok or not address_ok:
        db.session.commit()
        flash('验证过于频繁，请 15 分钟后重试。', 'error')
        return render_template('forgot_password.html'), 429
    user = User.query.filter_by(email=email).first() if email else None
    record = db.session.get(EmailChallenge, challenge_key('reset', email or ''))
    if not valid_challenge(record, user, email, code) or not user.email_verified_at:
        if record:
            record.failures += 1
        db.session.commit()
        flash('邮箱或验证码无效，验证码可能已过期、已使用或错误次数过多。', 'error')
        return render_template('forgot_password.html'), 400
    password = request.form.get('new_password', '')
    if not 8 <= len(password) <= 128 or password != request.form.get('confirm_password'):
        db.session.commit()
        flash('新密码需为 8-128 位，且两次输入一致。', 'error')
        return render_template('forgot_password.html'), 400
    if password_matches(user, password):
        db.session.commit()
        flash('新密码不能与原密码相同。', 'error')
        return render_template('forgot_password.html'), 400
    return complete_password_reset(user, password)


def forgot_password():
    return render_template('forgot_password.html')


def complete_password_reset(user, password):
    user.password = generate_password_hash(password)
    user.auth_version += 1
    EmailChallenge.query.filter_by(user_id=user.id).delete(synchronize_session='fetch')
    login_key = sha256(('account:' + user.username).encode('utf-8')).hexdigest()
    login_attempt = db.session.get(LoginAttempt, login_key)
    if login_attempt:
        db.session.delete(login_attempt)
    account_event(user, 'PASSWORD_RECOVERED')
    db.session.commit()
    if current_user.is_authenticated:
        logout_user()
    session.clear()
    flash('密码已重置，验证码和所有旧登录会话已失效。请使用新密码登录。', 'success')
    return redirect(url_for('login'))


@login_required
def send_binding_email():
    email = normalize_email(request.form.get('email', ''))
    password_ok = consume_limit('email-bind-password', str(current_user.id), maximum=10, seconds=900)
    if not password_ok or not password_matches(current_user, request.form.get('current_password', '')):
        db.session.commit()
        flash('当前密码不正确或验证次数过多，请稍后重试。', 'error')
        return redirect(url_for('profile'))
    if not email:
        db.session.commit()
        flash('请输入有效的邮箱地址。', 'error')
        return redirect(url_for('profile'))
    if not email_service_ready():
        db.session.commit()
        flash('邮箱服务尚未配置，请联系系统维护人员。', 'error')
        return redirect(url_for('profile'))
    if not send_limit(email, 'bind'):
        db.session.commit()
        flash('发送过于频繁，请至少间隔 60 秒；多次发送后请等待 15 分钟。', 'error')
        return redirect(url_for('profile'))
    db.session.commit()
    if User.query.filter(User.email == email, User.id != current_user.id).first():
        flash('该邮箱暂时无法绑定，请使用其他邮箱或联系维护人员。', 'error')
    elif send_challenge(current_user, email, 'bind'):
        session['binding_email'] = email
        flash('验证码已发送，请在 10 分钟内输入验证码完成绑定。', 'success')
    else:
        flash('邮件发送失败，请稍后重试或联系系统维护人员。', 'error')
    return redirect(url_for('profile'))


@login_required
def confirm_binding_email():
    email = normalize_email(request.form.get('email', ''))
    record = db.session.get(EmailChallenge, challenge_key('bind', str(current_user.id)))
    allowed = consume_limit('email-bind-verify', str(current_user.id), maximum=15, seconds=900)
    if not allowed or not valid_challenge(record, current_user, email, request.form.get('email_code', '').strip()):
        if record:
            record.failures += 1
        db.session.commit()
        flash('邮箱或验证码无效，请重新获取验证码后再试。', 'error')
        return redirect(url_for('profile'))
    current_user.email, current_user.email_verified_at = email, beijing_now()
    # Changing the address revokes all previously issued email challenges.
    EmailChallenge.query.filter_by(user_id=current_user.id).delete(synchronize_session='fetch')
    account_event(current_user, 'EMAIL_VERIFIED')
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('该邮箱暂时无法绑定，请使用其他邮箱或联系维护人员。', 'error')
    else:
        session.pop('binding_email', None)
        flash('邮箱绑定成功，可在登录页通过邮箱验证码找回密码。', 'success')
    return redirect(url_for('profile'))


def register_email_routes(app):
    app.add_url_rule('/forgot-password', view_func=forgot_password, methods=['GET'])
    for path, view in (
        ('/forgot-password/email/send', send_password_email),
        ('/forgot-password/email/reset', reset_password_by_email),
        ('/account/email/send', send_binding_email),
        ('/account/email/verify', confirm_binding_email),
    ):
        app.add_url_rule(path, view_func=view, methods=['POST'])
