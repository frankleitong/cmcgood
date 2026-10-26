import re

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from . import constants as C
from . import notify
from .auth import active_users, approver_emails, has_role, login_required, role_required
from .db import get_db, log_event, query, query_one
from .platforms import web_url
from .util import iso_days_later, now_iso

bp = Blueprint('repos', __name__)

NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$')

SORTS = {
    'name': 'r.platform, r.namespace, r.name',
    'idle': 'COALESCE(r.last_commit_at, r.created_at) ASC',
    'activity': 'r.last_commit_at IS NULL, r.last_commit_at DESC',
    'created': 'r.created_at DESC',
}


def get_repo_or_404(repo_id):
    repo = query_one(
        '''SELECT r.*, u.username AS owner_username, u.display_name AS owner_name, u.email AS owner_email
           FROM repos r LEFT JOIN users u ON u.id = r.owner_id WHERE r.id = ?''', repo_id)
    if repo is None:
        abort(404)
    return repo


def can_manage(repo):
    return has_role(g.user, 'admin') or repo['owner_id'] == g.user['id']


def find_registered(platform, namespace, name):
    return query_one('SELECT id FROM repos WHERE platform = ? AND namespace = ? AND name = ?',
                     platform, namespace, name)


def validate_repo_fields(form, check_identity=True, exclude_id=None):
    """校验 平台/组织/仓库名/负责人 等字段，返回错误列表。"""
    errors = []
    if check_identity:
        if form.get('platform') not in C.PLATFORMS:
            errors.append('请选择平台。')
        if not NAME_RE.match(form.get('namespace', '').strip()):
            errors.append('组织/用户名格式不正确。')
        if not NAME_RE.match(form.get('name', '').strip()):
            errors.append('仓库名只能包含字母、数字、点、下划线和中划线，最长 100 位。')
        if not errors:
            existing = find_registered(form['platform'], form['namespace'].strip(), form['name'].strip())
            if existing and existing['id'] != exclude_id:
                errors.append('该仓库已经登记过了。')
    owner_id = form.get('owner_id') or None
    if owner_id and not query_one('SELECT 1 FROM users WHERE id = ? AND active = 1', owner_id):
        errors.append('负责人不存在。')
    if form.get('visibility', 'private') not in C.VISIBILITY:
        errors.append('可见性不正确。')
    return errors


def namespace_choices():
    choices = []
    for platform, pcfg in current_app.config['PLATFORMS'].items():
        for raw in pcfg['namespaces']:
            ns = raw.split(':', 1)[1] if raw.lower().startswith('user:') else raw
            choices.append((platform, ns))
    return choices


def teams():
    return [r[0] for r in query("SELECT DISTINCT team FROM repos WHERE team != '' ORDER BY team")]


@bp.route('/')
@login_required
def index():
    args = request.args
    where, params = [], []
    q = args.get('q', '').strip()
    if q:
        where.append('(r.name LIKE ? OR r.namespace LIKE ? OR r.description LIKE ? OR r.team LIKE ?)')
        params += ['%' + q + '%'] * 4
    platform = args.get('platform', '')
    if platform in C.PLATFORMS:
        where.append('r.platform = ?')
        params.append(platform)
    status = args.get('status', '')
    if status in C.REPO_STATUS:
        where.append('r.status = ?')
        params.append(status)
    elif status == 'missing':
        where.append("r.on_platform = 0 AND r.status != 'archived'")
    elif status != 'all':
        where.append("r.status != 'archived'")
    team = args.get('team', '')
    if team:
        where.append('r.team = ?')
        params.append(team)
    if args.get('mine'):
        where.append('r.owner_id = ?')
        params.append(g.user['id'])
    sort = args.get('sort', 'name')
    sql = '''SELECT r.*, u.username AS owner_username, u.display_name AS owner_name
             FROM repos r LEFT JOIN users u ON u.id = r.owner_id'''
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY ' + SORTS.get(sort, SORTS['name'])
    repos = query(sql, *params)

    stats = {row[0]: row[1] for row in query('SELECT status, COUNT(*) FROM repos GROUP BY status')}
    stats['total'] = sum(stats.values())
    stats['missing'] = query_one("SELECT COUNT(*) FROM repos WHERE on_platform = 0 AND status != 'archived'")[0]
    last_sync = query_one("SELECT * FROM scan_runs WHERE kind = 'sync' AND status != 'running' "
                          "ORDER BY id DESC LIMIT 1")
    stats['unregistered'] = 0
    if last_sync:
        stats['unregistered'] = query_one("SELECT COUNT(*) FROM scan_findings WHERE run_id = ? "
                                          "AND issue = 'unregistered' AND resolved_at IS NULL", last_sync['id'])[0]
    stats['pending'] = query_one("SELECT COUNT(*) FROM applications WHERE status = 'pending'")[0]
    return render_template('repos/index.html', repos=repos, stats=stats, teams=teams(), last_sync=last_sync,
                           args=args, sort=sort)


@bp.route('/repos/<int:repo_id>')
@login_required
def detail(repo_id):
    repo = get_repo_or_404(repo_id)
    events = query('''SELECT e.*, u.username, u.display_name FROM events e
                      LEFT JOIN users u ON u.id = e.user_id
                      WHERE e.repo_id = ? ORDER BY e.id DESC LIMIT 100''', repo_id)
    apps = query('''SELECT a.*, u.username AS applicant_username, u.display_name AS applicant_name
                    FROM applications a JOIN users u ON u.id = a.applicant_id
                    WHERE a.repo_id = ? ORDER BY a.id DESC''', repo_id)
    pending_archive = any(a['kind'] == 'archive' and a['status'] == 'pending' for a in apps)
    return render_template('repos/detail.html', repo=repo, events=events, apps=apps,
                           pending_archive=pending_archive, can_manage=can_manage(repo))


@bp.route('/repos/new', methods=['GET', 'POST'])
@role_required('admin')
def new():
    form = request.form if request.method == 'POST' else {'platform': 'github', 'visibility': 'private'}
    if request.method == 'POST':
        errors = validate_repo_fields(form)
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            platform, ns, name = form['platform'], form['namespace'].strip(), form['name'].strip()
            now = now_iso()
            conn = get_db()
            cur = conn.execute(
                '''INSERT INTO repos (platform, namespace, name, url, description, team, visibility, owner_id,
                                      source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)''',
                (platform, ns, name, form.get('url', '').strip() or web_url(current_app.config, platform, ns, name),
                 form.get('description', '').strip(), form.get('team', '').strip(), form.get('visibility', 'private'),
                 form.get('owner_id') or None, now, now))
            log_event(conn, 'create', '管理员手工登记', repo_id=cur.lastrowid, user_id=g.user['id'])
            conn.commit()
            flash('已登记。', 'ok')
            return redirect(url_for('repos.detail', repo_id=cur.lastrowid))
    return render_template('repos/form.html', form=form, repo=None, users=active_users(), teams=teams(),
                           ns_choices=namespace_choices())


@bp.route('/repos/<int:repo_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(repo_id):
    repo = get_repo_or_404(repo_id)
    if not can_manage(repo):
        abort(403)
    is_admin = has_role(g.user, 'admin')
    form = request.form if request.method == 'POST' else dict(repo)
    if request.method == 'POST':
        errors = validate_repo_fields(form, check_identity=is_admin, exclude_id=repo_id)
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            changes = []
            fields = ['description', 'team', 'visibility', 'url']
            if is_admin:
                fields += ['platform', 'namespace', 'name', 'owner_id']
            values = {}
            for f in fields:
                v = form.get(f, '')
                v = v.strip() if isinstance(v, str) else v
                if f == 'owner_id':
                    v = int(v) if v else None
                values[f] = v
                if (repo[f] if repo[f] is not None else '') != (v if v is not None else ''):
                    changes.append(f)
            if changes:
                sets = ', '.join('{} = ?'.format(f) for f in fields)
                conn.execute('UPDATE repos SET {}, updated_at = ? WHERE id = ?'.format(sets),
                             [values[f] for f in fields] + [now_iso(), repo_id])
                log_event(conn, 'edit', '修改了：' + '、'.join(changes), repo_id=repo_id, user_id=g.user['id'])
                conn.commit()
            flash('已保存。', 'ok')
            return redirect(url_for('repos.detail', repo_id=repo_id))
    return render_template('repos/form.html', form=form, repo=repo, users=active_users(), teams=teams(),
                           ns_choices=namespace_choices(), is_admin=is_admin)


@bp.route('/repos/<int:repo_id>/archive-request', methods=['POST'])
@login_required
def archive_request(repo_id):
    repo = get_repo_or_404(repo_id)
    if not can_manage(repo):
        abort(403)
    if repo['status'] == 'archived':
        flash('该仓库已归档。', 'error')
        return redirect(url_for('repos.detail', repo_id=repo_id))
    if query_one("SELECT 1 FROM applications WHERE repo_id = ? AND kind = 'archive' AND status = 'pending'", repo_id):
        flash('已有待审批的归档申请。', 'error')
        return redirect(url_for('repos.detail', repo_id=repo_id))
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('请填写归档理由。', 'error')
        return redirect(url_for('repos.detail', repo_id=repo_id))
    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO applications (kind, applicant_id, repo_id, platform, namespace, name, description, team,
                                     visibility, owner_id, reason, created_at)
           VALUES ('archive', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (g.user['id'], repo_id, repo['platform'], repo['namespace'], repo['name'], repo['description'],
         repo['team'], repo['visibility'], repo['owner_id'], reason, now_iso()))
    log_event(conn, 'archive_request', reason, repo_id=repo_id, user_id=g.user['id'], application_id=cur.lastrowid)
    conn.commit()
    link = url_for('applications.detail', app_id=cur.lastrowid, _external=True)
    title = '归档申请：{}:{}/{}'.format(repo['platform'], repo['namespace'], repo['name'])
    body = '{} 申请归档代码仓 {}/{}\n理由：{}\n\n审批：{}'.format(
        g.user['display_name'] or g.user['username'], repo['namespace'], repo['name'], reason, link)
    notify.send_email(current_app.config, approver_emails(), title, body)
    notify.send_webhook(current_app.config, title + '\n' + body)
    flash('归档申请已提交，等待审批。', 'ok')
    return redirect(url_for('applications.detail', app_id=cur.lastrowid))


@bp.route('/repos/<int:repo_id>/snooze', methods=['POST'])
@login_required
def snooze(repo_id):
    repo = get_repo_or_404(repo_id)
    if not can_manage(repo):
        abort(403)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('请说明暂不归档的理由。', 'error')
        return redirect(url_for('repos.detail', repo_id=repo_id))
    days = current_app.config['SNOOZE_DAYS']
    conn = get_db()
    conn.execute('UPDATE repos SET snooze_until = ?, snooze_reason = ?, updated_at = ? WHERE id = ?',
                 (iso_days_later(days), reason, now_iso(), repo_id))
    log_event(conn, 'snooze', '暂不归档 {} 天：{}'.format(days, reason), repo_id=repo_id, user_id=g.user['id'])
    conn.commit()
    flash('已记录，{} 天内不再提醒。'.format(days), 'ok')
    return redirect(url_for('repos.detail', repo_id=repo_id))


@bp.route('/repos/<int:repo_id>/restore', methods=['POST'])
@role_required('admin')
def restore(repo_id):
    get_repo_or_404(repo_id)
    conn = get_db()
    conn.execute("UPDATE repos SET status = 'active', archived_at = NULL, snooze_until = NULL, "
                 "last_reminded_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), repo_id))
    log_event(conn, 'restore', '恢复为活跃', repo_id=repo_id, user_id=g.user['id'])
    conn.commit()
    flash('已恢复为活跃。', 'ok')
    return redirect(url_for('repos.detail', repo_id=repo_id))


@bp.route('/repos/<int:repo_id>/delete', methods=['POST'])
@role_required('admin')
def delete(repo_id):
    repo = get_repo_or_404(repo_id)
    conn = get_db()
    conn.execute('DELETE FROM repos WHERE id = ?', (repo_id,))
    log_event(conn, 'delete', '删除登记记录 {}:{}/{}'.format(repo['platform'], repo['namespace'], repo['name']),
              user_id=g.user['id'])
    conn.commit()
    flash('登记记录已删除（平台上的仓库不受影响）。', 'ok')
    return redirect(url_for('repos.index'))
