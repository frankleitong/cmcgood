import functools
import re
import secrets

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import constants as C
from .db import get_db, query, query_one
from .util import now_iso

bp = Blueprint('auth', __name__)

USERNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$')


def has_role(user, role):
    return bool(user) and C.ROLE_LEVEL.get(user['role'], 0) >= C.ROLE_LEVEL[role]


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


def _before_request():
    g.user = None
    uid = session.get('uid')
    if uid:
        g.user = query_one('SELECT * FROM users WHERE id = ? AND active = 1', uid)
    if request.method == 'POST':
        token = session.get('_csrf')
        if not token or request.form.get('_csrf') != token:
            abort(400, description='表单已过期，请刷新页面后重试。')


def _nav_context():
    pending = 0
    if has_role(g.get('user'), 'approver'):
        pending = query_one("SELECT COUNT(*) FROM applications WHERE status = 'pending'")[0]
    return {'nav_pending': pending}


def init_app(app):
    app.before_request(_before_request)
    app.context_processor(_nav_context)
    app.jinja_env.globals.update(csrf_token=csrf_token, has_role=has_role, C=C)


def _safe_next(target):
    if target and target.startswith('/') and not target.startswith('//'):
        return request.script_root + target
    return url_for('repos.index')


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            nxt = request.full_path if request.method == 'GET' else None
            return redirect(url_for('auth.login', next=nxt))
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not has_role(g.user, role):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def validate_user_fields(form, require_password, check_username=True):
    errors = []
    if check_username:
        username = form.get('username', '').strip()
        if not USERNAME_RE.match(username):
            errors.append('用户名需为 2-32 位字母、数字、点、下划线或中划线。')
        elif query_one('SELECT 1 FROM users WHERE username = ?', username):
            errors.append('用户名已存在。')
    email = form.get('email', '').strip()
    if email and '@' not in email:
        errors.append('邮箱格式不正确。')
    password = form.get('password', '')
    if require_password or password:
        if len(password) < 8:
            errors.append('密码至少 8 位。')
        elif password != form.get('password2', ''):
            errors.append('两次输入的密码不一致。')
    return errors


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = query_one('SELECT * FROM users WHERE username = ?', username)
        if user and user['active'] and check_password_hash(user['password_hash'], password):
            session.clear()
            session.permanent = True
            session['uid'] = user['id']
            return redirect(_safe_next(request.args.get('next')))
        flash('用户名或密码错误，或账号已停用。', 'error')
    return render_template('auth/login.html')


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if not current_app.config['ALLOW_REGISTER']:
        abort(404)
    if request.method == 'POST':
        errors = validate_user_fields(request.form, require_password=True)
        if not request.form.get('email', '').strip():
            errors.append('请填写邮箱，用于接收审批和归档提醒。')
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            # 系统里还没有任何用户时，第一个注册的人自动成为管理员
            first = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
            conn.execute(
                'INSERT INTO users (username, display_name, email, password_hash, role, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (request.form['username'].strip(), request.form.get('display_name', '').strip(),
                 request.form['email'].strip(), generate_password_hash(request.form['password']),
                 'admin' if first else 'developer', now_iso()))
            conn.commit()
            flash('注册成功，请登录。' + ('（你是第一个用户，已设为管理员）' if first else ''), 'ok')
            return redirect(url_for('auth.login'))
    return render_template('auth/register.html')


@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        form = request.form
        errors = validate_user_fields(form, require_password=False, check_username=False)
        if form.get('password') and not check_password_hash(g.user['password_hash'], form.get('old_password', '')):
            errors.append('当前密码不正确。')
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            conn.execute('UPDATE users SET display_name = ?, email = ? WHERE id = ?',
                         (form.get('display_name', '').strip(), form.get('email', '').strip(), g.user['id']))
            if form.get('password'):
                conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                             (generate_password_hash(form['password']), g.user['id']))
            conn.commit()
            flash('已保存。', 'ok')
            return redirect(url_for('auth.profile'))
    return render_template('auth/profile.html')


def active_users():
    return query('SELECT id, username, display_name, email FROM users WHERE active = 1 ORDER BY username')


def approver_emails():
    return [r['email'] for r in query(
        "SELECT email FROM users WHERE active = 1 AND role IN ('approver', 'admin') AND email != ''")]
