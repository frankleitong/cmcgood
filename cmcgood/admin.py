from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from . import constants as C
from .auth import role_required, validate_user_fields
from .db import get_db, query, query_one
from .util import now_iso

bp = Blueprint('admin', __name__, url_prefix='/admin')


@bp.route('/users')
@role_required('admin')
def users():
    rows = query('''SELECT u.*, (SELECT COUNT(*) FROM repos r WHERE r.owner_id = u.id AND r.status != 'archived')
                           AS repo_count
                    FROM users u ORDER BY u.active DESC, u.username''')
    return render_template('admin/users.html', users=rows)


@bp.route('/users/new', methods=['GET', 'POST'])
@role_required('admin')
def user_new():
    form = request.form if request.method == 'POST' else {'role': 'developer', 'active': '1'}
    if request.method == 'POST':
        errors = validate_user_fields(form, require_password=True)
        if form.get('role') not in C.ROLES:
            errors.append('角色不正确。')
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            conn.execute('INSERT INTO users (username, display_name, email, password_hash, role, created_at) '
                         'VALUES (?, ?, ?, ?, ?, ?)',
                         (form['username'].strip(), form.get('display_name', '').strip(),
                          form.get('email', '').strip(), generate_password_hash(form['password']),
                          form['role'], now_iso()))
            conn.commit()
            flash('用户已创建。', 'ok')
            return redirect(url_for('admin.users'))
    return render_template('admin/user_form.html', form=form, user=None)


@bp.route('/users/<int:user_id>', methods=['GET', 'POST'])
@role_required('admin')
def user_edit(user_id):
    user = query_one('SELECT * FROM users WHERE id = ?', user_id)
    if user is None:
        abort(404)
    form = request.form if request.method == 'POST' else dict(user)
    if request.method == 'POST':
        errors = validate_user_fields(form, require_password=False, check_username=False)
        role = form.get('role')
        active = 1 if form.get('active') == '1' else 0
        if role not in C.ROLES:
            errors.append('角色不正确。')
        if user_id == g.user['id'] and (role != 'admin' or not active):
            errors.append('不能取消自己的管理员身份或停用自己。')
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            conn.execute('UPDATE users SET display_name = ?, email = ?, role = ?, active = ? WHERE id = ?',
                         (form.get('display_name', '').strip(), form.get('email', '').strip(), role, active, user_id))
            if form.get('password'):
                conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                             (generate_password_hash(form['password']), user_id))
            conn.commit()
            flash('已保存。', 'ok')
            return redirect(url_for('admin.users'))
    return render_template('admin/user_form.html', form=form, user=user)
